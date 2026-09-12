# Interactive Telegram Bridge (Delegation to OpenCode)

**Project Name**: Interactive Telegram Bridge
**Target Agent**: opencode
**Target Model**: Muse Spark 1.2 Free
**Status**: Pending

## Overview
The goal of this project is to rewrite the existing `bot.py` Antigravity Telegram bridge into a fully interactive, asynchronous script. The current script handles synchronous `run_command` executions but hangs when encountering interactive `[Y/n]` permission prompts from the Antigravity CLI.

## Requirements
1. **Asynchronous Framework**: Transition from the blocking `urllib` to an async library (`asyncio` + `aiohttp` or `python-telegram-bot`) to handle Telegram polling and CLI output concurrently.
2. **Subprocess Management**: Use `asyncio.create_subprocess_exec` or `subprocess.Popen` to spawn the Antigravity CLI and read `stdout` and `stderr` streams continuously without blocking.
3. **Interactive Prompt Detection**: Continuously parse the CLI's `stdout`. When it detects a permission prompt (e.g., `Proceed? [Y/n]` or `[y/N]`), flush the buffer to Telegram and pause execution, keeping the CLI process alive.
4. **State Machine / Routing**: Implement a state tracker. If the CLI is waiting for user input, the next message received from Telegram must be written directly to the active process's `stdin` (e.g., passing `"y\n"`) rather than spawning a new prompt.
5. **No Context Bloat**: Do not use MCP. Keep it as a pure CLI bridge using `agy --prompt-interactive` or equivalent.

## Current Script Reference
The existing 50-line blocking script is located at:
`~/antigravity-telegram-bridge/bot.py`

## Instructions for OpenCode
Please write a new Python script that satisfies all the requirements above. Use modern Python 3 async patterns and ensure robust error handling so the background daemon does not crash.
