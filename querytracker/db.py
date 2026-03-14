#!/usr/bin/env python3
"""
Agent tracking database — SQLite backend with natural-language querying via Claude API.

Usage (CLI):
  db.py agents [--open] [--genre X] [--name X]     list scraped agents
  db.py submissions [--pending] [--agent /agent/ID] list query submissions
  db.py submit /agent/7674 [--date YYYY-MM-DD] [--method QueryManager] [--notes "..."]
  db.py status /agent/7674 rejected [--date YYYY-MM-DD] [--notes "..."]
  db.py note /agent/7674 "note text" [--tags "fit,historical"]
  db.py history /agent/7674                         full timeline for one agent
  db.py query "SELECT ..."                          run raw SQL
  db.py ask "which open agents in Historical haven't been queried?"  (requires API key)
  db.py score [--force]                             score all agents (--force rescores)
  db.py rank [--limit N]                            show ranked agents (firm rank 1-3)
  db.py keywords [get | set <text>]                 manage keyword list
  db.py para [get | set <text>]                     manage query paragraph
  db.py boilerplate [field [value]]                 manage submission form answers
  db.py export [--format csv|json] [--open] [--output path]  export agents to file
"""
import sqlite3
import pathlib
import sys
import os
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from common import get_api_key as _get_api_key  # noqa: E402

DB_PATH  = pathlib.Path.home() / "querytracker" / "agents.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    qt_path          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    agency           TEXT,
    open_closed      BOOLEAN,  -- 1=Open, 0=Closed
    query_method     TEXT,
    reply_rate       REAL,     -- percentage, e.g. 65.0
    request_rate     REAL,
    subs_reply_rate  REAL,
    last_reply       DATETIME,
    last_request     DATETIME,
    avg_request_time INTEGER,  -- days
    avg_reject_time  INTEGER,  -- days
    email            TEXT,
    website          TEXT,
    twitter          TEXT,
    genres           TEXT,
    mswl_file        TEXT,
    last_scraped     TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    qt_path       TEXT NOT NULL REFERENCES agents(qt_path),
    submitted     TEXT NOT NULL,
    method        TEXT,
    status        TEXT DEFAULT 'pending',
    response_date TEXT,
    response_days INTEGER,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS agent_notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    qt_path   TEXT NOT NULL REFERENCES agents(qt_path),
    created   TEXT NOT NULL,
    note      TEXT NOT NULL,
    tags      TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boilerplate (
    field   TEXT PRIMARY KEY,
    value   TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL DEFAULT ''
);
"""

# System prompt used when translating natural language → SQL
_SQL_SYSTEM = """\
You are a SQL query generator for a literary agent tracking SQLite database.
Given the schema below and a natural-language question, return ONLY a valid SQLite
SELECT statement — no explanation, no markdown fences, no semicolons at the end.

Schema:
""" + SCHEMA


# ── Database helpers ────────────────────────────────────────────────────────

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    con.commit()
    # Migrate: add new columns as the schema evolves
    for col_def in [
        "agency_bio_url TEXT", "agency_bio TEXT", "agency_page_scraped TEXT",
        "mswl_url TEXT", "mswl_text TEXT", "mswl_scraped TEXT",
        "agent_summary TEXT", "agent_summary_generated TEXT",
        "keyword_score    REAL",
        "para_score       REAL",
        "total_score      REAL",
        "matched_keywords TEXT",    # JSON array of matched keywords
        "para_reasoning   TEXT",    # Claude's 1-sentence explanation
        "scored_at        TEXT",
        "firm_rank        INTEGER", # 1/2/3 = eligible, NULL = blocked
        "clients_json          TEXT",    # JSON array of {name, amazon_url}
        "report_12mo           TEXT",    # raw 12-month queries-sent/replied report
        "queries_sent_12mo     INTEGER", # total queries sent in last 12 months
        "full_requests_12mo    INTEGER", # full manuscript requests in last 12 months
        "partial_requests_12mo INTEGER", # partial manuscript requests in last 12 months
        "rejections_12mo       INTEGER", # rejections in last 12 months
        "query_link      TEXT",     # direct QT query submission link
        "genre_counts    TEXT",     # map {genre: count} from Data Explorer (requests only)
        "wordcount_counts TEXT",    # map {wordcount_range: count} from Data Explorer
        "data_scraped    DATETIME", # when Data Explorer was last scraped
        "pm_url          TEXT",     # Publishers Marketplace profile URL
        "pm_bio          TEXT",     # bio text from PM profile
        "pm_deals        TEXT",     # JSON array of recent deals from PM
        "pm_scraped      TEXT",     # when PM was last scraped
        "agent_not_interested TEXT",       # extracted list of things agent doesn't want
        "not_interested_extracted TEXT",   # timestamp of last extraction
        "web_snippets    TEXT",     # JSON array [{url, title, text}] from DDG search
        "web_scraped     TEXT",     # when web search was last run
    ]:
        try:
            con.execute(f"ALTER TABLE agents ADD COLUMN {col_def}")
            con.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    # Drop old _json-suffixed columns superseded by properly-named ones (SQLite 3.35+)
    for old_col in ["genre_counts_json", "wordcount_counts_json"]:
        try:
            con.execute(f"ALTER TABLE agents DROP COLUMN {old_col}")
            con.commit()
        except sqlite3.OperationalError:
            pass  # already dropped or column never existed

    # Type-change migrations: convert TEXT columns to proper typed columns.
    # For each: add a _new temp column, convert existing data, drop old, rename.
    # Skipped automatically if column already has the target type.
    _col_types = {
        row[1]: row[2].upper()
        for row in con.execute("PRAGMA table_info(agents)").fetchall()
    }
    _type_migrations = [
        ("open_closed",      "BOOLEAN",
         "CASE WHEN open_closed='Open' THEN 1 WHEN open_closed='Closed' THEN 0 ELSE NULL END"),
        ("reply_rate",       "REAL",
         "CASE WHEN reply_rate IS NULL OR TRIM(reply_rate) IN ('','n/a') THEN NULL "
         "ELSE CAST(TRIM(SUBSTR(reply_rate,1,INSTR(reply_rate||'%','%')-1)) AS REAL) END"),
        ("request_rate",     "REAL",
         "CASE WHEN request_rate IS NULL OR TRIM(request_rate) IN ('','n/a') THEN NULL "
         "ELSE CAST(TRIM(SUBSTR(request_rate,1,INSTR(request_rate||'%','%')-1)) AS REAL) END"),
        ("subs_reply_rate",  "REAL",
         "CASE WHEN subs_reply_rate IS NULL OR TRIM(subs_reply_rate) IN ('','n/a') THEN NULL "
         "ELSE CAST(TRIM(SUBSTR(subs_reply_rate,1,INSTR(subs_reply_rate||'%','%')-1)) AS REAL) END"),
        ("last_reply",       "DATETIME",
         "CASE WHEN last_reply LIKE '__/__/____' "
         "THEN SUBSTR(last_reply,7,4)||'-'||SUBSTR(last_reply,1,2)||'-'||SUBSTR(last_reply,4,2) "
         "ELSE last_reply END"),
        ("last_request",     "DATETIME",
         "CASE WHEN last_request LIKE '__/__/____' "
         "THEN SUBSTR(last_request,7,4)||'-'||SUBSTR(last_request,1,2)||'-'||SUBSTR(last_request,4,2) "
         "ELSE last_request END"),
        ("avg_request_time", "INTEGER",
         "CASE WHEN avg_request_time IS NULL OR TRIM(avg_request_time) IN ('','n/a') THEN NULL "
         "ELSE CAST(TRIM(SUBSTR(avg_request_time,1,INSTR(avg_request_time||' ',' ')-1)) AS INTEGER) END"),
        ("avg_reject_time",  "INTEGER",
         "CASE WHEN avg_reject_time IS NULL OR TRIM(avg_reject_time) IN ('','n/a') THEN NULL "
         "ELSE CAST(TRIM(SUBSTR(avg_reject_time,1,INSTR(avg_reject_time||' ',' ')-1)) AS INTEGER) END"),
    ]
    for col, target_type, sql_expr in _type_migrations:
        if _col_types.get(col, "") == target_type:
            continue  # already migrated
        tmp = f"_{col}_typed"
        try:
            con.execute(f"ALTER TABLE agents ADD COLUMN {tmp} {target_type}")
            con.execute(f"UPDATE agents SET {tmp} = {sql_expr}")
            con.commit()
            con.execute(f"ALTER TABLE agents DROP COLUMN {col}")
            con.commit()
            con.execute(f"ALTER TABLE agents RENAME COLUMN {tmp} TO {col}")
            con.commit()
        except sqlite3.OperationalError:
            # Clean up temp column if something went wrong mid-migration
            try:
                con.execute(f"ALTER TABLE agents DROP COLUMN {tmp}")
                con.commit()
            except sqlite3.OperationalError:
                pass

    # Seed settings and boilerplate with defaults (safe to run every time)
    con.execute("INSERT OR IGNORE INTO settings (key, value, updated) VALUES (?, ?, '')", ("keywords", ""))
    con.execute("INSERT OR IGNORE INTO settings (key, value, updated) VALUES (?, ?, '')", ("query_paragraph", ""))
    for _field in ["synopsis", "bio", "comps", "hook", "wordcount", "genre", "series", "publications"]:
        con.execute("INSERT OR IGNORE INTO boilerplate (field, value, updated) VALUES (?, ?, '')", (_field, ""))
    con.commit()

    return con


# ── Field normalization helpers ───────────────────────────────────────────────

def _norm_open_closed(val):
    """'Open'→1, 'Closed'→0, anything else→None."""
    if val is None:
        return None
    v = str(val).strip().lower()
    if v == "open":
        return 1
    if v in ("closed", "0"):
        return 0
    if v == "1":
        return 1
    return None


def _norm_pct(val):
    """'65%', '65% (n=10)', '0.2%' → float. 'n/a'/None → None."""
    if not val:
        return None
    s = str(val).strip()
    if s.lower() in ("n/a", ""):
        return None
    try:
        return float(s.split("%")[0].strip())
    except (ValueError, IndexError):
        return None


def _norm_date_qt(val):
    """MM/DD/YYYY → 'YYYY-MM-DD'. ISO dates pass through. 'n/a'/None → None."""
    if not val:
        return None
    s = str(val).strip()
    if s.lower() in ("n/a", ""):
        return None
    # MM/DD/YYYY (QT format)
    if len(s) == 10 and s[2] == "/" and s[5] == "/":
        return f"{s[6:10]}-{s[0:2]}-{s[3:5]}"
    return s  # already ISO or unknown format — pass through


def _norm_days(val):
    """'76 days', '7 day' → int. 'n/a'/None → None."""
    if not val:
        return None
    s = str(val).strip()
    if s.lower() in ("n/a", ""):
        return None
    try:
        return int(s.split()[0])
    except (ValueError, IndexError):
        return None


def upsert_agent(data: dict):
    """Insert or update an agent from a qt.py profile dict. Safe to call from qt.py."""
    con = connect()
    con.execute("""
        INSERT INTO agents
            (qt_path, name, agency, open_closed, query_method,
             reply_rate, request_rate, subs_reply_rate,
             last_reply, last_request, avg_request_time, avg_reject_time,
             email, website, twitter, genres, mswl_file, last_scraped,
             agency_bio_url, agency_bio, agency_page_scraped,
             clients_json, report_12mo,
             queries_sent_12mo, full_requests_12mo, partial_requests_12mo, rejections_12mo,
             query_link, genre_counts, wordcount_counts, data_scraped)
        VALUES
            (:qt_path, :name, :agency, :open_closed, :query_method,
             :reply_rate, :request_rate, :subs_reply_rate,
             :last_reply, :last_request, :avg_request_time, :avg_reject_time,
             :email, :website, :twitter, :genres, :mswl_file, :last_scraped,
             :agency_bio_url, :agency_bio, :agency_page_scraped,
             :clients_json, :report_12mo,
             :queries_sent_12mo, :full_requests_12mo, :partial_requests_12mo, :rejections_12mo,
             :query_link, :genre_counts, :wordcount_counts, :data_scraped)
        ON CONFLICT(qt_path) DO UPDATE SET
            name                    = excluded.name,
            agency                  = CASE WHEN excluded.agency IS NOT NULL AND excluded.agency != '' THEN excluded.agency ELSE agency END,
            open_closed             = excluded.open_closed,
            query_method            = excluded.query_method,
            reply_rate              = excluded.reply_rate,
            request_rate            = excluded.request_rate,
            subs_reply_rate         = excluded.subs_reply_rate,
            last_reply              = excluded.last_reply,
            last_request            = excluded.last_request,
            avg_request_time        = excluded.avg_request_time,
            avg_reject_time         = excluded.avg_reject_time,
            email                   = excluded.email,
            website                 = excluded.website,
            twitter                 = excluded.twitter,
            genres                  = excluded.genres,
            mswl_file               = excluded.mswl_file,
            last_scraped            = excluded.last_scraped,
            agency_bio_url          = COALESCE(excluded.agency_bio_url, agency_bio_url),
            agency_bio              = COALESCE(excluded.agency_bio, agency_bio),
            agency_page_scraped     = COALESCE(excluded.agency_page_scraped, agency_page_scraped),
            clients_json            = COALESCE(excluded.clients_json, clients_json),
            report_12mo             = COALESCE(excluded.report_12mo, report_12mo),
            queries_sent_12mo       = COALESCE(excluded.queries_sent_12mo, queries_sent_12mo),
            full_requests_12mo      = COALESCE(excluded.full_requests_12mo, full_requests_12mo),
            partial_requests_12mo   = COALESCE(excluded.partial_requests_12mo, partial_requests_12mo),
            rejections_12mo         = COALESCE(excluded.rejections_12mo, rejections_12mo),
            query_link              = COALESCE(excluded.query_link, query_link),
            genre_counts     = COALESCE(excluded.genre_counts, genre_counts),
            wordcount_counts = COALESCE(excluded.wordcount_counts, wordcount_counts),
            data_scraped     = COALESCE(excluded.data_scraped, data_scraped)
    """, {
        "qt_path":               data.get("qt_path", ""),
        "name":                  data.get("name", ""),
        "agency":                data.get("agency", ""),
        "open_closed":           _norm_open_closed(data.get("open_closed")),
        "query_method":          data.get("query_method", ""),
        "reply_rate":            _norm_pct(data.get("reply_rate")),
        "request_rate":          _norm_pct(data.get("request_rate")),
        "subs_reply_rate":       _norm_pct(data.get("subs_reply_rate")),
        "last_reply":            _norm_date_qt(data.get("last_reply")),
        "last_request":          _norm_date_qt(data.get("last_request")),
        "avg_request_time":      _norm_days(data.get("avg_request_time")),
        "avg_reject_time":       _norm_days(data.get("avg_reject_time")),
        "email":                 data.get("email", ""),
        "website":               data.get("website", ""),
        "twitter":               data.get("twitter", ""),
        "genres":                data.get("genres", ""),
        "mswl_file":             data.get("mswl_file", ""),
        "last_scraped":          datetime.now().isoformat(timespec="seconds"),
        "agency_bio_url":        data.get("agency_bio_url") or None,
        "agency_bio":            data.get("agency_bio") or None,
        "agency_page_scraped":   data.get("agency_page_scraped") or None,
        "clients_json":          data.get("clients_json") or None,
        "report_12mo":           data.get("report_12mo") or None,
        "queries_sent_12mo":     data.get("queries_sent_12mo") or None,
        "full_requests_12mo":    data.get("full_requests_12mo") or None,
        "partial_requests_12mo": data.get("partial_requests_12mo") or None,
        "rejections_12mo":       data.get("rejections_12mo") or None,
        "query_link":            data.get("query_link") or None,
        "genre_counts":     data.get("genre_counts") or None,
        "wordcount_counts": data.get("wordcount_counts") or None,
        "data_scraped":     data.get("data_scraped") or None,
    })
    con.commit()
    con.close()


def upsert_agency_bio(qt_path: str, bio_url: str, bio: str):
    """Partial update — store agency website bio for an existing agent."""
    con = connect()
    con.execute("""
        UPDATE agents
        SET agency_bio_url = ?, agency_bio = ?, agency_page_scraped = ?
        WHERE qt_path = ?
    """, (bio_url, bio, datetime.now().isoformat(timespec="seconds"), qt_path))
    con.commit()
    con.close()


def upsert_mswl(qt_path: str, mswl_url: str, mswl_text: str):
    """Partial update — store Manuscript Wishlist data for an existing agent."""
    con = connect()
    con.execute("""
        UPDATE agents
        SET mswl_url = ?, mswl_text = ?, mswl_scraped = ?
        WHERE qt_path = ?
    """, (mswl_url, mswl_text, datetime.now().isoformat(timespec="seconds"), qt_path))
    con.commit()
    con.close()


def upsert_pm(qt_path: str, pm_url: str, pm_bio: str, pm_deals: str):
    """Partial update — store Publishers Marketplace profile data for an existing agent."""
    con = connect()
    con.execute("""
        UPDATE agents
        SET pm_url = ?, pm_bio = ?, pm_deals = ?, pm_scraped = ?
        WHERE qt_path = ?
    """, (pm_url, pm_bio, pm_deals, datetime.now().isoformat(timespec="seconds"), qt_path))
    con.commit()
    con.close()


def upsert_summary(qt_path: str, summary: str):
    """Partial update — store AI-generated executive summary for an existing agent."""
    con = connect()
    con.execute("""
        UPDATE agents
        SET agent_summary = ?, agent_summary_generated = ?
        WHERE qt_path = ?
    """, (summary, datetime.now().isoformat(timespec="seconds"), qt_path))
    con.commit()
    con.close()


# ── Settings & boilerplate helpers ──────────────────────────────────────────

def get_setting(key: str) -> str:
    """Return the value of a settings key, or '' if not set."""
    con = connect()
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    con.close()
    return row["value"] if row else ""


def set_setting(key: str, value: str):
    """Upsert a setting key/value."""
    con = connect()
    con.execute(
        "INSERT INTO settings (key, value, updated) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
        (key, value, datetime.now().isoformat(timespec="seconds"))
    )
    con.commit()
    con.close()


def get_boilerplate(field: str) -> str:
    """Return the stored answer for a boilerplate field, or '' if empty."""
    con = connect()
    row = con.execute("SELECT value FROM boilerplate WHERE field = ?", (field,)).fetchone()
    con.close()
    return row["value"] if row else ""


def set_boilerplate(field: str, value: str):
    """Upsert a boilerplate field answer."""
    con = connect()
    con.execute(
        "INSERT INTO boilerplate (field, value, updated) VALUES (?, ?, ?)"
        " ON CONFLICT(field) DO UPDATE SET value = excluded.value, updated = excluded.updated",
        (field, value, datetime.now().isoformat(timespec="seconds"))
    )
    con.commit()
    con.close()


# ── Not-interested extraction ────────────────────────────────────────────────

_NOT_INTERESTED_SYSTEM = """\
Extract what this literary agent explicitly does NOT want from the profile text.
Output ONLY a comma-separated list of short phrases (2-5 words each), nothing else.
If nothing explicit is stated, output nothing (empty string).
Do NOT explain, do NOT use sentences, do NOT say "no exclusions found".
Example output: paranormal romance, horror, sci-fi, children's only, vampires, dystopian
"""


def extract_not_interested(agent_row) -> str:
    """Use Haiku to extract what the agent explicitly doesn't want. Returns comma-sep string."""
    import anthropic

    combined = "\n\n".join(filter(None, [
        ("MSWL: " + (agent_row["mswl_text"] or ""))[:1200],
        ("Agency Bio: " + (agent_row["agency_bio"] or ""))[:600],
        ("PM Bio: " + (agent_row["pm_bio"] or ""))[:600],
        ("Web: " + (agent_row["web_snippets"] or ""))[:400],
    ]))
    if not combined.strip():
        return ""

    key = _get_api_key()
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=120,
        system=_NOT_INTERESTED_SYSTEM,
        messages=[{"role": "user", "content": combined}],
    )
    result = resp.content[0].text.strip()
    # If the model returned an explanation instead of keywords, discard it
    _PROSE_MARKERS = ("don't", "cannot", "can't", "i don't", "no explicit", "empty string",
                      "not stated", "not listed", "profile text", "please provide", "provided text")
    if any(m in result.lower() for m in _PROSE_MARKERS):
        return ""
    # Normalize: lowercase, remove empty entries, skip items that are full sentences
    items = [i.strip().lower() for i in result.split(",")
             if i.strip() and len(i.strip().split()) <= 8]
    return ", ".join(items)


def refresh_not_interested(force: bool = False):
    """Extract agent_not_interested for all agents that don't have it yet (or all if force)."""
    con = connect()
    where = "" if force else "WHERE agent_not_interested IS NULL OR agent_not_interested = ''"
    rows = con.execute(f"""
        SELECT qt_path, name, agency, mswl_text, agency_bio, pm_bio, web_snippets
        FROM agents {where} ORDER BY name
    """).fetchall()
    con.close()

    if not rows:
        print("All agents already have not_interested data. Use --force to re-extract.")
        return

    print(f"Extracting not_interested for {len(rows)} agent(s)…")
    for i, row in enumerate(rows, 1):
        row = dict(row)
        print(f"  [{i}/{len(rows)}] {row['name']}...", end=" ", flush=True)
        not_interested = extract_not_interested(row)
        con = connect()
        con.execute(
            "UPDATE agents SET agent_not_interested = ?, not_interested_extracted = ? WHERE qt_path = ?",
            (not_interested, datetime.now().isoformat(timespec="seconds"), row["qt_path"])
        )
        con.commit()
        con.close()
        preview = not_interested[:60] if not_interested else "(none found)"
        print(preview)
    print("Done.")


# ── Scoring ──────────────────────────────────────────────────────────────────

_PARA_SYSTEM = """\
You are evaluating literary agent fit for a fiction writer seeking PRIMARY REPRESENTATION.
Given a query paragraph describing the writer's book and an agent's profile,
respond with ONLY valid JSON in this exact format (no explanation, no markdown):
{"score": <integer 1-10>, "reason": "<one sentence, max 20 words>"}

Scoring guide:
10 = near-perfect fit, exactly what agent seeks
7-9 = strong fit, meaningful overlap
4-6 = partial fit, some relevant elements
1-3 = weak fit, little overlap

AUTOMATIC LOW SCORES (1-2) — always score 1-2 regardless of genre overlap:
- International rights / subagents / co-agents (their deal history is dominated by
  "International rights:" categories, or their bio describes them as a foreign rights agent)
- Film/TV packaging agents who do not represent books
- Agents who explicitly do not represent adult fiction (e.g. children's/YA-only agents
  being queried with adult fiction)
"""


def _score_keyword(agent_row, keywords: list[str]) -> tuple[float, list[str]]:
    """Return (score 0.0-1.0, list of matched keywords).

    Keywords found in the agent's positive profile text count +1.
    Keywords that also appear in agent_not_interested are excluded from the
    positive count (they matched but the agent doesn't want them).
    """
    if not keywords:
        return 0.0, []
    combined = " ".join(filter(None, [
        agent_row["mswl_text"] or "",
        agent_row["agency_bio"] or "",
        agent_row["pm_bio"] or "",
        agent_row["agent_summary"] or "",
        agent_row["genres"] or "",
    ])).lower()
    not_interested = (agent_row.get("agent_not_interested") or "").lower()

    matched_pos = []   # keyword present in profile AND not in exclusion list
    matched_neg = []   # keyword present but agent explicitly doesn't want it
    for kw in keywords:
        kw_low = kw.lower()
        if kw_low in combined:
            if not_interested and kw_low in not_interested:
                matched_neg.append(kw)
            else:
                matched_pos.append(kw)

    # Score: only positive hits count; negative hits don't add (already excluded from pos)
    score = len(matched_pos) / len(keywords)
    # Combined matched list marks negative hits with (!)
    all_matched = matched_pos + [f"{kw}(!)" for kw in matched_neg]
    return score, all_matched


def _score_para(agent_row, query_paragraph: str) -> tuple[float, str]:
    """Call Claude Haiku for a semantic fit score. Returns (1.0-10.0, reason)."""
    import anthropic
    import json as _json
    import re as _re

    key = _get_api_key()
    client = anthropic.Anthropic(api_key=key)

    profile_parts = [
        f"Agent: {agent_row['name']} ({agent_row['agency']})",
        f"Genres: {agent_row['genres'] or '(none listed)'}",
    ]
    mswl = (agent_row["mswl_text"] or "").strip()
    if mswl:
        profile_parts.append(f"MSWL:\n{mswl[:800]}")
    bio = (agent_row["agency_bio"] or "").strip()
    if bio:
        profile_parts.append(f"Agency Bio:\n{bio[:400]}")
    pm_bio = (agent_row["pm_bio"] or "").strip()
    if pm_bio:
        profile_parts.append(f"Publishers Marketplace Bio:\n{pm_bio[:400]}")
    pm_deals = (agent_row["pm_deals"] or "").strip()
    if pm_deals:
        profile_parts.append(f"Recent PM Deals:\n{pm_deals[:400]}")
    summary = (agent_row["agent_summary"] or "").strip()
    if summary:
        profile_parts.append(f"AI Summary:\n{summary[:400]}")
    not_interested = (agent_row.get("agent_not_interested") or "").strip()
    if not_interested:
        profile_parts.append(f"Agent explicitly does NOT want: {not_interested}")

    user_content = (
        f"QUERY PARAGRAPH:\n{query_paragraph}\n\n"
        f"AGENT PROFILE:\n" + "\n\n".join(profile_parts)
    )

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=_PARA_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = resp.content[0].text.strip()
    # Strip markdown fences if model wrapped in ```json ... ```
    clean = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=_re.DOTALL).strip()
    try:
        parsed = _json.loads(clean)
        score = float(max(1, min(10, parsed["score"])))
        reason = str(parsed.get("reason", ""))
    except Exception:
        m = _re.search(r'\b([1-9]|10)\b', clean)
        score = float(m.group(1)) if m else 5.0
        reason = clean[:120]
    return score, reason


def _compute_firm_ranks():
    """Assign firm_rank 1/2/3 to the top 3 scoring agents per agency; NULL for the rest."""
    con = connect()
    con.execute("UPDATE agents SET firm_rank = NULL")
    rows = con.execute("""
        SELECT qt_path, agency, total_score FROM agents
        WHERE total_score IS NOT NULL
        ORDER BY agency, total_score DESC
    """).fetchall()
    agency_counts: dict[str, int] = {}
    for row in rows:
        agency = row["agency"] or ""
        agency_counts[agency] = agency_counts.get(agency, 0) + 1
        if agency_counts[agency] <= 3:
            con.execute(
                "UPDATE agents SET firm_rank = ? WHERE qt_path = ?",
                (agency_counts[agency], row["qt_path"])
            )
    con.commit()
    con.close()


def score_all_agents(force: bool = False):
    """
    Score every agent in the DB using keyword matching and Claude semantic scoring.
    Skips agents already scored unless force=True. Recomputes firm ranks at the end.
    """
    import json as _json

    keywords_raw = get_setting("keywords")
    query_paragraph = get_setting("query_paragraph")
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()] if keywords_raw else []

    if not keywords and not query_paragraph:
        print(
            "No keywords or query paragraph set.\n"
            "  db.py keywords set 'romance, historical, ...'\n"
            "  db.py para set 'My novel is about...'"
        )
        return

    con = connect()
    where = "" if force else "WHERE scored_at IS NULL OR scored_at = ''"
    rows = con.execute(f"SELECT * FROM agents {where}").fetchall()
    con.close()

    total = len(rows)
    if total == 0:
        print("All agents already scored. Use --force to rescore.")
        return
    print(f"Scoring {total} agent(s)...")

    for i, row in enumerate(rows, 1):
        row = dict(row)
        kw_score, matched = _score_keyword(row, keywords)

        para_score, para_reason = 5.0, "(no query paragraph set)"
        if query_paragraph:
            try:
                para_score, para_reason = _score_para(row, query_paragraph)
            except Exception as e:
                para_reason = f"(error: {e})"

        total_score = round((kw_score * 10 * 0.4) + (para_score * 0.6), 2)

        con = connect()
        con.execute("""
            UPDATE agents SET
                keyword_score    = ?,
                para_score       = ?,
                total_score      = ?,
                matched_keywords = ?,
                para_reasoning   = ?,
                scored_at        = ?
            WHERE qt_path = ?
        """, (
            round(kw_score, 4),
            round(para_score, 2),
            total_score,
            _json.dumps(matched),
            para_reason,
            datetime.now().isoformat(timespec="seconds"),
            row["qt_path"],
        ))
        con.commit()
        con.close()
        print(f"  [{i}/{total}] {row['name']}: kw={kw_score:.2f} para={para_score:.1f} total={total_score}")

    _compute_firm_ranks()
    print("Done. Firm ranks updated.")


# ── Rank display ─────────────────────────────────────────────────────────────

def format_rank_table(rows) -> str:
    import json as _json
    if not rows:
        return "(no scored agents with firm rank 1-3)"
    header = (
        "Ranked agents — firm_rank #1-3 per agency | Score = 40% keyword + 60% para\n\n"
        f"{'#':<4} {'Name':<28} {'Agency':<28} {'Total':>6} {'KW':>5} {'Para':>5} {'Firm':>5}\n"
        + "─" * 85
    )
    lines = [header]
    for i, r in enumerate(rows, 1):
        matched = _json.loads(r["matched_keywords"] or "[]")
        total   = f"{r['total_score']:.2f}"   if r["total_score"]   is not None else "—"
        kw      = f"{r['keyword_score']:.2f}" if r["keyword_score"] is not None else "—"
        para    = f"{r['para_score']:.1f}"    if r["para_score"]    is not None else "—"
        firm    = f"#{r['firm_rank']}"        if r["firm_rank"]     else "—"
        name    = (r["name"]   or "")[:26]
        agency  = (r["agency"] or "")[:26]
        lines.append(f"{i:<4} {name:<28} {agency:<28} {total:>6} {kw:>5} {para:>5} {firm:>5}")
        if r["para_reasoning"]:
            lines.append(f"     Reason: {r['para_reasoning'][:80]}")
        if matched:
            lines.append(f"     Keywords: {', '.join(matched[:8])}")
    return "\n".join(lines)


# ── Boilerplate field definitions ─────────────────────────────────────────────

BOILERPLATE_FIELDS = {
    "synopsis":     {"label": "Plot synopsis (~1 page)",             "type": "text"},
    "bio":          {"label": "Author bio",                          "type": "text"},
    "comps":        {"label": "Comparable titles (comma-separated)", "type": "list"},
    "hook":         {"label": "One-line logline / hook",             "type": "text", "maxlen": 250},
    "wordcount":    {"label": "Manuscript word count",               "type": "int"},
    "genre":        {"label": "Genre / category (comma-separated)",  "type": "list"},
    "series":       {"label": "Series potential (yes/no)",           "type": "bool"},
    "publications": {"label": "Number of prior publications",        "type": "int"},
}


def _parse_boilerplate_input(field: str, raw: str) -> str:
    """Validate and normalize user input for a typed boilerplate field. Returns storage string."""
    import json as _json
    meta = BOILERPLATE_FIELDS[field]
    typ  = meta["type"]
    raw  = raw.strip()
    if typ == "int":
        try:
            return str(int(raw.replace(",", "").replace("_", "")))
        except ValueError:
            raise ValueError(f"'{field}' must be a whole number (got: {raw!r})")
    if typ == "bool":
        if raw.lower() in ("yes", "true", "1", "y"):
            return "true"
        if raw.lower() in ("no", "false", "0", "n"):
            return "false"
        raise ValueError(f"'{field}' must be yes or no (got: {raw!r})")
    if typ == "list":
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if not items:
            raise ValueError(f"'{field}' must be a comma-separated list")
        return _json.dumps(items)
    # text — check optional maxlen
    maxlen = meta.get("maxlen")
    if maxlen and len(raw) > maxlen:
        raise ValueError(f"'{field}' is limited to {maxlen} characters ({len(raw)} given)")
    return raw


def _format_boilerplate_display(field: str, stored: str) -> str:
    """Format a stored boilerplate value for human-readable display."""
    import json as _json
    if not stored:
        return "(empty)"
    meta = BOILERPLATE_FIELDS[field]
    typ  = meta["type"]
    if typ == "int":
        try:
            return f"{int(stored):,}"
        except (ValueError, TypeError):
            return stored
    if typ == "bool":
        return "Yes" if stored == "true" else "No"
    if typ == "list":
        try:
            items = _json.loads(stored)
            return ", ".join(items)
        except Exception:
            return stored
    return stored


# System prompt for query-fit executive summaries
_SUMMARY_SYSTEM = """\
You are a research assistant helping a fiction writer evaluate literary agents to query.
Given the agent data below (from QueryTracker, their agency website, and Manuscript Wishlist),
write a concise QUERY FIT ASSESSMENT.

Format — plain prose, 3-5 sentences, no headers:
1. Lead with a clear verdict: Strong fit / Possible fit / Weak fit
2. Key reasons from their MSWL/agency bio that match or don't match Women's Fiction, Romance, or Historical Fiction
3. One practical note: reply rate, average response time, submission method, or anything unusual

Be direct and opinionated. The writer wants actionable guidance, not a neutral recap.
"""


def generate_agent_summary(profile: dict) -> str:
    """
    Generate and store an AI query-fit assessment for an agent.
    Returns the summary string (empty string on failure or missing API key).
    """
    try:
        import anthropic
        key = _get_api_key()
    except Exception:
        return ""

    context_parts = [
        f"Agent: {profile.get('name', '')} ({profile.get('agency', '')})",
        f"QueryTracker: {profile.get('url', '')}",
        f"Status: {profile.get('open_closed', '')}",
        f"Query Method: {profile.get('query_method', '')}",
        f"Reply Rate: {profile.get('reply_rate', '')} | Request Rate: {profile.get('request_rate', '')}",
        f"Avg Request Time: {profile.get('avg_request_time', '')} | Avg Reject Time: {profile.get('avg_reject_time', '')}",
        f"Last Reply: {profile.get('last_reply', '')} | Last Request: {profile.get('last_request', '')}",
        f"Genres (QT): {profile.get('genres', '')}",
        "",
    ]

    agency_bio = (profile.get("agency_bio") or "").strip()
    if agency_bio:
        context_parts += [f"Agency Website Bio ({profile.get('agency_bio_url', '')}):", agency_bio[:800], ""]

    mswl = (profile.get("mswl_text") or "").strip()
    if mswl:
        context_parts += ["Manuscript Wishlist (manuscriptwishlist.com):", mswl[:1000], ""]

    submission = (profile.get("submission") or "").strip()
    if submission:
        context_parts += ["QueryTracker Submission Form / MSWL:", submission[:500]]

    context = "\n".join(context_parts).strip()

    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            system=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": context}],
        )
        summary = resp.content[0].text.strip()
    except Exception as e:
        return f"(summary unavailable: {e})"

    qt_path = profile.get("qt_path", "")
    if qt_path:
        upsert_summary(qt_path, summary)

    return summary


# ── Output formatting ───────────────────────────────────────────────────────

def format_table(rows, headers=None) -> str:
    if not rows:
        return "(no results)"
    if headers is None:
        headers = list(rows[0].keys())
    data = [dict(r) for r in rows]

    col_w = {}
    for h in headers:
        col_w[h] = min(max(len(h), max(len(str(r.get(h) or "")) for r in data)), 38)

    def cell(val, w):
        s = str(val or "")
        return s[:w - 1] + "…" if len(s) > w else s.ljust(w)

    sep   = "  ".join("─" * col_w[h] for h in headers)
    head  = "  ".join(h.ljust(col_w[h]) for h in headers)
    lines = [head, sep]
    for r in data:
        lines.append("  ".join(cell(r.get(h), col_w[h]) for h in headers))
    return "\n".join(lines)


# ── Claude API translation ──────────────────────────────────────────────────

def ask(question: str) -> str:
    """Translate a natural-language question to SQL via Claude, run it, return formatted table."""
    import anthropic  # imported here so the rest of db.py works without the package

    key    = _get_api_key()
    client = anthropic.Anthropic(api_key=key)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_SQL_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    sql = resp.content[0].text.strip()

    # Strip accidental markdown fences
    if sql.startswith("```"):
        sql = "\n".join(
            line for line in sql.splitlines()
            if not line.startswith("```")
        ).strip()

    # Safety: only allow SELECT statements
    if not sql.upper().lstrip().startswith("SELECT"):
        return f"Generated SQL is not a SELECT — refusing to run:\n{sql}"

    con = connect()
    try:
        rows = con.execute(sql).fetchall()
        header = f"SQL: {sql}\n\n"
        return header + format_table(rows)
    except sqlite3.Error as e:
        return f"SQL error: {e}\nQuery was:\n{sql}"
    finally:
        con.close()


# ── CLI ─────────────────────────────────────────────────────────────────────

# Valid values for submissions.status — enforced in cmd_status
SUBMISSION_STATUS = {
    "pending",          # submitted, awaiting response
    "rejected",         # form rejection or no response / pass
    "full_request",     # agent requested full manuscript
    "partial_request",  # agent requested partial manuscript
    "offered",          # agent offered representation
    "withdrawn",        # writer withdrew the query
}


def cmd_agents(args):
    filters, params = [], []
    i = 0
    name_filter = genre_filter = None
    open_only = False
    while i < len(args):
        if args[i] == "--open":
            open_only = True; i += 1
        elif args[i] == "--genre" and i + 1 < len(args):
            genre_filter = args[i + 1]; i += 2
        elif args[i] == "--name" and i + 1 < len(args):
            name_filter = args[i + 1]; i += 2
        else:
            i += 1
    if open_only:
        filters.append("open_closed = 1")
    if genre_filter:
        filters.append("genres LIKE ?"); params.append(f"%{genre_filter}%")
    if name_filter:
        filters.append("name LIKE ?"); params.append(f"%{name_filter}%")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    con = connect()
    rows = con.execute(
        f"SELECT name, agency, "
        f"CASE WHEN open_closed=1 THEN 'Open' WHEN open_closed=0 THEN 'Closed' ELSE '?' END AS open_closed, "
        f"query_method, reply_rate, request_rate, genres FROM agents {where} ORDER BY name",
        params
    ).fetchall()
    con.close()
    print(format_table(rows))


def cmd_submissions(args):
    filters, params = [], []
    i = 0
    pending_only = False
    agent_filter = None
    while i < len(args):
        if args[i] == "--pending":
            pending_only = True; i += 1
        elif args[i] == "--agent" and i + 1 < len(args):
            agent_filter = args[i + 1]; i += 2
        else:
            i += 1
    if pending_only:
        filters.append("s.status = 'pending'")
    if agent_filter:
        filters.append("s.qt_path = ?"); params.append(agent_filter)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    con = connect()
    rows = con.execute(f"""
        SELECT a.name, s.submitted, s.method, s.status, s.response_days, s.notes
        FROM submissions s JOIN agents a ON s.qt_path = a.qt_path
        {where}
        ORDER BY s.submitted DESC
    """, params).fetchall()
    con.close()
    print(format_table(rows))


def cmd_submit(args):
    if not args:
        print("Usage: db.py submit /agent/ID [--date YYYY-MM-DD] [--method X] [--notes 'text']")
        sys.exit(1)
    qt_path = args[0]
    submitted = date.today().isoformat()
    method = notes = None
    i = 1
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            submitted = args[i + 1]; i += 2
        elif args[i] == "--method" and i + 1 < len(args):
            method = args[i + 1]; i += 2
        elif args[i] == "--notes" and i + 1 < len(args):
            notes = args[i + 1]; i += 2
        else:
            i += 1
    con = connect()
    # Verify agent exists
    row = con.execute("SELECT name FROM agents WHERE qt_path = ?", (qt_path,)).fetchone()
    if not row:
        print(f"Agent {qt_path} not found in DB. Run qt.py --profiles first.")
        con.close(); sys.exit(1)
    con.execute(
        "INSERT INTO submissions (qt_path, submitted, method, notes) VALUES (?, ?, ?, ?)",
        (qt_path, submitted, method, notes)
    )
    con.commit()
    con.close()
    print(f"Logged submission to {row['name']} on {submitted}.")


def cmd_status(args):
    if len(args) < 2:
        print("Usage: db.py status /agent/ID <status> [--date YYYY-MM-DD] [--notes 'text']")
        print(f"Valid statuses: {', '.join(sorted(SUBMISSION_STATUS))}")
        sys.exit(1)
    qt_path, new_status = args[0], args[1]
    if new_status not in SUBMISSION_STATUS:
        print(f"Invalid status '{new_status}'. Valid: {', '.join(sorted(SUBMISSION_STATUS))}")
        sys.exit(1)
    response_date = date.today().isoformat()
    notes = None
    i = 2
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            response_date = args[i + 1]; i += 2
        elif args[i] == "--notes" and i + 1 < len(args):
            notes = args[i + 1]; i += 2
        else:
            i += 1
    con = connect()
    # Find the most recent pending submission for this agent
    row = con.execute("""
        SELECT id, submitted FROM submissions
        WHERE qt_path = ? AND status = 'pending'
        ORDER BY submitted DESC LIMIT 1
    """, (qt_path,)).fetchone()
    if not row:
        print(f"No pending submission found for {qt_path}.")
        con.close(); sys.exit(1)
    # Calculate response days
    try:
        submitted = date.fromisoformat(row["submitted"])
        responded = date.fromisoformat(response_date)
        response_days = (responded - submitted).days
    except Exception:
        response_days = None
    con.execute("""
        UPDATE submissions
        SET status = ?, response_date = ?, response_days = ?, notes = COALESCE(?, notes)
        WHERE id = ?
    """, (new_status, response_date, response_days, notes, row["id"]))
    con.commit()
    con.close()
    print(f"Updated {qt_path} submission status → {new_status} ({response_days} days).")


def cmd_note(args):
    if len(args) < 2:
        print("Usage: db.py note /agent/ID 'note text' [--tags 'tag1,tag2']")
        sys.exit(1)
    qt_path, note_text = args[0], args[1]
    tags = None
    i = 2
    while i < len(args):
        if args[i] == "--tags" and i + 1 < len(args):
            tags = args[i + 1]; i += 2
        else:
            i += 1
    con = connect()
    row = con.execute("SELECT name FROM agents WHERE qt_path = ?", (qt_path,)).fetchone()
    if not row:
        print(f"Agent {qt_path} not in DB.")
        con.close(); sys.exit(1)
    con.execute(
        "INSERT INTO agent_notes (qt_path, created, note, tags) VALUES (?, ?, ?, ?)",
        (qt_path, datetime.now().isoformat(timespec="seconds"), note_text, tags)
    )
    con.commit()
    con.close()
    print(f"Note added for {row['name']}.")


def cmd_query(args):
    if not args:
        print("Usage: db.py query \"SELECT ...\"")
        sys.exit(1)
    sql = " ".join(args)
    con = connect()
    try:
        rows = con.execute(sql).fetchall()
        print(format_table(rows))
    except sqlite3.Error as e:
        print(f"SQL error: {e}")
        sys.exit(1)
    finally:
        con.close()


def cmd_ask(args):
    if not args:
        print("Usage: db.py ask \"natural language question\"")
        sys.exit(1)
    question = " ".join(args)
    try:
        result = ask(question)
        print(result)
    except SystemExit as e:
        print(e); sys.exit(1)
    except ImportError:
        print("ERROR: anthropic package not installed. Run: pip3 install anthropic")
        sys.exit(1)
    except Exception as e:
        msg = str(e)
        if "credit balance is too low" in msg:
            print("ERROR: Anthropic account has no credits. Add credits at console.anthropic.com → Plans & Billing.")
        else:
            print(f"ERROR: {e}")
        sys.exit(1)


def cmd_score(args):
    force = "--force" in args
    score_all_agents(force=force)


def cmd_rank(args):
    limit = 25
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1]); i += 2
        else:
            i += 1
    con = connect()
    rows = con.execute("""
        SELECT name, agency,
               CASE WHEN open_closed=1 THEN 'Open' WHEN open_closed=0 THEN 'Closed' ELSE '?' END AS open_closed,
               firm_rank, total_score, keyword_score, para_score, matched_keywords, para_reasoning
        FROM agents
        WHERE firm_rank IS NOT NULL
        ORDER BY total_score DESC
        LIMIT ?
    """, (limit,)).fetchall()
    con.close()
    print(format_rank_table(rows))


def cmd_keywords(args):
    if not args or args[0] == "get":
        val = get_setting("keywords")
        kws = [k.strip() for k in val.split(",") if k.strip()] if val else []
        print(f"Keywords ({len(kws)}): {val or '(not set)'}")
    elif args[0] == "set" and len(args) > 1:
        text = " ".join(args[1:])
        set_setting("keywords", text)
        kws = [k.strip() for k in text.split(",") if k.strip()]
        print(f"Keywords updated ({len(kws)} keywords): {text}")
    else:
        print("Usage: db.py keywords [get | set <comma,separated,keywords>]")


def cmd_para(args):
    if not args or args[0] == "get":
        val = get_setting("query_paragraph")
        print(val or "(not set)")
    elif args[0] == "set" and len(args) > 1:
        text = " ".join(args[1:])
        set_setting("query_paragraph", text)
        print(f"Query paragraph updated ({len(text)} chars).")
    else:
        print("Usage: db.py para [get | set <text>]")


def _bp_type_tag(field: str) -> str:
    meta = BOILERPLATE_FIELDS[field]
    tag  = meta["type"]
    if tag == "text" and meta.get("maxlen"):
        tag = f"text ≤{meta['maxlen']}"
    return tag


def _print_list_field(field: str, items: list):
    """Print a numbered list display for a list-type boilerplate field."""
    meta = BOILERPLATE_FIELDS[field]
    print(f"[{field}] {meta['label']} ({len(items)} items):")
    if items:
        for i, item in enumerate(items, 1):
            print(f"  {i}. {item}")
    else:
        print("  (empty)")


def cmd_boilerplate(args):
    import json as _json
    if not args:
        print("Boilerplate fields:")
        for field, meta in BOILERPLATE_FIELDS.items():
            stored  = get_boilerplate(field)
            display = _format_boilerplate_display(field, stored)
            preview = (display[:70] + "…") if len(display) > 70 else display
            print(f"  {field:<15} [{_bp_type_tag(field)}] {meta['label']}")
            print(f"               {preview}")
        return

    field = args[0].lower()
    if field not in BOILERPLATE_FIELDS:
        print(f"Unknown field '{field}'. Valid: {', '.join(BOILERPLATE_FIELDS)}")
        sys.exit(1)

    meta    = BOILERPLATE_FIELDS[field]
    is_list = meta["type"] == "list"

    if len(args) == 1:
        stored = get_boilerplate(field)
        if is_list:
            try:
                items = _json.loads(stored) if stored else []
            except Exception:
                items = []
            _print_list_field(field, items)
        else:
            print(f"[{field}] {meta['label']} [{_bp_type_tag(field)}]")
            print(_format_boilerplate_display(field, stored))
        return

    if is_list:
        # Support add/remove/set subcommands.
        # Join remaining args so this works whether called from CLI (multiple args)
        # or from the Discord bot (single string arg like "add Women's Fiction, Romance").
        raw_cmd    = " ".join(args[1:])
        parts      = raw_cmd.split(None, 1)
        subcommand = parts[0].lower()
        raw        = parts[1].strip() if len(parts) > 1 else ""

        if subcommand not in ("add", "remove", "set"):
            print(f"Usage: boilerplate {field} <add|remove|set> <comma-separated items>")
            sys.exit(1)

        stored = get_boilerplate(field)
        try:
            current = _json.loads(stored) if stored else []
        except Exception:
            current = []

        if subcommand == "set":
            if not raw:
                print("Nothing to set.")
                sys.exit(1)
            try:
                normalized = _parse_boilerplate_input(field, raw)
            except ValueError as e:
                print(f"Validation error: {e}")
                sys.exit(1)
            current = _json.loads(normalized)
            set_boilerplate(field, normalized)

        elif subcommand == "add":
            if not raw:
                print("Nothing to add.")
                sys.exit(1)
            new_items = [x.strip() for x in raw.split(",") if x.strip()]
            added   = [x for x in new_items if x not in current]
            skipped = [x for x in new_items if x in current]
            current.extend(added)
            set_boilerplate(field, _json.dumps(current))
            if added:
                print(f"Added: {', '.join(added)}")
            if skipped:
                print(f"Already present (skipped): {', '.join(skipped)}")

        elif subcommand == "remove":
            if not raw:
                print("Nothing to remove.")
                sys.exit(1)
            remove_items = [x.strip() for x in raw.split(",") if x.strip()]
            removed = [x for x in remove_items if x in current]
            missing = [x for x in remove_items if x not in current]
            for x in removed:
                current.remove(x)
            set_boilerplate(field, _json.dumps(current))
            if removed:
                print(f"Removed: {', '.join(removed)}")
            if missing:
                print(f"Not found (skipped): {', '.join(missing)}")

        # Always show the updated list after any mutation
        _print_list_field(field, current)

    else:
        raw = " ".join(args[1:])
        try:
            normalized = _parse_boilerplate_input(field, raw)
        except ValueError as e:
            print(f"Validation error: {e}")
            sys.exit(1)
        set_boilerplate(field, normalized)
        print(f"[{field}] updated → {_format_boilerplate_display(field, normalized)}")


def cmd_summarize(args):
    """Generate agent_summary for all agents missing it. --force regenerates all."""
    force = "--force" in args
    con = connect()
    where = "" if force else "WHERE agent_summary IS NULL OR agent_summary = ''"
    rows = con.execute(f"""
        SELECT qt_path, name, agency, open_closed, query_method,
               reply_rate, request_rate, avg_request_time, avg_reject_time,
               last_reply, last_request, genres,
               agency_bio_url, agency_bio, mswl_text
        FROM agents {where} ORDER BY name
    """).fetchall()
    con.close()

    if not rows:
        print("All agents already have summaries. Use --force to regenerate.")
        return

    print(f"Generating summaries for {len(rows)} agent(s)…")
    for i, row in enumerate(rows, 1):
        profile = dict(row)
        print(f"  [{i}/{len(rows)}] {profile['name']}...", end=" ", flush=True)
        summary = generate_agent_summary(profile)
        if summary and not summary.startswith("(summary unavailable"):
            print("OK")
        else:
            print(f"FAILED: {summary}")
    print("Done.")


def cmd_not_interested(args):
    """Extract agent_not_interested for all agents. --force re-extracts all."""
    force = "--force" in args
    refresh_not_interested(force=force)


def cmd_export(args):
    """Export agent metadata to CSV or JSON (text blobs excluded)."""
    import csv
    import json as _json

    fmt        = "csv"
    open_only  = False
    out_path   = None

    i = 0
    while i < len(args):
        if args[i] == "--format" and i + 1 < len(args):
            fmt = args[i + 1]; i += 2
        elif args[i] == "--open":
            open_only = True; i += 1
        elif args[i] == "--output" and i + 1 < len(args):
            out_path = args[i + 1]; i += 2
        else:
            i += 1

    COLS = [
        "name", "agency", "open_closed", "query_method",
        "reply_rate", "request_rate", "subs_reply_rate",
        "last_reply", "last_request", "avg_request_time", "avg_reject_time",
        "genres", "total_score", "firm_rank",
        "mswl_url", "pm_url", "website", "email", "last_scraped",
    ]

    where = "WHERE open_closed = 1" if open_only else ""
    con = connect()
    rows = con.execute(
        f"SELECT {', '.join(COLS)} FROM agents {where} ORDER BY total_score DESC, name"
    ).fetchall()
    con.close()

    if not out_path:
        ts = datetime.now().strftime("%Y%m%d")
        suffix = "json" if fmt == "json" else "csv"
        out_path = str(pathlib.Path.home() / "querytracker" / f"agents_{ts}.{suffix}")

    data = [dict(r) for r in rows]
    if fmt == "json":
        pathlib.Path(out_path).write_text(_json.dumps(data, indent=2))
    else:
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLS)
            writer.writeheader()
            writer.writerows(data)

    print(f"Exported {len(data)} agents to: {out_path}")


def cmd_history(args):
    """Print full timeline for one agent: profile, submissions, notes, PM deals."""
    import json as _json

    if not args:
        print("Usage: db.py history /agent/ID  (or partial name)")
        return

    target = args[0]
    con = connect()

    # Resolve by qt_path or name
    row = con.execute(
        "SELECT * FROM agents WHERE qt_path = ? OR name LIKE ?",
        (target, f"%{target}%")
    ).fetchone()

    if not row:
        print(f"Agent not found: {target}")
        con.close()
        return

    a = dict(row)
    status_str = "OPEN" if a.get("open_closed") else "CLOSED"
    score_str  = f"{a['total_score']:.2f}" if a.get("total_score") else "—"
    rank_str   = f"#{a['firm_rank']}" if a.get("firm_rank") else "—"

    print(f"── {a['name']} ── {a['agency']} ──────────────────────────────────")
    print(f"  Status:      {status_str}")
    print(f"  Score:       {score_str}  Firm rank: {rank_str}")
    print(f"  Reply rate:  {a.get('reply_rate') or '—'}  "
          f"Request rate: {a.get('request_rate') or '—'}")
    print(f"  Genres:      {a.get('genres') or '—'}")
    if a.get("para_reasoning"):
        print(f"  Reason:      {a['para_reasoning']}")

    # Scrape timestamps
    print()
    print("  Scraped:")
    for label, col in [("QT profile", "last_scraped"), ("Data Explorer", "data_scraped"),
                        ("MSWL", "mswl_scraped"), ("PM", "pm_scraped"),
                        ("Agency site", "agency_page_scraped")]:
        val = a.get(col)
        if val:
            print(f"    {label:<16} {val}")

    # Submissions
    subs = con.execute(
        "SELECT submitted, method, status, response_days, notes FROM submissions "
        "WHERE qt_path = ? ORDER BY submitted DESC",
        (a["qt_path"],)
    ).fetchall()
    if subs:
        print()
        print("  Submissions:")
        for s in subs:
            resp = f"  ({s['response_days']}d)" if s.get("response_days") else ""
            note = f"  — {s['notes']}" if s.get("notes") else ""
            print(f"    {s['submitted']}  {s['method']:<20}  {s['status']}{resp}{note}")

    # Notes
    notes = con.execute(
        "SELECT created, tags, note FROM agent_notes WHERE qt_path = ? ORDER BY created DESC",
        (a["qt_path"],)
    ).fetchall()
    if notes:
        print()
        print("  Notes:")
        for n in notes:
            tags = f"  [{n['tags']}]" if n.get("tags") else ""
            print(f"    {n['created']}{tags}")
            print(f"      {n['note']}")

    # PM recent deals
    pm_raw = a.get("pm_deals")
    if pm_raw:
        try:
            pm_data = _json.loads(pm_raw)
            deals = pm_data.get("recent_deals", [])[:5]
            if deals:
                print()
                print("  Recent PM deals:")
                for d in deals:
                    print(f"    {d.get('date','?'):<12} {d.get('category',''):<28} {d.get('title','')[:50]}")
        except Exception:
            pass

    con.close()


CMDS = {
    "agents":         cmd_agents,
    "submissions":    cmd_submissions,
    "submit":         cmd_submit,
    "status":         cmd_status,
    "note":           cmd_note,
    "history":        cmd_history,
    "query":          cmd_query,
    "ask":            cmd_ask,
    "score":          cmd_score,
    "rank":           cmd_rank,
    "keywords":       cmd_keywords,
    "para":           cmd_para,
    "boilerplate":    cmd_boilerplate,
    "summarize":      cmd_summarize,
    "not-interested": cmd_not_interested,
    "export":         cmd_export,
}

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] not in CMDS:
        print(__doc__)
        sys.exit(0)
    CMDS[argv[0]](argv[1:])
