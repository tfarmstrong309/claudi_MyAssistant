#!/usr/bin/env python3
"""
Look up an agent's Manuscript Wishlist entry at manuscriptwishlist.com.

URL strategy (no login required):
  1. Try direct slug URL: mswl-post/firstname-lastname/
  2. Fall back to site search: /?s=firstname+lastname

Results (MSWL URL + full text) are saved to agents.db.

Usage:
  mswl_lookup.py --agent /agent/7674        look up one agent by QT path
  mswl_lookup.py --name "Lori Galvin"       look up by name (DB lookup)
  mswl_lookup.py --all-open                 all open agents in DB
  mswl_lookup.py --limit 5                  cap number processed
  mswl_lookup.py --force                    re-scrape even if already stored
"""
import asyncio
import pathlib
import re
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from db import connect, upsert_mswl
from common import parse_args, resolve_agents, clean_scraped_text, plain_browser_context

MSWL_BASE = "https://manuscriptwishlist.com"

_MSWL_EXTRA_SKIP = [
    "manuscript wishlist", "search mswl", "recently updated",
    "join mswl", "sign up", "log in", "logged in", "skip to content",
]


def make_slug(name: str) -> str:
    """'Lori Galvin' → 'lori-galvin'"""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


def clean_mswl(raw: str, max_lines: int = 150) -> str:
    """Strip nav/boilerplate and return meaningful wishlist text."""
    return clean_scraped_text(raw, max_lines=max_lines, min_len=12,
                              extra_skip=_MSWL_EXTRA_SKIP)


async def fetch_mswl(page, name: str) -> dict:
    """Fetch MSWL page for agent. Returns {url, text} or {url:'', text:''}."""
    slug = make_slug(name)
    direct_url = f"{MSWL_BASE}/mswl-post/{slug}/"

    # Stage 1: try direct URL
    try:
        resp = await page.goto(direct_url, wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_timeout(500)
        body_text = (await page.inner_text("body")).strip()
        # Valid page: 200 status and meaningful content (not a 404 page)
        if resp and resp.status == 200 and len(body_text) > 300:
            not_found_signals = ["page not found", "404", "nothing found"]
            title = (await page.title()).lower()
            if not any(s in title for s in not_found_signals):
                return {"url": direct_url, "text": clean_mswl(body_text)}
    except Exception:
        pass

    # Stage 2: search fallback
    search_url = f"{MSWL_BASE}/?s={name.replace(' ', '+')}"
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_timeout(300)
    except Exception as e:
        print(f"  Search failed: {e}", file=sys.stderr)
        return {"url": "", "text": ""}

    # Find a link to the agent's mswl-post page
    first = name.split()[0].lower()
    last  = name.split()[-1].lower()
    found_url = None

    for el in await page.query_selector_all("a[href*='mswl-post']"):
        href = (await el.get_attribute("href") or "").lower()
        if first in href or last in href:
            found_url = await el.get_attribute("href")
            break

    if not found_url:
        # Try matching by link text (agent name in the result title)
        for el in await page.query_selector_all("a"):
            text = (await el.inner_text()).strip().lower()
            href = (await el.get_attribute("href") or "")
            if (first in text or last in text) and "mswl-post" in href:
                found_url = href
                break

    if not found_url:
        return {"url": "", "text": ""}

    try:
        await page.goto(found_url, wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_timeout(500)
        body_text = await page.inner_text("body")
        return {"url": found_url, "text": clean_mswl(body_text)}
    except Exception as e:
        return {"url": found_url, "text": f"(fetch error: {e})"}


async def lookup_agent(browser, agent: dict, force: bool = False) -> dict | None:
    """Run MSWL lookup for one agent. Returns result dict or None if skipped/failed."""
    qt_path = agent["qt_path"]
    name    = agent["name"]

    # Skip if already scraped (unless --force)
    if not force:
        con = connect()
        row = con.execute(
            "SELECT mswl_url FROM agents WHERE qt_path = ?", (qt_path,)
        ).fetchone()
        con.close()
        if row and row["mswl_url"]:
            print(f"  {name}: already scraped ({row['mswl_url']}) — skip (use --force to re-scrape).")
            return None

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = await context.new_page()

    try:
        print(f"\n{name} ({agent.get('agency', '')})")
        result = await fetch_mswl(page, name)

        if result["url"]:
            print(f"  MSWL URL: {result['url']}")
            upsert_mswl(qt_path, result["url"], result["text"])
            print(f"  Saved to DB.")
            preview = result["text"].splitlines()[:5]
            if preview:
                print("  Preview:")
                for line in preview:
                    print(f"    {line}")
        else:
            print(f"  Not found on manuscriptwishlist.com.")

        return result if result["url"] else None

    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return None
    finally:
        await page.close()
        await context.close()


async def main():
    parsed = parse_args(sys.argv[1:])
    if not any([parsed["qt_path"], parsed["name"], parsed["all_open"]]):
        print(__doc__)
        sys.exit(0)

    agents = resolve_agents(
        qt_path=parsed["qt_path"],
        name=parsed["name"],
        all_open=parsed["all_open"],
    )
    if parsed["limit"]:
        agents = agents[:parsed["limit"]]

    print(f"Processing {len(agents)} agent(s)…")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for agent in agents:
            await lookup_agent(browser, agent, force=parsed["force"])
        await browser.close()

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
