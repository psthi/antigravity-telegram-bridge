# Antigravity Telegram Bridge 🚀

<div align="center">
  <p><strong>A lightning-fast, zero-overhead Telegram integration for your local Antigravity AI agents.</strong></p>
  <img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/Overhead-Zero-success.svg" alt="Zero Overhead">
</div>

## The Problem
Running local AI coding agents is incredibly powerful, but it traditionally ties you to your terminal. If you kick off a massive refactor or a complex research task before leaving your desk, you're left completely in the dark until you return. 

Standard remote solutions involve exposing bloated Web UIs to the internet or injecting heavy Model Context Protocol (MCP) servers into your agent's context window—eating up valuable tokens and slowing down inference.

## The Solution
**Antigravity Telegram Bridge** solves this elegantly. It is a lightweight, dependency-free (almost) Python sidecar that securely pipes your Telegram messages directly into the local `agy` CLI process, maintaining context without injecting bulky tool schemas into the AI's prompt space. 

Chat with your local AI, kick off terminal commands, and monitor long-running workflows directly from your phone. No token bloat. No exposed web ports. Pure speed.

## Features
* 🔒 **Secure Authorization**: Hardcoded to your specific Telegram Chat ID. Random users cannot access your local agent.
* 🪶 **Zero Token Bloat**: Bypasses the need for heavy MCP tool definitions. The bridge operates entirely at the CLI layer.
* 🧵 **Context Aware**: Routes all messages to a dedicated, persistent session ID so the agent remembers the ongoing conversation.
* chunking **Auto-Chunking**: Telegram's 4096-character limit is bypassed seamlessly via built-in response chunking.

---

## Installation & Setup

1. **Clone the repo**
   ```bash
   git clone https://github.com/yourusername/antigravity-telegram-bridge.git
   cd antigravity-telegram-bridge
   ```

2. **Install requirements**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the environment**
   ```bash
   cp .env.example .env
   ```
   Open `.env` and fill in your credentials:
   * `TELEGRAM_BOT_TOKEN`: Get this from [@BotFather](https://t.me/botfather) on Telegram.
   * `TELEGRAM_CHAT_ID`: Send a message to your new bot, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` to grab your unique Chat ID.

## Running the Bridge

You can run it directly in your terminal:
```bash
python3 bot.py
```

### Running as a Background Daemon (Recommended)
To keep the bot running permanently in the background (even if you close your terminal), we recommend using `systemd`:

```bash
# Create the service and run it in the background
systemd-run --user --unit=antigravity-telegram python3 $(pwd)/bot.py

# To check the logs:
journalctl --user -u antigravity-telegram -f

# To stop the bot:
systemctl --user stop antigravity-telegram
```

## How it Works
When a message arrives from your phone, the script calls `agy --conversation <id> -p "<text>"`. The output is synchronously captured and relayed back to the Telegram API.

## Tags
`antigravity` `ai-agent` `telegram-bot` `cli-bridge` `local-ai` `automation` `llm`
