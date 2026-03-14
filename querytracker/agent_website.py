#!/usr/bin/env python3
"""
Look up an agent's bio page on their agency's own website.

Crawl strategy (2 levels max, public sites — no QT login needed):
  Stage 1: Visit the agency homepage, scan all links for the agent's name
  Stage 2: If not found, find a team/agents sub-page and repeat the scan
  Stage 3: On the found page, extract the full bio using semantic selectors

Results (bio URL + bio text) are saved to agents.db.

Usage:
  agent_website.py --agent /agent/7674        look up one agent by QT path
  agent_website.py --name "Lori Galvin"       look up by name (DB lookup)
  agent_website.py --all-open                 all open agents in DB with a website
  agent_website.py --limit 5                  cap number processed (default: all)
  agent_website.py --force                    re-scrape even if already stored
"""
import asyncio
import pathlib
import sys
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from db import connect, upsert_agency_bio
from common import parse_args, resolve_agents, clean_scraped_text

# Navigation keywords used to find a team/agents sub-page
_TEAM_KEYWORDS = ["agents", "team", "about", "people", "our-agents",
                  "meet", "staff", "roster", "who-we-are"]


def clean_bio(raw: str, max_lines: int = 100) -> str:
    """Strip boilerplate and return up to max_lines of meaningful text."""
    return clean_scraped_text(raw, max_lines=max_lines, min_len=15)


async def _collect_internal_links(page, base_domain: str) -> list[dict]:
    """Return all same-domain links as {url, text} dicts."""
    links = []
    for el in await page.query_selector_all("a[href]"):
        raw = await el.get_attribute("href") or ""
        if not raw or raw.startswith("mailto") or raw.startswith("tel"):
            continue
        current_url = page.url
        full = raw if raw.startswith("http") else urljoin(current_url, raw)
        if urlparse(full).netloc != base_domain:
            continue
        text = (await el.inner_text()).strip().lower()
        links.append({"url": full, "text": text})
    return links


async def find_agent_page(page, agent_name: str, start_url: str) -> str | None:
    """
    Return the URL of the agent's bio page, or None if not found.
    Tries up to 2 levels: homepage, then a team/agents sub-page.
    """
    first = agent_name.split()[0].lower()
    last  = agent_name.split()[-1].lower()
    base_domain = urlparse(start_url).netloc

    def _name_match(url: str, text: str) -> bool:
        u = url.lower()
        return (first in u or last in u or
                first in text or last in text)

    def _team_match(url: str, text: str) -> bool:
        u = url.lower()
        return any(kw in u or kw in text for kw in _TEAM_KEYWORDS)

    # ── Stage 1: homepage ────────────────────────────────────────────────────
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(800)
    except Exception as e:
        print(f"  (could not load {start_url}: {e})", file=sys.stderr)
        return None

    links = await _collect_internal_links(page, base_domain)

    for lnk in links:
        if _name_match(lnk["url"], lnk["text"]):
            return lnk["url"]

    # ── Stage 2: team/agents sub-page ───────────────────────────────────────
    team_url = None
    for lnk in links:
        if _team_match(lnk["url"], lnk["text"]):
            team_url = lnk["url"]
            break

    if not team_url:
        return None

    try:
        await page.goto(team_url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(800)
    except Exception as e:
        print(f"  (could not load team page {team_url}: {e})", file=sys.stderr)
        return None

    links2 = await _collect_internal_links(page, base_domain)
    for lnk in links2:
        if _name_match(lnk["url"], lnk["text"]):
            return lnk["url"]

    return None


async def extract_bio(page, bio_url: str) -> str:
    """Navigate to bio_url and extract meaningful text."""
    try:
        await page.goto(bio_url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(500)
    except Exception as e:
        return f"(could not load bio page: {e})"

    # Prefer semantic/scoped selectors over full body
    for selector in ["main", "article", ".bio", ".agent-bio",
                     ".agent-profile", ".agent-detail", "#content", ".content"]:
        el = await page.query_selector(selector)
        if el:
            text = (await el.inner_text()).strip()
            if len(text) > 100:
                return clean_bio(text)

    # Fallback: full body text
    return clean_bio(await page.inner_text("body"))


async def lookup_agent(browser, agent: dict, force: bool = False) -> dict | None:
    """
    Run the full lookup for one agent dict (must have qt_path, name, website).
    Returns result dict or None if skipped/failed.
    """
    qt_path = agent["qt_path"]
    name    = agent["name"]
    website = agent["website"]

    if not website:
        print(f"  {name}: no website in DB — skipping.", file=sys.stderr)
        return None

    # Skip if already scraped (unless --force)
    if not force:
        con = connect()
        row = con.execute(
            "SELECT agency_bio_url FROM agents WHERE qt_path = ?", (qt_path,)
        ).fetchone()
        con.close()
        if row and row["agency_bio_url"]:
            print(f"  {name}: already scraped ({row['agency_bio_url']}) — skip (use --force to re-scrape).")
            return None

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = await context.new_page()

    try:
        print(f"\n{name} ({agent.get('agency', '')})")
        print(f"  Agency site: {website}")

        bio_url = await find_agent_page(page, name, website)

        if bio_url and bio_url != website:
            print(f"  Found agent page: {bio_url}")
            bio = await extract_bio(page, bio_url)
        else:
            if not bio_url:
                print(f"  Agent page not found — using best-effort from homepage.")
                bio_url = website
            bio = clean_bio(await page.inner_text("body"))

        upsert_agency_bio(qt_path, bio_url, bio)
        print(f"  Saved to DB.")

        preview = bio.splitlines()[:5]
        print("  Bio preview:")
        for line in preview:
            print(f"    {line}")

        return {"qt_path": qt_path, "name": name, "bio_url": bio_url, "bio": bio}

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
        extra_cols="website",
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
