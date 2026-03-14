# querytracker

Scrapes agent data from QueryTracker, Manuscript Wishlist, and Publishers Marketplace into a local SQLite database (`agents.db`), then scores and ranks agents for fit using keyword matching and Claude semantic analysis.

## Scripts

| Script | Purpose |
|--------|---------|
| `qt.py` | Main QueryTracker scraper — fetches agent profiles, stats, Data Explorer genre/wordcount data |
| `db.py` | Database CLI — query, score, rank, manage submissions, export data |
| `common.py` | Shared utilities (import-only) — arg parsing, DB lookup, text cleanup, browser helpers |
| `discover_agents.py` | Cross-source discovery pipeline — finds new agents by cross-referencing QT, MSWL, and PM |
| `mswl_lookup.py` | Scrapes individual agent entries from manuscriptwishlist.com |
| `mswl_search.py` | Searches manuscriptwishlist.com by genre to find new agents |
| `agent_website.py` | Scrapes agent bios from their agency websites |
| `pm_lookup.py` | Fetches agent deal history from Publishers Marketplace |
| `pm_search.py` | Searches Publishers Marketplace for agents by category/year |
| `status_refresh.py` | Quick refresh of open/closed status and query rates without full re-scrape |

## Common CLI Flags

Most scraper scripts accept these flags (parsed by `common.parse_args`):

```
--agent /agent/NNN      target a single agent by QT path
--name "First Last"     target by name (LIKE match)
--all-open              run on all open agents in the DB
--limit N               cap results at N agents
--force                 re-scrape even if data exists
--dry-run               print what would happen without writing to DB
--file path.txt         read agent paths from a file (one /agent/NNN per line)
```

## db.py Commands

```
agents                  list all agents in the DB
rank [--limit N]        show scored/ranked agents (top 3 per firm)
score [--force]         score all agents (keyword 40% + Claude semantic 60%)
keywords get|set        view or update the keyword list used for scoring
para get|set            view or update the query paragraph for semantic scoring
history /agent/NNN      full timeline for one agent (profile, submissions, notes)
export [--format csv|json] [--open] [--output path]
                        export agent metadata to file
submit /agent/NNN --method QueryManager
                        record a query submission
status /agent/NNN pending|rejected|full_request|partial_request|offered|withdrawn
                        update submission status
note /agent/NNN "text"  add a note to an agent record
boilerplate <field> [value]
                        get/set query letter boilerplate fields:
                        synopsis, bio, hook, comps, genre, wordcount, series, publications
ask <question>          natural language DB query via Claude API
```

## Scoring

Agents are scored on a 0–10 scale combining:
- **Keyword match (40%)** — presence of target keywords in MSWL/bio/website text
- **Claude semantic fit (60%)** — Claude Haiku rates the agent against your query paragraph

The `firm_rank` column enforces a one-query-per-firm rule; only the top-ranked agent at each agency is eligible for querying.

## Data Sources

- **QueryTracker** (`qt.py`) — stats, reply rates, genre/wordcount counts via Data Explorer
- **Manuscript Wishlist** (`mswl_lookup.py`, `mswl_search.py`) — what each agent is seeking
- **Agency websites** (`agent_website.py`) — agent bios from their own agency pages
- **Publishers Marketplace** (`pm_lookup.py`, `pm_search.py`) — recent deal history

## Files

```
agents.db           SQLite database (gitignored)
my_agents.txt       curated list of agent QT paths (one per line)
mswl/               saved MSWL/submission form files (gitignored)
common.py           shared utilities — import this, don't run it directly
```
