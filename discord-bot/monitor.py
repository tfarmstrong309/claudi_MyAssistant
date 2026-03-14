#!/usr/bin/env python3
"""
Runs every minute via systemd timer.
- Checks inbox.log for new messages since last check
- Sends an auto-acknowledgement via the bot to #general
- Appends new messages to pending.log for review
- Logs activity to #logs
"""
import pathlib
import subprocess
from datetime import datetime

BASE = pathlib.Path.home() / "discord-bot"
INBOX = BASE / "inbox.log"
PENDING = BASE / "pending.log"
MARKER = BASE / ".last_read_line"

def get_last_read():
    if MARKER.exists():
        try:
            return int(MARKER.read_text().strip())
        except ValueError:
            pass
    return 0

def set_last_read(n):
    MARKER.write_text(str(n))

def send_message(msg, channel="general"):
    subprocess.run(
        ["python3", str(BASE / "send.py"), "--channel", channel, msg],
        capture_output=True
    )

def main():
    if not INBOX.exists():
        return

    lines = INBOX.read_text().splitlines()
    last_read = get_last_read()
    new_lines = lines[last_read:]

    if not new_lines:
        return

    # Filter to only messages (not empty lines)
    new_messages = [l for l in new_lines if l.strip()]

    if not new_messages:
        set_last_read(len(lines))
        return

    # Append new messages to pending log
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(PENDING, "a") as f:
        f.write(f"\n--- Received at {timestamp} ---\n")
        for msg in new_messages:
            f.write(msg + "\n")

    # Send a single acknowledgement to #general
    count = len(new_messages)
    if count == 1:
        ack = "Message received. I'll get back to you shortly."
    else:
        ack = f"{count} messages received. I'll get back to you shortly."

    send_message(ack, "general")

    # Log activity to #logs
    send_message(f"[monitor] {count} new message(s) processed at {timestamp}", "logs")

    set_last_read(len(lines))
    print(f"[{timestamp}] Processed {count} new message(s), acknowledgement sent.")

if __name__ == "__main__":
    main()
