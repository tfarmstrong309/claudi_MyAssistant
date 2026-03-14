#!/usr/bin/env python3
"""
Shared utilities for the querytracker scraper suite.

Import-only — no CLI entry point.

Provides:
  get_api_key()          → str
  resolve_agents(...)    → list[dict]
  parse_args(argv)       → dict
  clean_scraped_text(..) → str
  plain_browser_context  → async (browser, context, page)
  discord_chunks(...)    → list[str]
"""
import os
import pathlib
import sqlite3
import sys

# ── Shared paths / constants ───────────────────────────────────────────────────

DB_PATH    = pathlib.Path.home() / "querytracker" / "agents.db"
KEY_FILE   = pathlib.Path.home() / ".anthropic_api_key"
QT_CREDS   = pathlib.Path.home() / ".querytracker_creds"
PM_CREDS   = pathlib.Path.home() / ".pm_creds"
PM_SESSION = pathlib.Path.home() / ".pm_session.json"

_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ── API key ────────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    """Return Anthropic API key from ~/.anthropic_api_key or ANTHROPIC_API_KEY env var."""
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    raise SystemExit(
        f"ERROR: Anthropic API key not found.\n"
        f"Store it in {KEY_FILE} or set ANTHROPIC_API_KEY env var."
    )


# ── DB agent lookup ────────────────────────────────────────────────────────────

def resolve_agents(
    qt_path: str | None = None,
    name: str | None = None,
    all_open: bool = False,
    file_path: str | None = None,
    extra_cols: str = "",
) -> list[dict]:
    """
    Look up agents in agents.db and return a list of dicts.

    Always includes: qt_path, name, agency.
    extra_cols: additional comma-separated column names (e.g. "website, pm_url").

    Sources (mutually exclusive, checked in order):
      file_path → load /agent/NNN paths from a text file (one per line, # = comment)
      qt_path   → single agent by exact qt_path
      name      → LIKE match on name column
      all_open  → all agents where open_closed = 1

    Prints an error and calls sys.exit(0) if no agents are found.
    """
    cols = "qt_path, name, agency"
    if extra_cols:
        cols += ", " + extra_cols.strip().strip(",")

    if file_path:
        fpath = pathlib.Path(file_path).expanduser()
        paths = [
            ln.strip() for ln in fpath.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
        if not paths:
            print(f"No agent paths found in {fpath}")
            sys.exit(0)
        con = _db_connect()
        placeholders = ",".join("?" * len(paths))
        rows = con.execute(
            f"SELECT {cols} FROM agents WHERE qt_path IN ({placeholders})",
            paths
        ).fetchall()
        con.close()
    else:
        con = _db_connect()
        if qt_path:
            rows = con.execute(
                f"SELECT {cols} FROM agents WHERE qt_path = ?", (qt_path,)
            ).fetchall()
        elif name:
            rows = con.execute(
                f"SELECT {cols} FROM agents WHERE name LIKE ?", (f"%{name}%",)
            ).fetchall()
        elif all_open:
            rows = con.execute(
                f"SELECT {cols} FROM agents WHERE open_closed = 1"
            ).fetchall()
        else:
            rows = []
        con.close()

    agents = [dict(r) for r in rows]
    if not agents:
        print("No matching agents found in DB. Run qt.py --profiles first.")
        sys.exit(0)
    return agents


def _db_connect():
    """Open agents.db with row_factory set."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ── CLI argument parsing ───────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> dict:
    """
    Parse the standard flags shared across all scraper scripts.

    Returns a dict with keys:
      qt_path   (str | None)   — value of --agent
      name      (str | None)   — value of --name
      all_open  (bool)         — --all-open flag
      limit     (int | None)   — value of --limit
      force     (bool)         — --force flag
      dry_run   (bool)         — --dry-run flag
      file_path (str | None)   — value of --file
      extra     (list[str])    — unrecognised tokens for script-specific handling

    Scripts process 'extra' for their own flags (e.g. --profiles, --discord,
    --categories, --years, --tier, --format, --open-only, etc.)
    """
    result: dict = {
        "qt_path":   None,
        "name":      None,
        "all_open":  False,
        "limit":     None,
        "force":     False,
        "dry_run":   False,
        "file_path": None,
        "extra":     [],
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--agent" and i + 1 < len(argv):
            result["qt_path"] = argv[i + 1]; i += 2
        elif a == "--name" and i + 1 < len(argv):
            result["name"] = argv[i + 1]; i += 2
        elif a == "--all-open":
            result["all_open"] = True; i += 1
        elif a == "--limit" and i + 1 < len(argv):
            result["limit"] = int(argv[i + 1]); i += 2
        elif a == "--force":
            result["force"] = True; i += 1
        elif a == "--dry-run":
            result["dry_run"] = True; i += 1
        elif a == "--file" and i + 1 < len(argv):
            result["file_path"] = argv[i + 1]; i += 2
        else:
            result["extra"].append(a); i += 1
    return result


# ── Text cleanup ───────────────────────────────────────────────────────────────

_SKIP_BASE = [
    "cookie", "privacy policy", "© ", "all rights reserved",
    "newsletter", "subscribe", "javascript", "skip to",
]

def clean_scraped_text(
    raw: str,
    max_lines: int = 100,
    min_len: int = 15,
    extra_skip: list[str] | None = None,
) -> str:
    """
    Strip boilerplate nav/footer lines from scraped page text.

    Filters out:
    - Empty lines or lines shorter than min_len characters
    - Lines containing any pattern from the base skip list + extra_skip

    Returns up to max_lines of meaningful content joined by newlines.

    Replaces agent_website.clean_bio() and mswl_lookup.clean_mswl()
    (structurally identical; only defaults and skip patterns differ).
    """
    skip = _SKIP_BASE + (extra_skip or [])
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or len(line) < min_len:
            continue
        lower = line.lower()
        if any(p in lower for p in skip):
            continue
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


# ── Anonymous Playwright browser ───────────────────────────────────────────────

async def plain_browser_context(playwright, headless: bool = True):
    """
    Launch a headless Chromium browser for public sites (no QT auth).
    Returns (browser, context, page).

    Replaces identical boilerplate in mswl_lookup, mswl_search, agent_website.

    Note: the QT-authenticated get_context() lives in qt.py — it owns QT
    session state and is imported directly from there by discover_agents.py.
    """
    browser = await playwright.chromium.launch(headless=headless)
    context = await browser.new_context(user_agent=_CHROME_UA)
    page    = await context.new_page()
    return browser, context, page


# ── Discord text chunker ───────────────────────────────────────────────────────

def discord_chunks(
    text: str,
    limit: int = 1900,
    code_block: bool = False,
) -> list[str]:
    """
    Split text into Discord-safe chunks at newline boundaries.

    If code_block=True, wraps each chunk in triple-backtick fences.

    Replaces 4 different while-loops in bot.py that had inconsistent limits
    (1850 vs 1900) and an rfind-or-fallback bug (rfind returning 0 is falsy).
    """
    if not text:
        return []

    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:          # no newline found, or newline at pos 0
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    if text:
        chunks.append(text)

    if code_block:
        return [f"```\n{c}\n```" for c in chunks]
    return chunks
