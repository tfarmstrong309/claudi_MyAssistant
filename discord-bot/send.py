#!/usr/bin/env python3
"""
Usage:
  send.py "message"                    → sends to #general
  send.py --channel updates "msg"      → sends to #updates
  send.py --channel logs "msg"         → sends to #logs
  send.py --channel tasks "msg"        → sends to #tasks
  send.py --channel agenthunt "msg"    → sends to #agenthunt

Valid channels: general, updates, logs, tasks, agenthunt
"""
import sys
import pathlib

VALID_CHANNELS = {"general", "updates", "logs", "tasks", "agenthunt"}
OUTBOX = pathlib.Path.home() / "discord-bot" / "outbox.queue"

args = sys.argv[1:]
channel = "general"

if "--channel" in args:
    idx = args.index("--channel")
    channel = args[idx + 1]
    args = args[:idx] + args[idx + 2:]
    if channel not in VALID_CHANNELS:
        print(f"Unknown channel '{channel}'. Valid: {', '.join(VALID_CHANNELS)}")
        sys.exit(1)

if not args:
    print("Usage: send.py [--channel <name>] \"message\"")
    sys.exit(1)

message = " ".join(args)
with open(OUTBOX, "a") as f:
    f.write(f"{channel}|{message}\n")

print(f"Queued to #{channel}: {message}")
