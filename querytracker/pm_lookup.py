#!/usr/bin/env python3
"""
Scrape Publishers Marketplace agent dealmaker profiles and recent deals.

Uses curl_cffi (Chrome TLS fingerprinting) to bypass Cloudflare on login and
deal searches. No Playwright needed for PM — pure HTTP requests + BeautifulSoup.

Requires a PM account — credentials in ~/.pm_creds (username on line 1, password on line 2).
Session cookies are cached to ~/.pm_session.json after first login.

Usage:
  pm_lookup.py --agent /agent/7674        look up one agent by QT path
  pm_lookup.py --name "Lori Galvin"       look up by name (DB lookup)
  pm_lookup.py --all-open                 all open agents in DB
  pm_lookup.py --limit 5                  cap number processed
  pm_lookup.py --force                    re-scrape even if already stored
"""
import json
import pathlib
import re
import sys
import time

import curl_cffi.requests as cf_requests
from bs4 import BeautifulSoup  # type: ignore[import]

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from db import connect, upsert_pm  # type: ignore[import]
from common import parse_args, resolve_agents

PM_BASE    = "https://www.publishersmarketplace.com"
CREDS_FILE = pathlib.Path.home() / ".pm_creds"
SESSION    = pathlib.Path.home() / ".pm_session.json"


# ── Session / auth ────────────────────────────────────────────────────────────

def _make_session() -> cf_requests.Session:
    return cf_requests.Session(impersonate="chrome131")


def _login(session: cf_requests.Session) -> bool:
    """Log in with credentials from ~/.pm_creds. Returns True on success."""
    lines = CREDS_FILE.read_text().strip().splitlines()
    username, password = lines[0].strip(), lines[1].strip()

    # GET first to acquire any initial cookies
    session.get(f"{PM_BASE}/login.php", timeout=30)

    r = session.post(
        f"{PM_BASE}/login.php",
        data={
            "redir":    f"{PM_BASE}/",
            "stage":    "1",
            "username": username,
            "pass":     password,
            "remember": "",
        },
        headers={
            "Referer":      f"{PM_BASE}/login.php",
            "Origin":       PM_BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30,
        allow_redirects=True,
    )
    return "login.php" not in r.url


def get_session() -> cf_requests.Session:
    """Return an authenticated curl_cffi session, reusing cached cookies."""
    session = _make_session()

    if SESSION.exists():
        try:
            data = json.loads(SESSION.read_text())
            cookies = data.get("cookies", {})
            if cookies:
                for name, value in cookies.items():
                    session.cookies.set(name, value, domain="www.publishersmarketplace.com")
                # Quick validation
                r = session.get(f"{PM_BASE}/", timeout=20)
                if "logout" in r.text.lower() or "pmUser" in str(session.cookies):
                    print("  (Using cached PM session)", file=sys.stderr)
                    return session
        except Exception:
            pass

    print("  Logging in to Publishers Marketplace...", file=sys.stderr)
    session = _make_session()
    if not _login(session):
        raise RuntimeError("PM login failed — check ~/.pm_creds")

    cookies = {c: session.cookies.get(c) for c in ("PHPSESSID", "pmUser")
               if session.cookies.get(c)}
    SESSION.write_text(json.dumps({"cookies": cookies}))
    print("  PM session cached.", file=sys.stderr)
    return session


# ── HTML parsing helpers ───────────────────────────────────────────────────────

def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _get_page(session: cf_requests.Session, url: str,
               post_data: dict | None = None,
               referer: str | None = None) -> str:
    """GET or POST a PM page; return HTML. Raises on Cloudflare block."""
    headers = {}
    if referer:
        headers["Referer"] = referer
    if post_data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = PM_BASE
        r = session.post(url, data=post_data, headers=headers, timeout=30)
    else:
        r = session.get(url, headers=headers, timeout=30)

    if "security verification" in r.text.lower() and "Performing" in r.text:
        raise RuntimeError(f"Cloudflare blocked: {url}")
    return r.text


# ── Dealmaker search ──────────────────────────────────────────────────────────

def _search_dealmaker(session: cf_requests.Session,
                       keyword: str, first: str, last: str) -> str | None:
    """Run one PM dealmaker search; return first matching detail URL or None."""
    search_url = f"{PM_BASE}/dealmakers/"
    html = _get_page(session, search_url)
    # Submit form — dealmaker search posts to same URL
    r = session.post(
        search_url,
        data={"keyword": keyword, "sub": "1"},
        headers={
            "Referer": search_url,
            "Origin":  PM_BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30,
        allow_redirects=True,
    )
    soup = _soup(r.text)
    for a in soup.find_all("a", href=re.compile(r"dealmakers/detail\.cgi")):
        text = a.get_text().strip().lower()
        href = a.get("href", "")
        if first in text or last in text:
            clean = re.sub(r"[&?](?:cat|s|nj)=.*", "", href)
            return (PM_BASE + clean) if href.startswith("/") else clean
    return None


def find_dealmaker_url(session: cf_requests.Session, name: str) -> str | None:
    """
    Search PM Dealmakers for an agent by name.
    Returns the detail.cgi URL or None.
    Falls back to last-name-only search for nickname mismatches.
    """
    first = name.split()[0].lower()
    last  = name.split()[-1].lower()

    url = _search_dealmaker(session, name, first, last)
    if url:
        return url
    return _search_dealmaker(session, last.capitalize(), first, last)


# ── Detail page scraping ──────────────────────────────────────────────────────

def scrape_detail(session: cf_requests.Session, url: str) -> dict:
    """
    Scrape an agent's PM dealmaker detail page.
    Returns {"bio": str, "deals": list[dict], "stats": dict}.
    """
    html = _get_page(session, url)
    soup = _soup(html)
    body_text = soup.get_text(" ", strip=True)

    # ── Deal stats ─────────────────────────────────────────────────────────────
    stats: dict = {}

    m = re.search(r"Total Deals:\s*(\d+)", body_text)
    if m:
        stats["total_deals"] = int(m.group(1))
    m = re.search(r"Most recent deal:\s*([^\n]+)", body_text)
    if m:
        stats["most_recent_deal"] = m.group(1).strip()

    categories = re.findall(
        r"(\d+)\s+(General|Debut|Romance|Literary|Mystery|Thriller|"
        r"Science Fiction|Fantasy|Historical|Horror|YA|"
        r"Children's|Nonfiction|Picture Book)[^\n]*",
        body_text,
    )
    if categories:
        stats["deal_categories"] = [{"count": int(c), "category": cat}
                                     for c, cat in categories]

    rankings = re.findall(r"#(\d+)\s*\n\s*in\s*([^\n]+)", html)
    if rankings:
        stats["rankings"] = [{"rank": int(r), "category": cat.strip()}
                              for r, cat in rankings]

    editors_m = re.search(
        r"Top Editors\s*\nInvolved this agent[^\n]*\n((?:[^\n]+\n){1,8})", html
    )
    if editors_m:
        editors = [e.strip() for e in editors_m.group(1).splitlines()
                   if e.strip() and len(e.strip()) > 2]
        stats["top_editors"] = editors[:5]

    imprints_m = re.search(
        r"Top Imprints\s*\nInvolved this agent[^\n]*\n((?:[^\n]+\n){1,8})", html
    )
    if imprints_m:
        imprints = [i.strip() for i in imprints_m.group(1).splitlines()
                    if i.strip() and len(i.strip()) > 2]
        stats["top_imprints"] = imprints[:5]

    # ── Individual deals via DOM ───────────────────────────────────────────────
    deals = []
    cat_counts: dict[str, int] = {}
    for deal_div in soup.find_all("div", class_="Deal"):
        title    = (deal_div.find(class_="Deal-title")    or soup.new_tag("x")).get_text(strip=True)
        author   = (deal_div.find(class_="Deal-author")   or soup.new_tag("x")).get_text(strip=True)
        category = (deal_div.find(class_="Deal-category") or soup.new_tag("x")).get_text(strip=True)
        date     = (deal_div.find(class_="Deal-date")     or soup.new_tag("x")).get_text(strip=True)
        desc     = (deal_div.find(class_="Deal-body")     or soup.new_tag("x")).get_text(strip=True)

        cat = category.strip()
        if cat:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if date.strip() and len(deals) < 25:
            deals.append({
                "date":        date.strip(),
                "title":       title.strip(),
                "author":      re.sub(r"^By\s*", "", author.strip(), flags=re.I),
                "category":    cat,
                "description": desc.strip()[:500],
            })

    if cat_counts:
        stats["category_counts"] = sorted(
            [{"category": k, "count": v} for k, v in cat_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )

    # ── Member page for full bio ───────────────────────────────────────────────
    bio = ""
    read_more = soup.find("a", string=re.compile(r"read more", re.I))
    if read_more:
        href = read_more.get("href", "").strip()
        if href and href != "#":
            member_url = (PM_BASE + href) if href.startswith("/") else href
            try:
                member_html = _get_page(session, member_url)
                member_soup = _soup(member_html)
                col = member_soup.find(class_="Member-meta-rightcol")
                if col:
                    bio = col.get_text("\n", strip=True)
                    if bio.lower().startswith("about\n"):
                        bio = bio[bio.index("\n") + 1:].strip()
            except Exception:
                pass

    # Fallback: truncated bio from dealmaker page
    if not bio:
        el = soup.find(class_="DM-bio")
        if el:
            bio = el.get_text("\n", strip=True)

    for cutoff in ["Copyright ©", "if(navigator", "$(document)"]:
        idx = bio.find(cutoff)
        if idx > 0:
            bio = bio[:idx].strip()
    bio = bio.replace("read more …", "").replace("read more…", "").strip()

    return {"bio": bio, "deals": deals, "stats": stats}


# ── Per-agent orchestration ───────────────────────────────────────────────────

def lookup_agent(session: cf_requests.Session,
                  agent: dict, force: bool = False) -> bool:
    """Look up one agent on PM. Returns True if data was saved."""
    qt_path = agent["qt_path"]
    name    = agent["name"]

    if not force:
        con = connect()
        row = con.execute("SELECT pm_url FROM agents WHERE qt_path = ?",
                          (qt_path,)).fetchone()
        con.close()
        if row and row["pm_url"]:
            print(f"  {name}: already scraped ({row['pm_url']}) — skip (use --force).")
            return False

    print(f"\n{name} ({agent.get('agency', '')})")

    pm_url = find_dealmaker_url(session, name)
    if not pm_url:
        print("  Not found on Publishers Marketplace.")
        return False

    print(f"  PM URL: {pm_url}")
    data = scrape_detail(session, pm_url)

    bio   = data["bio"]
    deals = data["deals"]
    stats = data["stats"]

    print(f"  Bio: {len(bio)} chars")
    print(f"  Deals: {len(deals)} found")
    if stats.get("total_deals"):
        print(f"  Total PM deals: {stats['total_deals']}")
    if stats.get("rankings"):
        for r in stats["rankings"][:3]:
            print(f"  Ranked #{r['rank']} in {r['category']}")

    deals_payload = {"stats": stats, "recent_deals": deals}
    upsert_pm(qt_path, pm_url, bio, json.dumps(deals_payload))
    print("  Saved to DB.")

    if bio:
        for line in bio.splitlines()[:3]:
            print(f"    {line}")

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
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

    session = get_session()
    for agent in agents:
        try:
            lookup_agent(session, agent, force=parsed["force"])
        except Exception as e:
            print(f"  ERROR {agent['name']}: {e}", file=sys.stderr)
        time.sleep(1)  # polite delay between agents

    print("\nDone.")


if __name__ == "__main__":
    main()
