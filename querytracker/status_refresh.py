#!/usr/bin/env python3
"""
Refresh open/closed status and query stats from QueryTracker for stored agents.

Faster than a full profile rescrape — navigates to each agent page but only
extracts the 6 status fields (open_closed, reply_rate, request_rate,
subs_reply_rate, last_reply, last_request). Leaves all enrichment columns
(mswl_text, pm_bio, agency_bio, summaries, etc.) untouched.

Usage:
  status_refresh.py                      top 100 agents by score DESC
  status_refresh.py --limit N            cap count
  status_refresh.py --open-only          only agents currently marked open
  status_refresh.py --agent /agent/NNN   single agent by QT path
  status_refresh.py --name "Smith"       single agent by name
  status_refresh.py --file path.txt      agents from file (one /agent/NNN per line)
"""
import asyncio
import pathlib
import sqlite3
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from common import parse_args, resolve_agents
from qt import get_context, BASE_URL

DB_PATH = pathlib.Path.home() / "querytracker" / "agents.db"

# Stat keys to extract — same patterns as qt._scrape_profile_basics()
_STAT_KEYS = [
    ("query reply rate:", "reply_rate"),
    ("request rate:",     "request_rate"),
    ("subs reply rate:",  "subs_reply_rate"),
    ("last reply:",       "last_reply"),
    ("last request:",     "last_request"),
]


async def scrape_status_quick(page, qt_path: str) -> dict:
    """
    Navigate to agent's QT page and extract the 6 status fields only.
    Returns dict with: open_closed (int 0/1), reply_rate, request_rate,
    subs_reply_rate, last_reply, last_request.
    """
    url = f"{BASE_URL}{qt_path}"
    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    body  = await page.inner_text("body")
    lines = [l.strip() for l in body.splitlines() if l.strip()]

    stats = {}
    for i, line in enumerate(lines):
        ll = line.lower()
        for key, field in _STAT_KEYS:
            if field not in stats and key in ll and i + 1 < len(lines):
                stats[field] = lines[i + 1]

    open_closed = 0 if "Closed to Unsolicited" in body else 1

    return {
        "open_closed":     open_closed,
        "reply_rate":      stats.get("reply_rate", ""),
        "request_rate":    stats.get("request_rate", ""),
        "subs_reply_rate": stats.get("subs_reply_rate", ""),
        "last_reply":      stats.get("last_reply", ""),
        "last_request":    stats.get("last_request", ""),
    }


def _parse_rate(val: str) -> float | None:
    """Convert '65.3%' or '65.3' to float, or return None."""
    if not val:
        return None
    try:
        return float(val.strip().rstrip("%"))
    except ValueError:
        return None


def update_status(qt_path: str, data: dict):
    """Partial UPDATE — only the 6 status fields."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """UPDATE agents SET
            open_closed     = ?,
            reply_rate      = ?,
            request_rate    = ?,
            subs_reply_rate = ?,
            last_reply      = ?,
            last_request    = ?
        WHERE qt_path = ?""",
        (
            data["open_closed"],
            _parse_rate(data["reply_rate"]),
            _parse_rate(data["request_rate"]),
            _parse_rate(data["subs_reply_rate"]),
            data["last_reply"] or None,
            data["last_request"] or None,
            qt_path,
        )
    )
    con.commit()
    con.close()


async def main():
    parsed    = parse_args(sys.argv[1:])
    open_only = "--open-only" in parsed["extra"]

    # Resolve agents list
    if any([parsed["qt_path"], parsed["name"], parsed["file_path"]]):
        agents = resolve_agents(
            qt_path=parsed["qt_path"],
            name=parsed["name"],
            file_path=parsed["file_path"],
            extra_cols="open_closed",
        )
    else:
        # Default: top agents by score, optionally filtered to open only
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        where = "WHERE open_closed = 1" if open_only else ""
        limit_clause = f"LIMIT {parsed['limit']}" if parsed["limit"] else "LIMIT 100"
        rows = con.execute(
            f"SELECT qt_path, name, agency, open_closed FROM agents "
            f"{where} ORDER BY total_score DESC NULLS LAST, name {limit_clause}"
        ).fetchall()
        con.close()
        agents = [dict(r) for r in rows]
        if not agents:
            print("No agents in DB.")
            return

    if parsed["limit"] and any([parsed["qt_path"], parsed["name"], parsed["file_path"]]):
        agents = agents[:parsed["limit"]]

    total = len(agents)
    print(f"Refreshing status for {total} agent(s)…")

    async with async_playwright() as p:
        browser, _, page = await get_context(p)

        changed = 0
        for i, agent in enumerate(agents, 1):
            try:
                data = await scrape_status_quick(page, agent["qt_path"])
                new_status  = data["open_closed"]
                old_status  = 1 if agent.get("open_closed") else 0
                new_label   = "OPEN"   if new_status else "CLOSED"
                old_label   = "OPEN"   if old_status else "CLOSED"

                update_status(agent["qt_path"], data)

                if new_status != old_status:
                    changed += 1
                    flag = f"  *** CHANGED: {old_label} → {new_label}"
                else:
                    flag = ""
                print(f"[{i:3}/{total}] {agent['name']:<30} {new_label}{flag}")

            except Exception as e:
                print(f"[{i:3}/{total}] {agent['name']:<30} ERROR: {e}")

        await browser.close()

    print(f"\nDone. {changed} status change(s) out of {total} agents.")


if __name__ == "__main__":
    asyncio.run(main())
