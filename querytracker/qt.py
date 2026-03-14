#!/usr/bin/env python3
"""
Search QueryTracker for literary agents.

Usage:
  qt.py --name "Jane Smith"              search by agent name
  qt.py --genre "Literary Fiction"       search by genre (overrides defaults)
  qt.py --name "Smith" --genre "Fantasy" combined
  qt.py --agent /agent/12345             full QT profile for a specific agent
  qt.py --profiles --name "Smith"        search + fetch full profiles
  qt.py --limit 20                       max results (default 25)
  qt.py --all-genres                     remove default genre filters
  qt.py --include-closed                 include agents closed to queries

Default search filters (override with flags above):
  - Open to queries only
  - Genres: Women's Fiction, Romance, Historical
"""
import asyncio
from datetime import datetime
import json
import pathlib
import re
import sys
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

# DB integration (same directory)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from common import parse_args as _parse_args
try:
    from db import upsert_agent as _upsert_agent, generate_agent_summary as _generate_summary, connect as _db_connect
    from db import upsert_mswl as _upsert_mswl, upsert_agency_bio as _upsert_agency_bio
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    _generate_summary = None
    _db_connect = None
    _upsert_mswl = None
    _upsert_agency_bio = None

try:
    from mswl_lookup import fetch_mswl
    from agent_website import find_agent_page, extract_bio as _extract_bio
    _ENRICHMENT_AVAILABLE = True
except ImportError:
    _ENRICHMENT_AVAILABLE = False

CREDS    = pathlib.Path.home() / ".querytracker_creds"
SESSION  = pathlib.Path.home() / ".querytracker_session.json"
BASE_URL = "https://querytracker.net"

USERNAME, PASSWORD = CREDS.read_text().strip().splitlines()[:2]

DEFAULT_GENRES = ["Women's Fiction", "Romance", "Historical"]


async def login(page):
    await page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded")
    await page.fill("input[name='username']", USERNAME)
    await page.fill("input[name='password']", PASSWORD)
    await page.click("button[type='submit'][name='login']")
    await page.wait_for_load_state("domcontentloaded")
    return "AUSTENRAND" in (await page.inner_text("body")).upper()


async def get_context(playwright):
    browser = await playwright.chromium.launch(headless=True)

    if SESSION.exists():
        try:
            state = json.loads(SESSION.read_text())
            context = await browser.new_context(storage_state=state)
            page = await context.new_page()
            await page.goto(f"{BASE_URL}/dashboard/index.php", wait_until="domcontentloaded")
            body = await page.inner_text("body")
            if "AUSTENRAND" in body.upper():
                return browser, context, page
            await page.close()
            await context.close()
        except Exception:
            pass

    context = await browser.new_context()
    page = await context.new_page()
    success = await login(page)
    if not success:
        print("ERROR: Login failed. Check credentials in ~/.querytracker_creds")
        sys.exit(1)

    state = await context.storage_state()
    SESSION.write_text(json.dumps(state))
    return browser, context, page


async def search_agents(page, name=None, genre=None, limit=25, open_only=True, default_genres=True):
    await page.goto(f"{BASE_URL}/agents/search", wait_until="domcontentloaded")

    # Always use 500 per page (QT maximum) to minimise page count
    try:
        await page.locator("select").filter(has_text="50").first.select_option("500", timeout=3000)
    except Exception:
        pass

    if name:
        await page.fill("#searchAgents", name)
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("domcontentloaded")

    async def click_label_for(text_match):
        """Find a label containing text_match and click it (handles hidden checkboxes)."""
        labels = await page.query_selector_all("label")
        for label in labels:
            text = (await label.inner_text()).strip()
            if text_match.lower() in text.lower():
                try:
                    await label.click(timeout=3000)
                    return text
                except Exception:
                    # Fallback: click via JS
                    try:
                        await label.evaluate("el => el.click()")
                        return text
                    except Exception:
                        pass
        return None

    # Apply "Open to Queries" filter
    if open_only:
        try:
            matched = await click_label_for("open")
            if matched:
                print(f"Applied filter: {matched}", file=sys.stderr)
                await page.wait_for_timeout(500)
        except Exception:
            pass

    # Apply genre filters
    genres_to_apply = []
    if genre:
        genres_to_apply = [genre]
    elif default_genres:
        genres_to_apply = DEFAULT_GENRES

    for genre_name in genres_to_apply:
        try:
            matched = await click_label_for(genre_name)
            if matched:
                print(f"Applied genre filter: {matched}", file=sys.stderr)
        except Exception:
            pass
        await page.wait_for_timeout(300)

    await page.wait_for_timeout(2000)

    async def _collect_page_results(results, seen):
        """Scrape agent links from current page into results/seen. Returns count added."""
        added = 0
        links = await page.query_selector_all("a[href*='/agent/']")
        for link in links:
            if limit and len(results) >= limit:
                break
            href = await link.get_attribute("href") or ""
            if not re.match(r"^/agent/\d+$", href):
                continue
            if href in seen:
                continue
            seen.add(href)

            agent_name = (await link.inner_text()).strip()
            if not agent_name:
                continue

            row = await link.evaluate_handle("el => el.closest('tr')")
            row_text = await row.evaluate("el => el ? el.innerText : ''")
            cols = [c.strip() for c in row_text.split("\t") if c.strip()]

            results.append({
                "name": agent_name,
                "agency": cols[1] if len(cols) > 1 else "",
                "qt_path": href,
                "url": f"{BASE_URL}{href}",
            })
            added += 1
        return added

    results = []
    seen = set()

    # Collect first page
    await _collect_page_results(results, seen)

    # Paginate through remaining pages using the pager <select data-bind="filters.page">
    while not (limit and len(results) >= limit):
        pager_sel = await page.query_selector("select[data-bind='filters.page']")
        if not pager_sel:
            break

        # Get available page options
        options = await pager_sel.query_selector_all("option")
        current = None
        next_val = None
        for opt in options:
            val = await opt.get_attribute("value")
            selected = await opt.get_attribute("selected")
            if selected is not None:
                current = val
            elif current is not None and next_val is None:
                next_val = val

        if not next_val:
            break  # Already on last page

        # Navigate to next page — select may be hidden, so trigger via JS
        await pager_sel.evaluate(
            "(el, v) => { el.value = v; el.dispatchEvent(new Event('change', {bubbles:true})); }",
            next_val
        )
        await page.wait_for_timeout(2500)
        added = await _collect_page_results(results, seen)
        if added == 0:
            break  # No new results, stop

        body_text = await page.inner_text("body")
        m = re.search(r"Showing \d+ of (\d+) Agents Found", body_text)
        total_str = f"/{m.group(1)}" if m else ""
        print(f"  QT page {next_val}: {len(results)}{total_str} agents collected", file=sys.stderr)

    return results


def _parse_report_totals(report_12mo: str):
    """Extract per-row totals from the 12-month report string.
    Rows (in order): Queries Sent, Partial Requests, Full Requests, Rejections, Closed/No Response.
    Returns dict with keys: sent, partial, full, rejections (all int or None)."""
    empty = {"sent": None, "partial": None, "full": None, "rejections": None}
    if not report_12mo or report_12mo.startswith("("):
        return empty
    lines = [l.strip() for l in report_12mo.splitlines() if l.strip()]
    # Skip header line ("Via: Postal | Email | ...")
    data_lines = [l for l in lines if not l.lower().startswith("via:")]
    if not data_lines:
        return empty

    def _last_int(line):
        nums = re.findall(r"\d+", line)
        return int(nums[-1]) if nums else None

    # Report rows (order is fixed by QT):
    # 0: Queries Sent, 1: Partial Requests, 2: Full Requests, 3: Rejections, 4: Closed/No Response
    return {
        "sent":       _last_int(data_lines[0]) if len(data_lines) > 0 else None,
        "partial":    _last_int(data_lines[1]) if len(data_lines) > 1 else None,
        "full":       _last_int(data_lines[2]) if len(data_lines) > 2 else None,
        "rejections": _last_int(data_lines[3]) if len(data_lines) > 3 else None,
    }


async def get_agent_data_stats(page, qt_path: str) -> dict:
    """Scrape the QT Data Explorer for an agent with Reply = 'Partial or Full Request'.
    Returns genre_counts {genre: count}, wordcount_counts {range: count}, total_requests int.
    Uses the previous-year timeframe (QT default)."""
    result = {"genre_counts": {}, "wordcount_counts": {}, "total_requests": 0}
    try:
        await page.goto(f"{BASE_URL}{qt_path}/data", wait_until="domcontentloaded")
        await page.wait_for_timeout(800)

        # Find and set the Reply filter to "Partial or Full Request" (value='6')
        reply_sel = None
        for sel in await page.query_selector_all("select"):
            for opt in await sel.query_selector_all("option"):
                if (await opt.get_attribute("value")) == "6" and \
                        "Partial or Full Request" in (await opt.inner_text()):
                    reply_sel = sel
                    break
            if reply_sel:
                break

        if not reply_sel:
            print(f"  (data stats: Reply filter not found for {qt_path})", file=sys.stderr)
            return result

        # Native select may be hidden (custom JS dropdown); use element.evaluate to bypass
        await reply_sel.evaluate("el => { el.value = '6'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
        await page.wait_for_timeout(1200)

        # Determine total pages from pagination select ("Page N of M" options)
        max_pages = 1
        page_sel = None
        for sel in await page.query_selector_all("select"):
            opts = await sel.query_selector_all("option")
            if not opts:
                continue
            first_txt = (await opts[0].inner_text()).strip()
            if re.match(r"Page \d+ of \d+", first_txt):
                page_sel = sel
                last_txt = (await opts[-1].inner_text()).strip()
                m = re.match(r"Page \d+ of (\d+)", last_txt)
                max_pages = int(m.group(1)) if m else len(opts)
                break

        genre_counts: dict = {}
        wc_counts:    dict = {}
        total = 0

        for page_num in range(1, max_pages + 1):
            if page_sel and page_num > 1:
                pn = str(page_num)
                await page_sel.evaluate(
                    "(el, v) => { el.value = v; el.dispatchEvent(new Event('change', {bubbles: true})); }",
                    pn,
                )
                await page.wait_for_timeout(800)

            for row in await page.query_selector_all("table tbody tr"):
                cells = await row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                genre = (await cells[0].inner_text()).strip()
                wc    = (await cells[1].inner_text()).strip()
                # Skip blank/placeholder cells
                if genre and genre not in ("—", "Genre"):
                    genre_counts[genre] = genre_counts.get(genre, 0) + 1
                if wc and wc not in ("—", "Word Count"):
                    wc_counts[wc] = wc_counts.get(wc, 0) + 1
                total += 1

        result["genre_counts"]     = genre_counts
        result["wordcount_counts"] = wc_counts
        result["total_requests"]   = total
        print(f"  Data Explorer: {total} request(s) found ({max_pages} page(s))", file=sys.stderr)

    except Exception as e:
        print(f"  (data stats error for {qt_path}: {e})", file=sys.stderr)

    return result


async def get_full_profile(page, path):
    """Single-pass profile: quick stats, genres, clients, 12-month report."""
    url = f"{BASE_URL}{path}" if path.startswith("/") else path
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(500)

    # External links (agency website)
    ext_links = await page.query_selector_all("a[href^='http']")
    websites = []
    for link in ext_links:
        href = await link.get_attribute("href") or ""
        skip = ["querytracker", "google", "yahoo", "bing", "amazon", "twitter",
                "publishersmarketplace", "sfwa", "manuscriptwishlist", "aalitagents"]
        if not any(s in href.lower() for s in skip) and href not in websites:
            websites.append(href)

    # Submission form link (/query/ID)
    query_link = None
    for link in await page.query_selector_all("a[href*='/query/']"):
        href = await link.get_attribute("href") or ""
        if re.match(r"^/query/\d+$", href):
            query_link = href
            break

    # === GENRES TAB ===
    genres = []
    try:
        genre_tab = await page.query_selector("[data-tab-button='genres']")
        if genre_tab:
            await genre_tab.click()
            await page.wait_for_timeout(800)
            genre_els = await page.query_selector_all(
                "[data-tab-content='genres'] li, [data-tab-content='genres'] .genre, [data-tab='genres'] li"
            )
            for el in genre_els:
                t = (await el.inner_text()).strip()
                if t:
                    genres.append(t)
            if not genres:
                updated_body = await page.inner_text("body")
                updated_lines = [l.strip() for l in updated_body.splitlines() if l.strip()]
                capture = False
                for line in updated_lines:
                    if "this agent is seeking" in line.lower():
                        capture = True
                        continue
                    if capture:
                        if line in ("Clients", "Letter", "Comments", "Reports", "Main", "Quick Stats", "Disclaimer"):
                            break
                        if "always verify" in line.lower() or "querytracker can not" in line.lower():
                            break
                        if line and not line.startswith("(") and not line.startswith("•") and len(line) > 1:
                            genres.append(line)
                        if len(genres) > 30:
                            break
    except Exception:
        pass

    # === CLIENTS TAB ===
    clients = []
    try:
        clients_tab = await page.query_selector("[data-tab-button='clients']")
        if clients_tab:
            await clients_tab.click()
            await page.wait_for_timeout(1000)

            # Try data-tab-section='clients' links (QT uses data-tab-section, not data-tab-content)
            ui_skip = {"add a client", "view our entire", "who reps whom", "manage", "database"}
            client_links = await page.query_selector_all("[data-tab-section='clients'] a")
            for el in client_links:
                name = (await el.inner_text()).strip()
                href = await el.get_attribute("href") or ""
                if not name or len(name) < 3 or len(name) > 50:
                    continue
                if any(s in name.lower() for s in ui_skip):
                    continue
                if "amazon.com" in href:
                    amazon_url = href
                else:
                    amazon_url = f"https://www.amazon.com/s?k={name.replace(' ', '+')}&i=stripbooks"
                clients.append({"name": name, "amazon_url": amazon_url})
                if len(clients) >= 10:
                    break

            # Fallback: parse body text after "Known Clients"
            if not clients:
                cb = await page.inner_text("body")
                cb_lines = [l.strip() for l in cb.splitlines() if l.strip()]
                capture = False
                for line in cb_lines:
                    if "known clients" in line.lower():
                        capture = True
                        continue
                    if capture:
                        if line in ("Genres", "Letter", "Comments", "Reports", "Main", "Quick Stats", "Disclaimer"):
                            break
                        if 3 < len(line) < 60 and not line.startswith("(") and not line[0].isdigit():
                            amazon_url = f"https://www.amazon.com/s?k={line.replace(' ', '+')}&i=stripbooks"
                            clients.append({"name": line, "amazon_url": amazon_url})
                            if len(clients) >= 10:
                                break
    except Exception as e:
        print(f"  (clients tab error: {e})", file=sys.stderr)

    # === REPORTS TAB — 12-Month Queries Sent/Replied ===
    report_12mo = ""
    try:
        reports_tab = await page.query_selector("[data-tab-button='reports']")
        if reports_tab:
            await reports_tab.click()
            await page.wait_for_timeout(800)

            # Select report type: qResults = "Queries Sent/Replied"
            selects = await page.query_selector_all("select")
            for sel in selects:
                options = await sel.query_selector_all("option")
                opt_vals = [await o.get_attribute("value") or "" for o in options]
                if "qResults" in opt_vals:
                    await sel.select_option("qResults")
                    await page.wait_for_timeout(300)
                    break

            # Timeframe is radio buttons (name="report-timeframe"), not a select
            # value="12" = Previous 12 Months
            radio_12 = await page.query_selector("input[type='radio'][name='report-timeframe'][value='12']")
            if radio_12:
                await radio_12.evaluate("el => el.click()")
                await page.wait_for_timeout(800)

            # Parse tab-separated data rows (actual report data, not dropdown labels)
            rb = await page.inner_text("body")
            header = ""
            rlines = []
            for line in rb.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                # Data rows have tabs and contain numbers
                if "\t" in line and any(c.isdigit() for c in stripped):
                    parts = [p.strip() for p in stripped.split("\t") if p.strip()]
                    if not header:
                        header = "Via:  Postal | Email | Online Form | QueryManager | Total"
                    rlines.append("  ".join(parts))
                    if len(rlines) >= 6:
                        break
            if rlines:
                report_12mo = header + "\n" + "\n".join(rlines)
    except Exception as e:
        report_12mo = f"(report error: {e})"

    # === MAIN TAB — quick stats ===
    try:
        main_tab = await page.query_selector("[data-tab-button='main']")
        if main_tab:
            await main_tab.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass

    body = await page.inner_text("body")
    lines = [l.strip() for l in body.splitlines() if l.strip()]

    # Agent name from h1; agency from body text scan or agency link
    agent_name = ""
    agency_name = ""
    try:
        h1 = await page.query_selector("h1")
        if h1:
            agent_name = (await h1.inner_text()).strip()
        title = await page.title()
        if not agent_name:
            agent_name = title.split(" - ")[0].split(" | ")[0].strip()
        # Agency: try extracting from page title ("Name | Agency | QueryTracker")
        title_parts = [p.strip() for p in title.replace(" - ", " | ").split(" | ")]
        if len(title_parts) >= 2:
            # Parts: [Name, Agency, "QueryTracker"] — take middle part(s)
            non_qt = [p for p in title_parts if p and "querytracker" not in p.lower()]
            if len(non_qt) >= 2:
                agency_name = non_qt[1]
        # Fallback: /agency/ link
        if not agency_name:
            for a in await page.query_selector_all("a[href*='/agency/']"):
                txt = (await a.inner_text()).strip()
                if txt:
                    agency_name = txt
                    break
        # Fallback: scan body lines for "Member of <Agency>" pattern
        if not agency_name:
            for line in lines:
                if line.lower().startswith("member of "):
                    agency_name = line[len("member of "):].strip()
                    break
        # Fallback: line immediately after agent name (common QT layout)
        if not agency_name and agent_name:
            _skip = {
                "open to queries", "closed to", "query reply rate", "request rate",
                "accepts queries", "main", "genres", "clients", "letter", "comments",
                "reports", "quick stats", "twitter", "e-mail", "disclaimer",
                "query tracker", "querytracker",
            }
            for i, line in enumerate(lines):
                if agent_name.lower() in line.lower() and i + 1 < len(lines):
                    candidate = lines[i + 1]
                    if not any(s in candidate.lower() for s in _skip) and len(candidate) > 3:
                        agency_name = candidate
                    break
    except Exception:
        pass

    # Single-pass stat extraction — avoids 10 separate O(n) scans
    _STAT_KEYS = [
        ("query reply rate:",     "reply_rate"),
        ("request rate:",         "request_rate"),
        ("subs reply rate:",      "subs_reply_rate"),
        ("last reply:",           "last_reply"),
        ("last request:",         "last_request"),
        ("average request time:", "avg_request_time"),
        ("average reject time:",  "avg_reject_time"),
        ("accepts queries via",   "query_method"),
        ("e-mail:",               "email"),
        ("twitter (x):",          "twitter"),
    ]
    stats = {}
    for i, line in enumerate(lines):
        ll = line.lower()
        for key, field in _STAT_KEYS:
            if field not in stats and key in ll and i + 1 < len(lines):
                val = lines[i + 1]
                if field == "query_method":
                    val = val.replace("[Go To Form]", "").strip()
                stats[field] = val

    return {
        "name":             agent_name,
        "agency":           agency_name,
        "reply_rate":       stats.get("reply_rate", ""),
        "request_rate":     stats.get("request_rate", ""),
        "subs_reply_rate":  stats.get("subs_reply_rate", ""),
        "last_reply":       stats.get("last_reply", ""),
        "last_request":     stats.get("last_request", ""),
        "avg_request_time": stats.get("avg_request_time", ""),
        "avg_reject_time":  stats.get("avg_reject_time", ""),
        "query_method":     stats.get("query_method", ""),
        "open_closed":      "Closed" if "Closed to Unsolicited" in body else "Open",
        "email":            stats.get("email", ""),
        "website":          websites[0] if websites else "",
        "twitter":          stats.get("twitter", ""),
        "genres":           ", ".join(genres[:20]),
        "clients":          clients,
        "report_12mo":      report_12mo,
        "query_link":       query_link,
        "qt_path":          path if path.startswith("/agent/") else f"/agent/{path.split('/')[-1]}",
        "url":              f"{BASE_URL}{path}" if path.startswith("/") else path,
    }


async def get_submission_form(context, query_path):
    """Fetch the agent's submission/query form page and extract bio + MSWL."""
    if not query_path:
        return ""

    url = f"{BASE_URL}{query_path}" if query_path.startswith("/") else query_path
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(800)

        body = await page.inner_text("body")
        lines = [l.strip() for l in body.splitlines() if l.strip()]

        # Short lines that are section headers to keep
        keep_short = {"bio", "mswl", "notes", "notes:", "specifically:", "tips:"}

        # Boilerplate to skip
        skip_contains = [
            "characters remaining", "cookie", "privacy policy", "© ",
            "all rights reserved", "newsletter", "log out", "you are signed into",
            "use the restore answers", "required field",
        ]
        skip_startswith = [
            "send a query to", "view this agent's querytracker",
            "querymanager", "word count:", "genre:", "title:", "step 1:", "step 2:",
        ]

        bio_lines = []
        for line in lines:
            lower = line.lower()
            if any(s in lower for s in skip_contains):
                continue
            if any(lower.startswith(s) for s in skip_startswith):
                continue
            # Keep named section headers even if short
            if lower in keep_short:
                bio_lines.append(f"\n[{line.upper()}]")
                continue
            if len(line) < 20:
                continue
            bio_lines.append(line)

        return "\n".join(bio_lines).strip()

    except Exception as e:
        return f"(could not fetch submission form: {e})"
    finally:
        await page.close()


def save_mswl(agent_name, submission_text):
    """Save submission form / MSWL to ~/querytracker/mswl/<AgentName>.txt"""
    if not submission_text:
        return None
    mswl_dir = pathlib.Path.home() / "querytracker" / "mswl"
    mswl_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\s-]", "", agent_name).strip().replace(" ", "_")
    filepath = mswl_dir / f"{safe_name}.txt"
    filepath.write_text(submission_text)
    return str(filepath)


async def get_agency_bio(context, agent_name, website_url):
    """Visit agency website, find the agent's bio page, return {bio, bio_url}."""
    if not website_url:
        return {"bio": "", "bio_url": ""}

    page = await context.new_page()
    try:
        await page.goto(website_url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1000)

        first_name = agent_name.split()[0].lower()
        last_name  = agent_name.split()[-1].lower()
        base_domain = urlparse(website_url).netloc

        links = await page.query_selector_all("a[href]")
        bio_url = None
        for link in links:
            raw_href = await link.get_attribute("href") or ""
            if raw_href.startswith("mailto") or not raw_href:
                continue
            full_href = raw_href if raw_href.startswith("http") else urljoin(website_url, raw_href)
            if urlparse(full_href).netloc != base_domain:
                continue
            href_lower = full_href.lower()
            text_lower = (await link.inner_text()).strip().lower()
            if first_name in href_lower or last_name in href_lower or first_name in text_lower or last_name in text_lower:
                bio_url = full_href
                break

        if bio_url and bio_url != website_url:
            await page.goto(bio_url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(500)

        body = await page.inner_text("body")
        lines = [l.strip() for l in body.splitlines() if l.strip() and len(l.strip()) > 20]
        bio_lines = []
        for line in lines:
            if any(skip in line.lower() for skip in ["cookie", "privacy policy", "© ", "all rights reserved", "newsletter"]):
                continue
            bio_lines.append(line)
            if len(bio_lines) >= 20:
                break

        return {"bio": "\n".join(bio_lines), "bio_url": bio_url or website_url}

    except Exception as e:
        return {"bio": f"(could not fetch website: {e})", "bio_url": website_url}
    finally:
        await page.close()


async def build_profiles(context, qt_page, agents):
    """For each agent, fetch full QT profile + submission form + agency bio.
    For single-agent requests, also scrapes MSWL and agency website inline."""
    single = len(agents) == 1
    profiles = []
    for agent in agents:
        qt = await get_full_profile(qt_page, agent["qt_path"])
        # Prefer name from QT profile; fall back to what search returned; last resort: path
        agent_name = qt.get("name") or agent.get("name") or agent["qt_path"]
        print(f"  Fetching profile: {agent_name}...", file=sys.stderr)

        submission = ""
        mswl_file = None
        if qt.get("query_link"):
            print(f"    Fetching submission form...", file=sys.stderr)
            submission = await get_submission_form(context, qt["query_link"])
            if submission:
                mswl_file = save_mswl(agent_name, submission)
                if mswl_file:
                    print(f"    Saved MSWL: {mswl_file}", file=sys.stderr)

        # For single agents we skip the shallow bio scrape — the deep scrape below replaces it.
        bio = ""
        agency_bio_url = ""
        if qt.get("website") and not (single and _ENRICHMENT_AVAILABLE):
            print(f"    Fetching agency bio...", file=sys.stderr)
            agency_result = await get_agency_bio(context, agent_name, qt["website"])
            bio = agency_result["bio"]
            agency_bio_url = agency_result["bio_url"]

        # For single-agent requests, do a full inline MSWL + agency website scrape
        mswl_text = ""
        mswl_url  = ""
        if single and _ENRICHMENT_AVAILABLE:
            # --- MSWL ---
            print(f"    Scraping MSWL...", file=sys.stderr)
            try:
                mswl_page = await context.new_page()
                mswl_result = await fetch_mswl(mswl_page, agent_name)
                await mswl_page.close()
                if mswl_result["url"]:
                    mswl_url  = mswl_result["url"]
                    mswl_text = mswl_result["text"]
                    if _DB_AVAILABLE and _upsert_mswl:
                        _upsert_mswl(agent["qt_path"], mswl_url, mswl_text)
                    print(f"    MSWL: {mswl_url}", file=sys.stderr)
                else:
                    print(f"    MSWL: not found", file=sys.stderr)
            except Exception as e:
                print(f"    MSWL error: {e}", file=sys.stderr)

            # --- Agency website (deep crawl) ---
            website = qt.get("website") or ""
            if website:
                print(f"    Deep-scraping agency website...", file=sys.stderr)
                try:
                    web_page = await context.new_page()
                    bio_page_url = await find_agent_page(web_page, agent_name, website)
                    if bio_page_url:
                        deep_bio = await _extract_bio(web_page, bio_page_url)
                        if deep_bio:
                            bio = deep_bio
                            agency_bio_url = bio_page_url
                            if _DB_AVAILABLE and _upsert_agency_bio:
                                _upsert_agency_bio(agent["qt_path"], bio_page_url, bio)
                            print(f"    Agency page: {bio_page_url}", file=sys.stderr)
                    await web_page.close()
                except Exception as e:
                    print(f"    Agency website error: {e}", file=sys.stderr)

        # Pull any stored enrichment from DB for multi-agent runs
        agency_bio_stored = bio
        if _DB_AVAILABLE and _db_connect:
            try:
                con = _db_connect()
                row = con.execute(
                    "SELECT mswl_text, mswl_url, agency_bio, agency_bio_url FROM agents WHERE qt_path = ?",
                    (agent["qt_path"],)
                ).fetchone()
                con.close()
                if row:
                    if not mswl_text and row["mswl_text"]:
                        mswl_text = row["mswl_text"]
                    if not mswl_url and row["mswl_url"]:
                        mswl_url = row["mswl_url"]
                    if not bio and row["agency_bio"]:
                        agency_bio_stored = row["agency_bio"]
                    if not agency_bio_url and row["agency_bio_url"]:
                        agency_bio_url = row["agency_bio_url"]
            except Exception:
                pass

        # Scrape Data Explorer — Partial/Full requests only — for genre + word count stats
        data_stats: dict = {"genre_counts": {}, "wordcount_counts": {}, "total_requests": 0}
        print(f"    Scraping Data Explorer (request stats)...", file=sys.stderr)
        try:
            data_page = await context.new_page()
            data_stats = await get_agent_data_stats(data_page, agent["qt_path"])
            await data_page.close()
        except Exception as e:
            print(f"    Data Explorer error: {e}", file=sys.stderr)

        profiles.append({
            **agent, **qt,
            "submission":      submission,
            "mswl_file":       mswl_file,
            "bio":             agency_bio_stored,
            "agency_bio_url":  agency_bio_url,
            "mswl_text":       mswl_text,
            "mswl_url":        mswl_url,
            "genre_counts":    data_stats["genre_counts"],
            "wordcount_counts": data_stats["wordcount_counts"],
        })

    return profiles


def format_profiles(profiles):
    out = []
    for p in profiles:
        out.append(f"{'='*70}")
        out.append(f"{p['name']}  —  {p['agency']}")
        out.append(f"QueryTracker: {p['url']}")
        out.append("")
        out.append(f"  Status:             {p['open_closed']}")
        out.append(f"  Query Method:       {p['query_method']}")
        out.append(f"  Reply Rate:         {p['reply_rate']}")
        out.append(f"  Request Rate:       {p['request_rate']}")
        out.append(f"  Subs Reply Rate:    {p['subs_reply_rate']}")
        out.append(f"  Last Reply:         {p['last_reply']}")
        out.append(f"  Last Request:       {p['last_request']}")
        out.append(f"  Avg Request Time:   {p['avg_request_time']}")
        out.append(f"  Avg Reject Time:    {p['avg_reject_time']}")
        out.append(f"  Email:              {p['email']}")
        out.append(f"  Website:            {p['website']}")
        out.append(f"  Twitter:            {p['twitter']}")
        if p.get("genres"):
            out.append(f"  Genres:             {p['genres']}")

        clients = p.get("clients", [])
        if clients:
            out.append("")
            out.append("  --- Recent Clients & Books ---")
            for c in clients[:10]:
                out.append(f"  • {c['name']}")
                out.append(f"    {c['amazon_url']}")

        report = p.get("report_12mo", "")
        if report:
            out.append("")
            out.append("  --- 12-Month Query Report (Queries Sent/Replied) ---")
            for line in report.splitlines():
                out.append(f"  {line}")

        submission = p.get("submission", "")
        if submission:
            out.append("")
            mswl_file = p.get("mswl_file")
            header = "  --- Submission Form / MSWL ---"
            if mswl_file:
                header += f"  [saved: {mswl_file}]"
            out.append(header)
            for line in submission.splitlines()[:30]:
                out.append(f"  {line}")

        bio = p.get("bio", "")
        if bio:
            out.append("")
            out.append("  --- Agency/Agent Bio ---")
            for line in bio.splitlines()[:15]:
                out.append(f"  {line}")

        out.append("")
    return "\n".join(out)


def format_results(results, query_desc):
    if not results:
        return f"No agents found for: {query_desc}"

    lines = [f"QueryTracker Agent Search — {query_desc}", f"{len(results)} result(s)\n"]
    lines.append(f"{'Agent':<35} {'Agency':<40}")
    lines.append("-" * 77)
    for r in results:
        lines.append(f"{r['name']:<35} {r['agency']:<40}")
        lines.append(f"  {r['url']}")
    return "\n".join(lines)


def format_discord_profiles(profiles):
    """Format profiles as Discord markdown (readable, not code blocks)."""
    messages = []
    for p in profiles:
        out = []
        status_emoji = "🟢" if p["open_closed"] == "Open" else "🔴"

        # Query-fit assessment (prepended if available)
        summary = p.get("agent_summary", "").strip()
        if summary:
            out.append("📋 **QUERY FIT ASSESSMENT**")
            out.append(summary)
            out.append("")
            out.append("─────────────────────────────────────")
            out.append("")

        # Header
        out.append(f"**{p['name']}** — {p['agency']}")
        out.append(f"<{p['url']}>")
        if p.get("mswl_url"):
            out.append(f"<{p['mswl_url']}>")
        if p.get("agency_bio_url"):
            out.append(f"<{p['agency_bio_url']}>")
        out.append("")

        # Status + method
        out.append(f"{status_emoji} **{p['open_closed']}**  |  {p['query_method']}")

        # Stats row
        stats = []
        if p["reply_rate"]:    stats.append(f"Reply **{p['reply_rate']}**")
        if p["request_rate"]:  stats.append(f"Request **{p['request_rate']}**")
        if p["subs_reply_rate"]: stats.append(f"Subs **{p['subs_reply_rate']}**")
        if stats:
            out.append(" | ".join(stats))

        # Timing row
        times = []
        if p["last_reply"]:        times.append(f"Last Reply: {p['last_reply']}")
        if p["last_request"]:      times.append(f"Last Request: {p['last_request']}")
        if p["avg_request_time"]:  times.append(f"Avg Request: {p['avg_request_time']}")
        if p["avg_reject_time"]:   times.append(f"Avg Reject: {p['avg_reject_time']}")
        if times:
            out.append(" | ".join(times))

        # Contact
        contact = []
        if p["email"]:   contact.append(f"📧 {p['email']}")
        if p["website"]: contact.append(f"🌐 {p['website']}")
        if p["twitter"]: contact.append(f"🐦 {p['twitter']}")
        if contact:
            out.append("  ".join(contact))

        # Genres (trimmed)
        if p.get("genres"):
            out.append(f"\n**Genres:** {p['genres']}")

        # Clients
        clients = p.get("clients", [])
        if clients:
            out.append("\n**📚 Clients:**")
            for c in clients[:10]:
                out.append(f"• [{c['name']}](<{c['amazon_url']}>)")

        # 12-month report as markdown table
        report = p.get("report_12mo", "")
        if report:
            out.append("\n**📊 12-Month Query Report:**")
            rlines = [l for l in report.splitlines() if l.strip()]
            # rlines[0] is our Via: header, rlines[1:] are data rows (double-space separated)
            table = ["| | Postal | Email | Form | QM | **Total** |",
                     "|---|:---:|:---:|:---:|:---:|:---:|"]
            for line in rlines[1:]:
                parts = [part.strip() for part in line.split("  ") if part.strip()]
                if len(parts) >= 2:
                    label = parts[0].replace("\xa0", " ")
                    vals  = parts[1:]
                    while len(vals) < 5:
                        vals.append("—")
                    table.append(f"| {label} | " + " | ".join(vals[:5]) + " |")
            out.append("\n".join(table))

        # MSWL preview (first ~800 chars, full content saved to file)
        submission = p.get("submission", "")
        mswl_file = p.get("mswl_file", "")
        if submission:
            file_note = f" *(full text saved to `{mswl_file}`)*" if mswl_file else ""
            out.append(f"\n**📋 Submission / MSWL**{file_note}")
            preview_lines = []
            for line in submission.splitlines():
                if line.startswith("\n[") or line.startswith("["):
                    preview_lines.append(f"**{line.strip()}**")
                else:
                    preview_lines.append(line)
                if sum(len(l) for l in preview_lines) > 800:
                    preview_lines.append("*…(see saved file for full text)*")
                    break
            out.extend(preview_lines)

        out.append("\n─────────────────────────────────────")
        messages.append("\n".join(out))

    return messages


def format_discord_results(results, query_desc):
    """Format search results as Discord markdown."""
    if not results:
        return [f"No agents found for: {query_desc}"]
    lines = [f"**QueryTracker Search** — {query_desc}", f"*{len(results)} result(s)*", ""]
    for r in results:
        lines.append(f"**{r['name']}** — {r['agency']}")
        lines.append(f"<{r['url']}>")
    return ["\n".join(lines)]


async def get_agent_detail(page, path):
    url = f"{BASE_URL}{path}" if path.startswith("/") else path
    await page.goto(url, wait_until="domcontentloaded")
    body = await page.inner_text("body")
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    return "\n".join(lines[:80])


async def main():
    parsed = _parse_args(sys.argv[1:])
    extra  = parsed["extra"]

    name      = parsed["name"]
    detail    = parsed["qt_path"]
    limit     = parsed["limit"] or 25
    file_path = parsed["file_path"]

    genre          = None
    profiles       = "--profiles" in extra or bool(file_path)
    open_only      = "--include-closed" not in extra
    default_genres = True
    discord_fmt    = "--discord" in extra

    # Handle --genre value (still in extra as ["--genre", "Romance"])
    for i, tok in enumerate(extra):
        if tok == "--genre" and i + 1 < len(extra):
            genre = extra[i + 1]
            default_genres = False
            break
    if "--all-genres" in extra:
        default_genres = False

    if not any([name, genre, detail, profiles, default_genres, file_path]):
        print(__doc__)
        sys.exit(0)

    async with async_playwright() as p:
        browser, context, page = await get_context(p)

        if detail:
            result = await get_agent_detail(page, detail)
            print(result)
        elif profiles:
            if file_path:
                import os
                fpath = os.path.expanduser(file_path)
                with open(fpath) as fh:
                    paths = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
                agents = [{"qt_path": p} for p in paths]
                print(f"Loading {len(agents)} agents from {fpath}", file=sys.stderr)
            else:
                query_desc = " | ".join(filter(None, [
                    f"name={name!r}" if name else None,
                    f"genre={genre!r}" if genre else None,
                ]))
                print(f"Searching: {query_desc}", file=sys.stderr)
                agents = await search_agents(page, name=name, genre=genre, limit=limit,
                                             open_only=open_only, default_genres=default_genres)
            if not agents:
                print("No agents found.")
            else:
                print(f"Found {len(agents)} agents, fetching profiles...", file=sys.stderr)
                agent_profiles = await build_profiles(context, page, agents)
                if _DB_AVAILABLE:
                    for prof in agent_profiles:
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
                            "genre_counts":     json.dumps(gc)  if gc  else None,
                            "wordcount_counts": json.dumps(wcc) if wcc else None,
                            "data_scraped":          datetime.now().isoformat(timespec="seconds") if (gc or wcc) else None,
                        }
                        _upsert_agent(db_prof)
                    print(f"Saved {len(agent_profiles)} agent(s) to DB.", file=sys.stderr)

                # Generate AI query-fit summaries for Discord output
                if discord_fmt and _DB_AVAILABLE and _generate_summary:
                    print("Generating query-fit summaries...", file=sys.stderr)
                    for prof in agent_profiles:
                        summary = _generate_summary(prof)
                        prof["agent_summary"] = summary

                if discord_fmt:
                    # Print each profile separated by a sentinel the bot can split on
                    messages = format_discord_profiles(agent_profiles)
                    print("\x00MSG\x00".join(messages))
                else:
                    print(format_profiles(agent_profiles))
        else:
            query_desc = " | ".join(filter(None, [
                f"name={name!r}" if name else None,
                f"genre={genre!r}" if genre else None,
            ]))
            results = await search_agents(page, name=name, genre=genre, limit=limit,
                                          open_only=open_only, default_genres=default_genres)
            if discord_fmt:
                messages = format_discord_results(results, query_desc)
                print("\x00MSG\x00".join(messages))
            else:
                print(format_results(results, query_desc))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
