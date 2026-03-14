#!/usr/bin/env python3
"""
Search manuscriptwishlist.com for agents seeking specific genres.
No login required — uses the public structured search.

Results are AJAX-rendered after form submission; this script
checks the appropriate checkboxes, submits, and paginates.

Usage:
  mswl_search.py                             default genres, list matches
  mswl_search.py --enrich                    also scrape each agent's full MSWL text
  mswl_search.py --genres "historical,womens-fiction,literary,bookclub"
  mswl_search.py --limit 100
  mswl_search.py --list-genres              show all available genre values

Agents already in DB: printed (and their mswl_url/mswl_text updated with --enrich).
New discoveries (not in DB): printed for manual review / QT lookup.
"""
import asyncio
import pathlib
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from db import connect, upsert_mswl
from common import plain_browser_context

MSWL_BASE  = "https://manuscriptwishlist.com"
SEARCH_URL = f"{MSWL_BASE}/find-agentseditors/search/"

DEFAULT_GENRES = ["historical", "womens-fiction", "literary", "bookclub", "romance", "commercial"]

ALL_GENRES = {
    "actionadventure":      "Action/Adventure",
    "bookclub":             "Bookclub",
    "commercial":           "Commercial",
    "contemporary":         "Contemporary",
    "crime":                "Crime",
    "domestic-thriller":    "Domestic Thriller",
    "family-saga":          "Family Saga",
    "fantasy":              "Fantasy",
    "general":              "General",
    "gothic":               "Gothic",
    "historical":           "Historical",
    "horror":               "Horror",
    "humor":                "Humor",
    "lgbtq":                "LGBTQ",
    "literary":             "Literary",
    "magical-realism":      "Magical Realism",
    "mystery":              "Mystery",
    "romance":              "Romance",
    "romcom":               "Romcom",
    "science-fiction":      "Science Fiction",
    "speculative":          "Speculative",
    "thriller":             "Thriller",
    "upmarket-speculative": "Upmarket Speculative",
    "womens-fiction":       "Women's Fiction",
    "young-adult":          "Young Adult",
}


# ── Scraping helpers ──────────────────────────────────────────────────────────

async def _setup_and_submit(page, genres: list[str]):
    """Navigate to search page, tick checkboxes, submit, wait for AJAX results."""
    await page.goto(SEARCH_URL, timeout=30000, wait_until="load")
    await page.wait_for_timeout(1500)

    # Age group: Adult only
    await page.check('input[name="wpv-age-group[]"][value="adult"]')

    # Requestor types: agents only (not editors)
    for val in ["agent", "associate-literary-agent", "assistant-literary-agent", "junior-agent"]:
        cb = await page.query_selector(f'input[name="wpv-requestor[]"][value="{val}"]')
        if cb:
            await cb.check()

    # Fiction genres
    for genre in genres:
        cb = await page.query_selector(f'input[name="wpv-fiction-genre[]"][value="{genre}"]')
        if cb:
            await cb.check()
        else:
            print(f"  Warning: genre '{genre}' not found on page", file=sys.stderr)

    # Submit and wait for AJAX
    await page.click('input[name="wpv_filter_submit"]')
    await page.wait_for_timeout(3000)


async def _scrape_cards(page) -> list[dict]:
    """Extract agent cards from the current (AJAX-rendered) results."""
    results = []
    for card in await page.query_selector_all(".aesc-wrap"):
        name_el   = await card.query_selector(".aesc-title")
        agency_el = await card.query_selector(".aesc-agency-press-name")
        link_el   = await card.query_selector("a[href*='/mswl-post/']")

        if not name_el:
            continue

        name   = (await name_el.inner_text()).strip()
        agency = (await agency_el.inner_text()).strip() if agency_el else ""
        href   = (await link_el.get_attribute("href")) if link_el else ""

        if name:
            results.append({"name": name, "agency": agency, "url": href})
    return results


async def _next_page(page) -> bool:
    """Click the WPV 'Next' pagination link if present. Returns True if navigated."""
    nxt = await page.query_selector("a.js-wpv-pagination-next-link, a.wpv-filter-next-link")
    if nxt:
        await nxt.click()
        await page.wait_for_timeout(3000)
        return True
    return False


async def _scrape_mswl_text(page, url: str) -> str:
    """Scrape full wishlist text from an agent's individual MSWL profile page."""
    await page.goto(url, timeout=20000, wait_until="load")
    await page.wait_for_timeout(800)

    for sel in [".entry-content", "article .post-content", "article", "main"]:
        el = await page.query_selector(sel)
        if el:
            text = (await el.inner_text()).strip()
            if len(text) > 100:
                for cutoff in ["Filed Under:", "Tagged With:", "Leave a Reply",
                               "About Manuscript Wishlist", "© Manuscript Wishlist"]:
                    idx = text.find(cutoff)
                    if idx > 100:
                        text = text[:idx].strip()
                return text
    return ""


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    args = sys.argv[1:]

    genres      = DEFAULT_GENRES[:]
    limit       = None
    enrich      = False
    list_genres = False

    i = 0
    while i < len(args):
        if args[i] == "--genres" and i + 1 < len(args):
            genres = [g.strip() for g in args[i + 1].split(",")]; i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1]); i += 2
        elif args[i] == "--enrich":
            enrich = True; i += 1
        elif args[i] == "--list-genres":
            list_genres = True; i += 1
        else:
            i += 1

    if list_genres:
        print("Available fiction genre values for --genres:")
        for k, v in sorted(ALL_GENRES.items()):
            print(f"  {k:<28} {v}")
        sys.exit(0)

    bad = [g for g in genres if g not in ALL_GENRES]
    if bad:
        print(f"Unknown genre(s): {bad}  Run --list-genres to see valid values.")
        sys.exit(1)

    print(f"Searching MSWL: {', '.join(ALL_GENRES[g] for g in genres)}")
    if enrich:
        print("  (--enrich: will scrape full wishlist text for matched agents)")

    # Load existing agent names for matching
    con = connect()
    db_agents = {r["name"].lower(): dict(r)
                 for r in con.execute("SELECT qt_path, name, agency FROM agents").fetchall()}
    con.close()

    async with async_playwright() as p:
        browser, context, pg = await plain_browser_context(p)

        # Submit the search form
        await _setup_and_submit(pg, genres)

        # Paginate and collect all cards
        all_found = []
        page_num  = 1

        while True:
            batch = await _scrape_cards(pg)
            if not batch:
                print(f"  Page {page_num}: no cards found — stopping.")
                break

            all_found.extend(batch)
            print(f"  Page {page_num}: {len(batch)} agents  (total: {len(all_found)})")

            if limit and len(all_found) >= limit:
                all_found = all_found[:limit]
                break

            if not await _next_page(pg):
                break
            page_num += 1

        print(f"\nTotal: {len(all_found)} agents across {page_num} page(s)")

        # Partition: in DB vs new
        in_db      = []
        new_agents = []
        for agent in all_found:
            key = agent["name"].lower()
            if key in db_agents:
                in_db.append((agent, db_agents[key]))
            else:
                new_agents.append(agent)

        print(f"  {len(in_db)} already in DB | {len(new_agents)} new discoveries\n")

        # Report / update DB agents
        if in_db:
            print("── Agents already in your DB ────────────────────────────────")
            for agent, db_row in in_db:
                if enrich and agent["url"]:
                    text = await _scrape_mswl_text(pg, agent["url"])
                    if text:
                        upsert_mswl(db_row["qt_path"], agent["url"], text)
                        print(f"  ✓ {agent['name']:<28} {len(text):>5} chars saved")
                    else:
                        print(f"  ✗ {agent['name']:<28} no text found")
                else:
                    print(f"  {agent['name']:<28} {agent['agency'][:35]:<36} {agent['url']}")

        # New discoveries
        if new_agents:
            print("\n── New discoveries (not yet in DB) ──────────────────────────")
            print(f"  {'Name':<30} {'Agency':<35} MSWL URL")
            print("  " + "─" * 95)
            for a in new_agents:
                print(f"  {a['name']:<30} {a['agency'][:34]:<35} {a['url']}")
            print(f"\n  To add: look up on QueryTracker, append /agent/NNN paths to my_agents.txt")

        await context.close()
        await browser.close()

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
