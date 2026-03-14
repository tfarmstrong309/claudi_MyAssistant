#!/usr/bin/env python3
"""
Cross-reference QueryTracker, MSWL, and Publishers Marketplace to find the
best-fit agents for Women's Fiction / Historical / Literary / Romance.

An agent that appears on 2 or more of the three sources gets a full QT profile
scraped and stored in agents.db.

Pipeline:
  1. QT genre search  — all open agents for Women's Fiction + Romance + Historical
  2. MSWL genre search — agents with matching wishlist genres
  3. PM deals search  — agents with recent deal activity in matching genres
  4. Cross-reference  — keep agents hitting ≥ 2 sources; skip those already in DB
  5. Full QT profile  — scrape and upsert for each new 2-of-3 hit

Usage:
  discover_agents.py              full run (all three sources, no limit)
  discover_agents.py --dry-run    show what would be scraped, don't write DB
  discover_agents.py --limit N    cap QT scrape at N agents (for testing)
  discover_agents.py --skip-pm    skip Publishers Marketplace step
  discover_agents.py --skip-mswl  skip MSWL step
"""
import asyncio
import pathlib
import re
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from db import connect, upsert_agent  # type: ignore[import]
from common import parse_args

# ── Config ────────────────────────────────────────────────────────────────────

QT_GENRES    = ["Women's Fiction", "Romance", "Historical"]

MSWL_GENRES  = ["historical", "womens-fiction", "literary", "bookclub", "romance", "commercial"]

PM_CATEGORIES = [
    "fiction_commercial",
    "fiction_literary",
    "fiction_romance",
    "fiction_general",
]
PM_YEARS = [2024, 2025]


# ── Name normalisation ────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for fuzzy matching."""
    return re.sub(r"[^a-z ]", "", name.lower()).strip()



# ── Step 1: QT agents ─────────────────────────────────────────────────────────

async def get_qt_agents(p, limit=None) -> dict[str, dict]:
    """Return {norm_name: {name, agency, qt_path}} for all open genre agents on QT."""
    from qt import get_context, search_agents  # type: ignore[import]

    print("\n[1/3] QueryTracker: fetching open agents for", QT_GENRES, "…")
    browser, _context, page = await get_context(p)
    agents = await search_agents(
        page,
        limit=limit or 9999,
        open_only=True,
        default_genres=True,
    )
    await browser.close()
    print(f"      → {len(agents)} agents found on QT")
    return {_norm(a["name"]): a for a in agents}


# ── Step 2: MSWL agents ───────────────────────────────────────────────────────

async def get_mswl_agents(p) -> dict[str, dict]:
    """Return {norm_name: {name, agency, mswl_url}} for MSWL genre search."""
    from mswl_search import _setup_and_submit, _scrape_cards, _next_page, UA  # type: ignore[import]

    print("\n[2/3] MSWL: searching genres", MSWL_GENRES, "…")
    browser = await p.chromium.launch(headless=True)
    ctx = await browser.new_context(user_agent=UA, locale="en-US")
    pg = await ctx.new_page()

    await _setup_and_submit(pg, MSWL_GENRES)
    agents = {}
    page_num = 1
    while True:
        cards = await _scrape_cards(pg)
        for card in cards:
            k = _norm(card["name"])
            if k:
                agents[k] = card
        print(f"      page {page_num}: {len(cards)} cards  (total {len(agents)})", file=sys.stderr)
        if not await _next_page(pg):
            break
        page_num += 1

    await browser.close()
    print(f"      → {len(agents)} agents found on MSWL")
    return agents


# ── Step 3: PM agents ─────────────────────────────────────────────────────────

def get_pm_agents() -> dict[str, dict]:
    """Return {norm_name: {name, agency, pm_url, deal_count}} from PM deals."""
    from pm_lookup import get_session  # type: ignore[import]
    from pm_search import (  # type: ignore[import]
        SEARCH_URL, PAGE_SIZE, ALL_CATEGORIES,
        _get_total_from_html, _parse_deals_from_html,
        _get_form_fields, _extract_agent,
    )

    print("\n[3/3] Publishers Marketplace: searching", PM_CATEGORIES, PM_YEARS, "…")
    session = get_session()

    pm_agents: dict[str, dict] = {}

    for year in PM_YEARS:
        for category in PM_CATEGORIES:
            url = f"{SEARCH_URL}?ss_c=deal&category={category}&year={year}"
            from pm_lookup import _get_page  # type: ignore[import]
            html = _get_page(session, url)
            total = _get_total_from_html(html)
            if not total:
                continue
            num_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
            print(f"      {ALL_CATEGORIES.get(category, category)} {year}: "
                  f"{total} deals, {num_pages} page(s)", file=sys.stderr)

            all_deals = _parse_deals_from_html(html)
            base_fields = _get_form_fields(html, category, year)

            for p_num in range(2, num_pages + 1):
                offset = (p_num - 1) * PAGE_SIZE
                post_data = {**base_fields, "p": str(p_num), "o": str(offset), "t": str(total)}
                try:
                    page_html = _get_page(session, SEARCH_URL, post_data=post_data, referer=url)
                except Exception:
                    break
                batch = _parse_deals_from_html(page_html)
                if not batch:
                    break
                all_deals.extend(batch)

            for deal in all_deals:
                agent_name, agency_name, pm_url = _extract_agent(deal["dealmakers"])
                if not agent_name or not pm_url:
                    continue
                k = _norm(agent_name)
                if k not in pm_agents:
                    pm_agents[k] = {
                        "name":       agent_name,
                        "agency":     agency_name,
                        "pm_url":     pm_url,
                        "deal_count": 0,
                    }
                pm_agents[k]["deal_count"] += 1

    print(f"      → {len(pm_agents)} agents found on PM")
    return pm_agents


# ── Step 4: Cross-reference ───────────────────────────────────────────────────

def cross_reference(qt: dict, mswl: dict, pm: dict) -> list[dict]:
    """Return agents hitting ≥ 2 sources, sorted by hit count desc."""
    all_keys = set(qt) | set(mswl) | set(pm)
    results = []
    for k in all_keys:
        sources = []
        if k in qt:    sources.append("qt")
        if k in mswl:  sources.append("mswl")
        if k in pm:    sources.append("pm")
        if len(sources) < 2:
            continue

        # Prefer QT data for canonical name/agency; fall back to mswl then pm
        base = qt.get(k) or mswl.get(k) or pm.get(k)
        results.append({
            "name":       base["name"],
            "agency":     base.get("agency", ""),
            "qt_path":    qt[k].get("qt_path") if k in qt else None,
            "mswl_url":   mswl[k].get("url")   if k in mswl else None,
            "pm_url":     pm[k].get("pm_url")   if k in pm else None,
            "deal_count": pm[k].get("deal_count", 0) if k in pm else 0,
            "sources":    sources,
            "hit_count":  len(sources),
        })

    results.sort(key=lambda x: (-x["hit_count"], -x["deal_count"], x["name"]))
    return results


# ── Step 5: Full QT profile ───────────────────────────────────────────────────

async def scrape_full_profiles(p, candidates: list[dict], dry_run: bool):
    """For each candidate with a qt_path, scrape full profile and upsert to DB."""
    import json
    from datetime import datetime
    from qt import get_context, build_profiles, _parse_report_totals  # type: ignore[import]

    to_scrape = [c for c in candidates if c.get("qt_path")]
    if not to_scrape:
        print("\nNo candidates have QT paths — nothing to scrape.")
        return

    print(f"\n[5/5] Scraping {len(to_scrape)} full QT profiles…")
    if dry_run:
        print("      (dry-run: skipping)")
        return

    browser, context, page = await get_context(p)

    # build_profiles expects list of {name, agency, qt_path, url}
    agent_stubs = [
        {"name": c["name"], "agency": c["agency"],
         "qt_path": c["qt_path"], "url": f"https://querytracker.net{c['qt_path']}"}
        for c in to_scrape
    ]
    profiles = await build_profiles(context, page, agent_stubs)
    await browser.close()

    # Save each profile to DB (same logic as qt.py main())
    saved = 0
    for prof in profiles:
        try:
            totals = _parse_report_totals(prof.get("report_12mo", ""))
            gc  = prof.get("genre_counts")    or {}
            wcc = prof.get("wordcount_counts") or {}
            db_prof = {
                **prof,
                "agency_bio":            prof.get("bio", ""),
                "clients_json":          json.dumps(prof["clients"]) if prof.get("clients") else None,
                "queries_sent_12mo":     totals["sent"],
                "full_requests_12mo":    totals["full"],
                "partial_requests_12mo": totals["partial"],
                "rejections_12mo":       totals["rejections"],
                "genre_counts":          json.dumps(gc)  if gc  else None,
                "wordcount_counts":      json.dumps(wcc) if wcc else None,
                "data_scraped":          datetime.now().isoformat(timespec="seconds") if (gc or wcc) else None,
            }
            upsert_agent(db_prof)
            saved += 1
        except Exception as e:
            print(f"      Error saving {prof.get('name', '?')}: {e}")

    print(f"      Done — {saved}/{len(profiles)} profiles saved to agents.db")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parsed    = parse_args(sys.argv[1:])
    dry_run   = parsed["dry_run"]
    limit     = parsed["limit"]
    skip_mswl = "--skip-mswl" in parsed["extra"]
    skip_pm   = "--skip-pm"   in parsed["extra"]

    # Load existing DB agents so we can skip re-scraping them
    con = connect()
    db_agents = {_norm(r["name"]) for r in con.execute("SELECT name FROM agents").fetchall()}
    con.close()
    print(f"DB already has {len(db_agents)} agents.")

    async with async_playwright() as p:
        qt_agents   = await get_qt_agents(p, limit=limit)
        mswl_agents = await get_mswl_agents(p) if not skip_mswl else {}
    pm_agents = get_pm_agents() if not skip_pm else {}

    # Cross-reference
    hits = cross_reference(qt_agents, mswl_agents, pm_agents)
    new_hits  = [h for h in hits if _norm(h["name"]) not in db_agents]
    already   = [h for h in hits if _norm(h["name"])     in db_agents]

    print(f"\n── Cross-reference results ──────────────────────────────────────")
    print(f"  Total 2-of-3 hits: {len(hits)}")
    print(f"  Already in DB:     {len(already)}")
    print(f"  New to scrape:     {len(new_hits)}")

    # Show all-3 hits first
    all3 = [h for h in new_hits if h["hit_count"] == 3]
    two  = [h for h in new_hits if h["hit_count"] == 2]

    if all3:
        print(f"\n  ★ All-3 hits (QT + MSWL + PM) — {len(all3)} new agents:")
        for h in all3:
            deals = f" | {h['deal_count']} PM deals" if h["deal_count"] else ""
            print(f"    {h['name']:<32} {h['agency'][:35]:<36}{deals}")

    if two:
        print(f"\n  ◆ 2-of-3 hits — {len(two)} new agents:")
        for h in two:
            srcs = "+".join(s.upper() for s in h["sources"])
            deals = f" | {h['deal_count']} deals" if h["deal_count"] else ""
            print(f"    [{srcs}] {h['name']:<30} {h['agency'][:35]:<36}{deals}")

    if already:
        print(f"\n  Already in DB ({len(already)}):")
        for h in already:
            srcs = "+".join(s.upper() for s in h["sources"])
            print(f"    [{srcs}] {h['name']}")

    # Scrape full profiles for new agents
    if new_hits:
        async with async_playwright() as p:
            await scrape_full_profiles(p, new_hits, dry_run)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
