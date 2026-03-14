#!/usr/bin/env python3
"""
Usage: python3 read.py         → show all messages
       python3 read.py -n 10   → show last N messages
       python3 read.py --clear → clear inbox after reading
"""
import sys
import pathlib

INBOX = pathlib.Path.home() / "discord-bot" / "inbox.log"

args = sys.argv[1:]
clear = "--clear" in args
n = None
if "-n" in args:
    idx = args.index("-n")
    n = int(args[idx + 1])

lines = INBOX.read_text().splitlines() if INBOX.exists() else []
if n:
    lines = lines[-n:]

if not lines:
    print("(no messages)")
else:
    for line in lines:
        print(line)

if clear:
    INBOX.write_text("")
    print("\n(inbox cleared)")
