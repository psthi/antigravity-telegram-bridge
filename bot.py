#!/usr/bin/env python3
"""
Interactive Telegram Bridge — Async Antigravity CLI ↔ Telegram

Rewritten per requirements.md:
  - asyncio + aiohttp for concurrent Telegram polling and CLI I/O
  - asyncio.create_subprocess_exec with continuous stdout/stderr reading
  - Interactive prompt detection (e.g. Proceed? [Y/n] / [y/N]) → flush to Telegram & pause
  - State machine: when CLI waits for input, next Telegram message → stdin
  - Pure CLI bridge, no MCP, uses `agy --prompt-interactive` (configurable via AGY_INTERACTIVE_FLAG)
  - Robust error handling so daemon does not crash

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, AGY_CONVERSATION_ID, AGY_PATH,
     AGY_INTERACTIVE_FLAG (default: --prompt-interactive; set to 0/empty to disable)
"""
import asyncio
import aiohttp
import re
import os
import sys
import json
import shutil
import signal
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CONVERSATION_ID = os.getenv("AGY_CONVERSATION_ID", "telegram-bridge-session")
AGY_PATH = os.getenv("AGY_PATH") or shutil.which("agy") or "agy"
# Set to "0" or "" to disable the interactive flag
AGY_INTERACTIVE_FLAG = os.getenv("AGY_INTERACTIVE_FLAG", "--prompt-interactive")

if not TOKEN or not CHAT_ID:
    print("Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
    sys.exit(1)

API_BASE = f"https://api.telegram.org/bot{TOKEN}/"
MAX_TELEGRAM_LEN = 4000  # under 4096 to leave headroom for prefixes
POLL_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Prompt detection  (requirement #3)
# ---------------------------------------------------------------------------
# Covers: Proceed? [Y/n], [y/N], [Y/N], (y/n), Do you want to proceed?, Allow? [Y/n] etc.
PROMPT_RE = re.compile(
    r"(\[Y/n\]|\[y/N\]|\[Y/N\]|\[y/n\]|\(Y/n\)|\(y/N\)|\(Y/N\)|\(y/n\)"
    r"|Proceed\s*\?\s*\["
    r"|Do you want to (proceed|continue)"
    r"|Allow\?.*\[)",
    re.IGNORECASE,
)

# Fallback generic: any bracket with Y/N slash pattern
GENERIC_PROMPT_RE = re.compile(r"\[[^\]]*?[Yy]\s*/\s*[Nn][^\]]*?\]")

def contains_prompt(text: str) -> bool:
    return bool(PROMPT_RE.search(text) or GENERIC_PROMPT_RE.search(text))

# ---------------------------------------------------------------------------
# Global state machine (requirement #4)
# ---------------------------------------------------------------------------
state_lock = asyncio.Lock()
active_process: asyncio.subprocess.Process | None = None
awaiting_input: bool = False
# Keep reference to reader tasks so they can be cancelled on /cancel
reader_tasks: list[asyncio.Task] = []

# aiohttp session — created in main()
http_session: aiohttp.ClientSession | None = None

# ---------------------------------------------------------------------------
# Telegram helpers (async)
# ---------------------------------------------------------------------------
async def send_message(text: str):
    """Chunk and send via Telegram Bot API. Never raises."""
    if not text or not text.strip():
        text = "(Action completed silently)"
    # Ensure http_session exists
    if http_session is None or http_session.closed:
        print("send_message: no active http session, dropping message")
        return
    # Chunk at MAX_TELEGRAM_LEN; prefer to split on newline boundaries when possible
    chunks = [text[i:i + MAX_TELEGRAM_LEN] for i in range(0, len(text), MAX_TELEGRAM_LEN)]
    for chunk in chunks:
        # Telegram parse_mode plain text (no markdown to avoid escaping issues)
        data = {"chat_id": CHAT_ID, "text": chunk}
        try:
            async with http_session.post(API_BASE + "sendMessage", data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"send_message failed {resp.status}: {body[:500]}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Failed to send message: {e}")
        # Small throttle between chunks to avoid flood limits
        if len(chunks) > 1:
            await asyncio.sleep(0.2)

async def get_updates(offset: int | None) -> dict:
    url = API_BASE + f"getUpdates?timeout={POLL_TIMEOUT}"
    if offset is not None:
        url += f"&offset={offset}"
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT + 10)) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"getUpdates HTTP {resp.status}: {body[:500]}")
                return {"ok": False, "result": []}
            data = await resp.json()
            return data
    except asyncio.TimeoutError:
        return {"ok": False, "result": []}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"Error fetching updates: {e}")
        return {"ok": False, "result": []}

# ---------------------------------------------------------------------------
# CLI subprocess management (requirements #2, #3, #5)
# ---------------------------------------------------------------------------
def build_agy_command(prompt: str) -> list[str]:
    """Build agy command. Requirement #5: pure CLI bridge using agy --prompt-interactive."""
    cmd: list[str] = [AGY_PATH, "--dangerously-skip-permissions"]
    flag = (AGY_INTERACTIVE_FLAG or "").strip()
    if flag and flag not in ("0", "false", "False", "no", "off"):
        cmd.append(flag)
    cmd.extend(["--conversation", CONVERSATION_ID, "-p", prompt])
    return cmd

async def _stream_stdout(proc: asyncio.subprocess.Process):
    """Continuously read stdout, detect prompts, flush to Telegram."""
    global awaiting_input
    buffer = ""
    try:
        while True:
            chunk_bytes = await proc.stdout.read(1024)
            if not chunk_bytes:
                break
            try:
                chunk = chunk_bytes.decode("utf-8", errors="replace")
            except Exception:
                chunk = chunk_bytes.decode(errors="replace")
            buffer += chunk
            # Debug
            # print(f"[stdout chunk] {chunk!r}")

            if contains_prompt(buffer):
                # Requirement #3: flush buffer and pause, keeping CLI alive
                to_send = buffer.strip()
                if to_send:
                    await send_message(to_send)
                # Helpful hint for the user
                await send_message("⏸️ Agent is waiting for approval — reply `y` / `n` (or `yes`/`no`).")
                buffer = ""
                async with state_lock:
                    awaiting_input = True
                # Process is now blocked on stdin; this reader will block on next read()
                # until the user responds and we write to stdin.
                # Continue loop — next read() will await new output after stdin is fed.
                continue

            # Streaming interim output for long tasks (avoid silent hanging)
            # If buffer grows large without a prompt, flush incrementally.
            if len(buffer) > MAX_TELEGRAM_LEN * 2:
                # Keep last 500 chars to avoid splitting a prompt across boundary
                flush_part = buffer[:-500]
                # Try to split on last newline for cleaner chunks
                last_nl = flush_part.rfind("\n")
                if last_nl > 1000:
                    flush_part = flush_part[: last_nl + 1]
                    buffer = buffer[last_nl + 1 :]
                else:
                    buffer = buffer[-500:]
                if flush_part.strip():
                    await send_message(flush_part.strip())

        # EOF — process stdout closed; flush remainder
        if buffer.strip():
            await send_message(buffer.strip())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"stdout reader error: {e}")
        if buffer.strip():
            try:
                await send_message(f"⚠️ stdout reader error: {e}\n\nBuffered output:\n{buffer.strip()[:3500]}")
            except Exception:
                pass

async def _stream_stderr(proc: asyncio.subprocess.Process):
    """Read stderr and forward non-empty errors on completion."""
    buffer = ""
    try:
        while True:
            chunk_bytes = await proc.stderr.read(1024)
            if not chunk_bytes:
                break
            chunk = chunk_bytes.decode("utf-8", errors="replace")
            buffer += chunk
            # Stream large stderr incrementally as well
            if len(buffer) > MAX_TELEGRAM_LEN * 2:
                to_send = buffer.strip()
                # keep tail
                await send_message(f"⚠️ stderr:\n{to_send[:MAX_TELEGRAM_LEN]}")
                buffer = buffer[-500:]
        if buffer.strip():
            # Only send stderr if process failed or stdout was empty — otherwise it's noisy
            # We send it in all cases but clearly labelled
            await send_message(f"⚠️ stderr:\n{buffer.strip()[:MAX_TELEGRAM_LEN]}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"stderr reader error: {e}")

async def _monitor_process(proc: asyncio.subprocess.Process, prompt_preview: str):
    """Wait for process exit, reset state, report result."""
    global active_process, awaiting_input, reader_tasks
    try:
        return_code = await proc.wait()
        # Give readers a moment to drain
        await asyncio.sleep(0.3)
        # Cancel lingering reader tasks
        for t in reader_tasks:
            if not t.done():
                t.cancel()
        # Small grace to let cancellation propagate
        if reader_tasks:
            await asyncio.gather(*reader_tasks, return_exceptions=True)

        # Report completion if process exited without already flushing everything
        # (stdout reader already flushed). Only report non-zero / empty cases.
        if return_code is not None and return_code != 0:
            await send_message(f"Process exited with code {return_code} (prompt: {prompt_preview[:80]!r})")
        elif return_code == 0:
            # Normal completion — readers already sent output; nothing extra needed
            pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"monitor error: {e}")
        try:
            await send_message(f"⚠️ Monitor error: {e}")
        except Exception:
            pass
    finally:
        async with state_lock:
            active_process = None
            awaiting_input = False
            reader_tasks = []
        print("State reset: ready for next prompt")

async def spawn_agent(prompt_text: str):
    """Spawn agy CLI asynchronously and attach stream readers."""
    global active_process, awaiting_input, reader_tasks

    async with state_lock:
        if active_process is not None and active_process.returncode is None:
            await send_message("⏳ Agent is still busy. Send /cancel to abort or wait for completion.")
            return
        awaiting_input = False

    cmd = build_agy_command(prompt_text)
    print(f"Spawning: {' '.join(cmd)!r}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ,
        )
    except FileNotFoundError:
        await send_message(f"⚠️ agy binary not found at `{AGY_PATH}`. Set AGY_PATH in .env")
        return
    except Exception as e:
        await send_message(f"⚠️ Failed to spawn agent: {e}")
        return

    async with state_lock:
        active_process = proc
        awaiting_input = False
        # Launch readers
        t_out = asyncio.create_task(_stream_stdout(proc), name="agy-stdout")
        t_err = asyncio.create_task(_stream_stderr(proc), name="agy-stderr")
        t_mon = asyncio.create_task(_monitor_process(proc, prompt_text), name="agy-monitor")
        reader_tasks = [t_out, t_err, t_mon]

    # Detect immediate failure due to unknown --prompt-interactive flag and retry without it
    # Give process a moment to fail fast
    await asyncio.sleep(0.6)
    if proc.returncode is not None and proc.returncode != 0:
        # Peek if flag was used and failure might be flag-related; retry once without flag
        flag = (AGY_INTERACTIVE_FLAG or "").strip()
        if flag and flag in cmd:
            # Try to read stderr quickly (already handled by reader, but check return code)
            print(f"Process failed fast with flag {flag!r}, retrying without flag...")
            # State already reset by monitor? Ensure clean
            await asyncio.sleep(0.5)
            async with state_lock:
                if active_process is None:  # monitor reset it
                    fallback_cmd = [AGY_PATH, "--dangerously-skip-permissions", "--conversation", CONVERSATION_ID, "-p", prompt_text]
                    print(f"Retrying fallback: {' '.join(fallback_cmd)!r}")
                    try:
                        proc2 = await asyncio.create_subprocess_exec(
                            *fallback_cmd,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            env=os.environ,
                        )
                        active_process = proc2
                        awaiting_input = False
                        t_out2 = asyncio.create_task(_stream_stdout(proc2), name="agy-stdout-fallback")
                        t_err2 = asyncio.create_task(_stream_stderr(proc2), name="agy-stderr-fallback")
                        t_mon2 = asyncio.create_task(_monitor_process(proc2, prompt_text), name="agy-monitor-fallback")
                        reader_tasks = [t_out2, t_err2, t_mon2]
                    except Exception as e2:
                        await send_message(f"⚠️ Fallback spawn failed: {e2}")
                else:
                    # Process still considered active, don't retry
                    pass

# ---------------------------------------------------------------------------
# Message routing — state machine (requirement #4)
# ---------------------------------------------------------------------------
async def handle_incoming_text(text: str):
    global awaiting_input, active_process

    stripped = text.strip()

    # Slash commands — always handled as control, never forwarded to stdin
    if stripped.startswith("/"):
        cmd = stripped.split()[0].lower()
        if cmd == "/start":
            await send_message("System ready. Send a prompt to begin.\n\nCommands: /status /cancel /help")
            return
        elif cmd == "/help":
            await send_message(
                "🤖 Antigravity Telegram Bridge — Help\n\n"
                "• Send any text to run the agent\n"
                "• When the agent asks `Proceed? [Y/n]` reply `y` or `n`\n"
                "• /status — show if agent is busy/waiting\n"
                "• /cancel — kill the active agent process\n"
                "• /help — this message"
            )
            return
        elif cmd == "/status":
            async with state_lock:
                if active_process is None:
                    await send_message("✅ Idle — no active agent.")
                elif awaiting_input:
                    await send_message("⏸️ Agent is waiting for your input (`y`/`n`).")
                else:
                    await send_message(f"⏳ Agent is running (PID {active_process.pid}).")
            return
        elif cmd == "/cancel":
            async with state_lock:
                proc = active_process
            if proc is None or proc.returncode is not None:
                await send_message("No active process to cancel.")
                return
            try:
                proc.terminate()
                await asyncio.sleep(1)
                if proc.returncode is None:
                    proc.kill()
                await send_message("🛑 Cancelled active agent process.")
            except ProcessLookupError:
                await send_message("Process already exited.")
            except Exception as e:
                await send_message(f"⚠️ Cancel failed: {e}")
            return
        else:
            await send_message(f"Unknown command `{cmd}`. Try /help")
            return

    # State machine routing (requirement #4)
    async with state_lock:
        proc = active_process
        waiting = awaiting_input

    if proc is not None and waiting:
        # Route directly to stdin of the live CLI process
        print(f"Routing to stdin (waiting prompt): {text!r}")
        try:
            if proc.stdin is None or proc.stdin.is_closing():
                await send_message("⚠️ Agent stdin is closed — cannot forward input. Spawning new session.")
                async with state_lock:
                    active_process = None
                    awaiting_input = False
                await spawn_agent(text)
                return
            proc.stdin.write((text + "\n").encode("utf-8"))
            await proc.stdin.drain()
            async with state_lock:
                awaiting_input = False
            await send_message("↳ Sent to agent…")
        except BrokenPipeError:
            await send_message("⚠️ Agent process closed its input. Starting new session.")
            async with state_lock:
                active_process = None
                awaiting_input = False
            await spawn_agent(text)
        except Exception as e:
            await send_message(f"⚠️ Failed to forward to agent: {e}")
        return

    if proc is not None and not waiting:
        # Agent busy but not awaiting input — don't spawn overlapping session
        await send_message("⏳ Agent is still working on the previous task. Please wait…\nSend /status to check or /cancel to abort.")
        return

    # Idle — spawn new agent turn
    await send_message("…")
    await spawn_agent(text)

# ---------------------------------------------------------------------------
# Main polling loop — robust, never crash daemon (requirement: robust error handling)
# ---------------------------------------------------------------------------
async def poll_loop():
    print("Starting Antigravity Telegram Bridge (async)...")
    print(f"Using agy binary at: {AGY_PATH}")
    print(f"Routing to conversation ID: {CONVERSATION_ID}")
    print(f"Interactive flag: {AGY_INTERACTIVE_FLAG!r}")
    try:
        await send_message("🚀 Antigravity Telegram Bridge is online. You can now chat with your local AI agent.")
    except Exception as e:
        print(f"Startup send_message failed: {e}")

    offset: int | None = None
    # Ignore old updates on startup
    try:
        init = await get_updates(offset)
        if init.get("ok") and init.get("result"):
            offset = init["result"][-1]["update_id"] + 1
    except Exception as e:
        print(f"Init offset fetch failed: {e}")

    consecutive_errors = 0
    while True:
        try:
            updates = await get_updates(offset)
            consecutive_errors = 0
            if not updates.get("ok"):
                await asyncio.sleep(2)
                continue
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg or "text" not in msg:
                    continue
                msg_chat_id = str(msg["chat"]["id"])
                text = msg["text"]
                if msg_chat_id != str(CHAT_ID):
                    print(f"Unauthorized access attempt from Chat ID: {msg_chat_id}")
                    continue
                print(f"Received from authorized user: {text!r} (awaiting_input={awaiting_input}, active={active_process is not None})")
                try:
                    await handle_incoming_text(text)
                except Exception as e:
                    print(f"handle_incoming_text error: {e}")
                    try:
                        await send_message(f"⚠️ Handler error: {e}")
                    except Exception:
                        pass
        except asyncio.CancelledError:
            print("Poll loop cancelled, shutting down...")
            raise
        except Exception as e:
            consecutive_errors += 1
            print(f"Poll loop error ({consecutive_errors}): {e}")
            # Exponential backoff, capped
            await asyncio.sleep(min(2 * consecutive_errors, 30))
            if consecutive_errors > 10:
                print("Too many consecutive errors, resetting http session...")
                global http_session
                try:
                    if http_session and not http_session.closed:
                        await http_session.close()
                except Exception:
                    pass
                http_session = aiohttp.ClientSession()
                consecutive_errors = 0

async def main():
    global http_session
    http_session = aiohttp.ClientSession()
    poll_task = asyncio.create_task(poll_loop())
    # Wait for poll_task forever; signals will cancel it via loop shutdown
    try:
        await poll_task
    except asyncio.CancelledError:
        print("Main cancelled")
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        async with state_lock:
            proc = active_process
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
            except Exception:
                pass
        # Cancel readers
        for t in list(reader_tasks):
            if not t.done():
                t.cancel()
        if reader_tasks:
            await asyncio.gather(*reader_tasks, return_exceptions=True)
        if http_session and not http_session.closed:
            await http_session.close()
        print("Bridge shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")
        sys.exit(0)
