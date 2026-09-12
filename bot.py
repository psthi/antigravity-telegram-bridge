#!/usr/bin/env python3
import urllib.request
import urllib.parse
import json
import time
import subprocess
import os
import sys
import shutil
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CONVERSATION_ID = os.getenv("AGY_CONVERSATION_ID", "telegram-bridge-session")
AGY_PATH = os.getenv("AGY_PATH") or shutil.which("agy") or "agy"

if not TOKEN or not CHAT_ID:
    print("Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in the .env file.")
    sys.exit(1)

URL = f"https://api.telegram.org/bot{TOKEN}/"

def send_message(text):
    url = URL + "sendMessage"
    data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': text}).encode('utf-8')
    req = urllib.request.Request(url, data=data)
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Failed to send message: {e}")

def get_updates(offset=None):
    url = URL + "getUpdates?timeout=30"
    if offset:
        url += f"&offset={offset}"
    try:
        response = urllib.request.urlopen(url, timeout=35)
        return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Error fetching updates: {e}")
        return {"ok": False, "result": []}

def main():
    print(f"Starting Antigravity Telegram Bridge...")
    print(f"Using agy binary at: {AGY_PATH}")
    print(f"Routing to conversation ID: {CONVERSATION_ID}")
    
    send_message("🚀 Antigravity Telegram Bridge is online. You can now chat with your local AI agent.")
    
    # Initialize offset by fetching current updates and ignoring old ones
    offset = None
    initial_updates = get_updates()
    if initial_updates.get("ok") and initial_updates.get("result"):
        offset = initial_updates["result"][-1]["update_id"] + 1

    while True:
        updates = get_updates(offset)
        if updates.get("ok") and updates.get("result"):
            for update in updates["result"]:
                offset = update["update_id"] + 1
                
                # Verify message is from the authorized user
                if "message" in update and "text" in update["message"]:
                    msg_chat_id = str(update["message"]["chat"]["id"])
                    text = update["message"]["text"]
                    
                    if msg_chat_id != CHAT_ID:
                        print(f"Unauthorized access attempt from Chat ID: {msg_chat_id}")
                        continue
                        
                    print(f"Received from authorized user: {text}")
                    
                    if text == "/start":
                        send_message("System ready. Send a prompt to begin.")
                        continue
                        
                    # Send typing indicator / thinking placeholder
                    send_message("...")
                    
                    try:
                        # Call Antigravity CLI
                        cmd = [
                            AGY_PATH, 
                            "--conversation", CONVERSATION_ID, 
                            "-p", text
                        ]
                        result = subprocess.run(cmd, capture_output=True, text=True, env=os.environ)
                        
                        response_text = result.stdout.strip()
                        if not response_text and result.stderr:
                            response_text = "⚠️ Error: " + result.stderr.strip()
                            
                        if not response_text:
                            response_text = "(Action completed silently)"
                            
                        # Telegram limits messages to 4096 characters, chunk if necessary
                        for i in range(0, len(response_text), 4000):
                            send_message(response_text[i:i+4000])
                            
                    except Exception as e:
                        send_message(f"⚠️ System Error: {e}")
                        
        time.sleep(1)

if __name__ == "__main__":
    main()
