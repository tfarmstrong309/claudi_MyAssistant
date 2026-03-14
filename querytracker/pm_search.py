#!/usr/bin/env python3
"""
Search Publishers Marketplace deals by fiction category to discover agents.

Searches recent deals (configurable year range) across relevant fiction categories,
extracts the agent from each deal, and reports who's making deals in your genres.

Cross-references with the local DB — shows in-DB agents separately.

Uses curl_cffi for Cloudflare-safe HTTP requests (no Playwright needed).

Usage:
  pm_search.py                            default categories, 2024-2025
  pm_search.py --years 2023,2024,2025     custom year range
  pm_search.py --categories commercial,literary,romance,general
  pm_search.py --tier 1                   large publishers only (1=large, 2=mid, 3=small)
  pm_search.py --list-categories          show available category values

Categories that map to our genres:
  fiction_commercial   Commercial fiction (often includes women's fiction, book club)
  fiction_literary     Literary fiction
  fiction_romance      Romance (includes historical romance)
  fiction_general      General/Other fiction (catch-all, includes historical, upmarket)
  fiction_debut        Debut fiction (cross-genre)
"""
import pathlib
import re
import sys
import time

from bs4 import BeautifulSoup  # type: ignore[import]

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from db import connect  # type: ignore[import]
from pm_lookup import get_session, PM_BASE, _get_page  # type: ignore[import]

# ── Constants ──────────────────────────────────────────────────────────────────

SEARCH_URL = f"{PM_BASE}/deals/search.cgi"

DEFAULT_CATEGORIES = [
    "fiction_commercial",
    "fiction_literary",
    "fiction_romance",
    "fiction_general",
]

ALL_CATEGORIES = {
    "fiction":            "Fiction (all)",
    "fiction_debut":      "Fiction: Debut",
    "fiction_commercial": "Fiction: Commercial",
    "fiction_literary":   "Fiction: Literary",
    "fiction_horror":     "Fiction: Horror",
    "fiction_mystery":    "Fiction: Mystery/Crime",
    "fiction_newadult":   "Fiction: New Adult",
    "fiction_paranormal": "Fiction: Paranormal",
    "fiction_scifi":      "Fiction: Sci-Fi/Fantasy",
    "fiction_thriller":   "Fiction: Thriller",
    "fiction_romance":    "Fiction: Romance",
    "fiction_romantasy":  "Fiction: Romantasy",
    "fiction_general":    "Fiction: General/Other",
}

DEFAULT_YEARS = [2024, 2025]
PAGE_SIZE     = 100   # PM returns 100 deals per page

AGENCY_KEYWORDS = (
    "literary", "agency", "management", "associates", "represent",
    "talent", "creative management",
)
PUBLISHER_KEYWORDS = (
    "publishing", "publishers", "press", "books", "imprint",
    "harlequin", "simon", "penguin", "random", "harper", "macmillan",
    "norton", "hachette", "scholastic", "sourcebooks", "workman",
    "algonquin", "hyperion", "knopf", "doubleday", "putnam", "berkley",
    "avon", "bantam", "dell", "dutton", "henry holt", "farrar",
    "picador", "flatiron", "grand central", "little brown",
    "viking", "crown", "anchor", "vintage", "ballantine",
)
_ORG_SUFFIXES = (" llc", " inc", " ltd", " llp", " corp", " co.", " group",
                  " studio", " media", " entertainment", " productions")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_agency(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in AGENCY_KEYWORDS)


def _is_publisher(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in PUBLISHER_KEYWORDS)


def _is_person_name(name: str) -> bool:
    if not name or name[0].isdigit():
        return False
    n_low = name.lower()
    if any(n_low.endswith(s) or s.strip() in n_low for s in _ORG_SUFFIXES):
        return False
    if "&" in name or _is_publisher(name) or _is_agency(name):
        return False
    _ORG_WORDS = {"house", "stories", "story", "books", "press", "inspired",
                   "imprint", "label", "studio", "studios", "records"}
    if any(w.lower() in _ORG_WORDS for w in name.split()):
        return False
    parts = name.strip().split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    if any(w.isupper() and len(w) >= 2 for w in parts):
        return False
    return True


def _extract_agent(dealmakers: list[dict]) -> tuple[str, str, str]:
    """From a list of {name, href} dicts, extract (agent_name, agency_name, pm_url)."""
    if not dealmakers:
        return "", "", ""
    for i in range(len(dealmakers) - 1, 0, -1):
        dm = dealmakers[i]
        if _is_agency(dm["name"]) and not _is_publisher(dm["name"]):
            agent_dm = dealmakers[i - 1]
            if _is_person_name(agent_dm["name"]):
                return agent_dm["name"], dm["name"], agent_dm["href"]
    if len(dealmakers) == 2:
        if _is_agency(dealmakers[1]["name"]) and _is_person_name(dealmakers[0]["name"]):
            return dealmakers[0]["name"], dealmakers[1]["name"], dealmakers[0]["href"]
    for dm in reversed(dealmakers):
        if _is_person_name(dm["name"]) and not _is_publisher(dm["name"]):
            return dm["name"], "", dm["href"]
    return "", "", ""


def _parse_deals_from_html(html: str) -> list[dict]:
    """Extract deal records from HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    deals = []
    for deal_div in soup.find_all("div", class_="Deal"):
        title    = (deal_div.find(class_="Deal-title")    or soup.new_tag("x")).get_text(strip=True)
        author   = (deal_div.find(class_="Deal-author")   or soup.new_tag("x")).get_text(strip=True)
        category = (deal_div.find(class_="Deal-category") or soup.new_tag("x")).get_text(strip=True)
        date     = (deal_div.find(class_="Deal-date")     or soup.new_tag("x")).get_text(strip=True)
        dms = [
            {"name": a.get_text(strip=True), "href": a.get("href", "")}
            for a in deal_div.find_all("a", class_="dealmaker")
        ]
        deals.append({
            "title": title, "author": author,
            "category": category, "date": date,
            "dealmakers": dms,
        })
    return deals


def _get_total_from_html(html: str) -> int:
    # Handles both plain and <b>-wrapped numbers: "Showing <b>1 - 100</b> of <b>419</b> deals"
    m = re.search(r"Showing\s+(?:<b>)?\d+(?:</b>)?\s*-\s*(?:<b>)?\d+(?:</b>)?\s+of\s+(?:<b>)?(\d+)(?:</b>)?\s+deals", html)
    if m:
        return int(m.group(1))
    m = re.search(r"Showing\s+all\s+(?:<b>)?(\d+)(?:</b>)?\s+deals", html)
    return int(m.group(1)) if m else 0


def _get_form_fields(html: str, category: str, year: int) -> dict:
    """Extract pageMenuForm hidden fields from the HTML."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", {"id": "pageMenuForm"})
    fields: dict = {}
    if form:
        for inp in form.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            val  = inp.get("value", "")
            if name:
                fields[name] = val
    # Ensure category and year are set (fallback if not in form)
    fields.setdefault("category", category)
    fields.setdefault("year", str(year))
    fields.setdefault("gtoken", "")
    fields.setdefault("p", "")
    return fields


def _search_category_year(session, category: str, year: int, tier: str) -> list[dict]:
    """Fetch all pages for one category+year combination."""
    url = (f"{SEARCH_URL}?ss_c=deal&category={category}&year={year}"
           + (f"&ss_f_deal_tier={tier}" if tier else ""))

    html = _get_page(session, url)
    total = _get_total_from_html(html)
    if not total:
        return []

    num_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"    {ALL_CATEGORIES.get(category, category)} {year}: {total} deals, "
          f"{num_pages} page(s)", file=sys.stderr)

    all_deals = _parse_deals_from_html(html)
    base_fields = _get_form_fields(html, category, year)

    for pg in range(2, num_pages + 1):
        offset = (pg - 1) * PAGE_SIZE
        post_data = {**base_fields, "p": str(pg), "o": str(offset), "t": str(total)}
        try:
            page_html = _get_page(session, SEARCH_URL, post_data=post_data, referer=url)
        except Exception:
            break
        batch = _parse_deals_from_html(page_html)
        if not batch:
            break
        all_deals.extend(batch)
        time.sleep(0.5)  # polite delay

    return all_deals


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    categories = DEFAULT_CATEGORIES[:]
    years      = DEFAULT_YEARS[:]
    tier       = ""
    list_cats  = False

    i = 0
    while i < len(args):
        if args[i] == "--categories" and i + 1 < len(args):
            categories = [c.strip() for c in args[i+1].split(",")]; i += 2
        elif args[i] == "--years" and i + 1 < len(args):
            years = [int(y.strip()) for y in args[i+1].split(",")]; i += 2
        elif args[i] == "--tier" and i + 1 < len(args):
            tier = args[i+1]; i += 2
        elif args[i] == "--list-categories":
            list_cats = True; i += 1
        else:
            i += 1

    if list_cats:
        print("Available category values for --categories:")
        for k, v in ALL_CATEGORIES.items():
            print(f"  {k:<28} {v}")
        sys.exit(0)

    bad = [c for c in categories if c not in ALL_CATEGORIES]
    if bad:
        print(f"Unknown categories: {bad}  Run --list-categories to see valid values.")
        sys.exit(1)

    cat_labels  = ", ".join(ALL_CATEGORIES[c] for c in categories)
    year_labels = ", ".join(str(y) for y in years)
    print(f"Searching PM deals: {cat_labels}")
    print(f"Years: {year_labels}" + (f"  Tier: {tier}" if tier else ""))

    # Load DB agents for cross-reference
    con = connect()
    db_agents = {r["name"].lower(): dict(r)
                 for r in con.execute("SELECT qt_path, name, agency FROM agents").fetchall()}
    con.close()

    agents: dict[str, dict] = {}

    session = get_session()

    for year in years:
        for category in categories:
            deals = _search_category_year(session, category, year, tier)
            for deal in deals:
                agent_name, agency_name, pm_url = _extract_agent(deal["dealmakers"])
                if not agent_name or not pm_url:
                    continue
                pm_id = pm_url.rstrip("/").split("id=")[-1] if "id=" in pm_url else pm_url
                if pm_id not in agents:
                    agents[pm_id] = {
                        "name":       agent_name,
                        "agency":     agency_name,
                        "pm_url":     pm_url,
                        "deal_count": 0,
                        "categories": set(),
                    }
                agents[pm_id]["deal_count"] += 1
                agents[pm_id]["categories"].add(deal["category"])

    sorted_agents = sorted(agents.values(), key=lambda x: x["deal_count"], reverse=True)

    in_db      = [(a, db_agents[a["name"].lower()]) for a in sorted_agents
                  if a["name"].lower() in db_agents]
    new_agents = [a for a in sorted_agents if a["name"].lower() not in db_agents]

    print(f"\nTotal: {len(sorted_agents)} unique agents from "
          f"{sum(a['deal_count'] for a in sorted_agents)} deals")
    print(f"  {len(in_db)} already in DB | {len(new_agents)} new discoveries\n")

    if in_db:
        print("── Agents already in your DB ─────────────────────────────────────────")
        print(f"  {'Name':<30} {'Agency':<35} {'Deals':>5}")
        print("  " + "─" * 72)
        for agent, _ in in_db:
            cats = ", ".join(sorted(agent["categories"]))[:40]
            print(f"  {agent['name']:<30} {agent['agency'][:34]:<35} {agent['deal_count']:>5}")
            print(f"    {cats}")

    if new_agents:
        print(f"\n── New discoveries (not yet in DB) ───────────────────────────────────")
        print(f"  {'Name':<30} {'Agency':<35} {'Deals':>5}  PM URL")
        print("  " + "─" * 105)
        for a in new_agents:
            cats = ", ".join(sorted(a["categories"]))[:35]
            print(f"  {a['name']:<30} {a['agency'][:34]:<35} {a['deal_count']:>5}")
            print(f"    {cats}")
            print(f"    {a['pm_url']}")

        print(f"\n  To add: look up on QueryTracker, append /agent/NNN paths to my_agents.txt")

    print("\nDone.")


if __name__ == "__main__":
    main()
