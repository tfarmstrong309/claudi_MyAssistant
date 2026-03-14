# Claudi — My Assistant

A personal AI-assisted system for researching and tracking literary agent queries. Built around a SQLite database of agents scraped from QueryTracker, Publishers Marketplace, and Manuscript Wishlist, with a Discord bot as the primary interface.

## Components

### [`querytracker/`](querytracker/)
The core scraping and database engine. Collects agent data from multiple sources, scores agents for fit, and exposes everything via a command-line interface.

### [`discord-bot/`](discord-bot/)
A Discord bot that acts as a chat interface to the querytracker system. All major operations (searching agents, checking scores, looking up MSWL entries, refreshing statuses) can be triggered from Discord via slash-style commands.

## Architecture

```
QueryTracker.net ──┐
manuscriptwishlist.com ──┤──► querytracker/ ──► agents.db ──► db.py CLI
Publishers Marketplace ──┘                                        │
                                                                  ▼
                                                          discord-bot/bot.py
```

## Credentials Required

| File | Purpose |
|------|---------|
| `~/.querytracker_creds` | QueryTracker login (username / password) |
| `~/.pm_creds` | Publishers Marketplace login |
| `~/.anthropic_api_key` | Claude API key (for semantic scoring and NL queries) |
| `~/.discord_token` | Discord bot token |

## Dependencies

```bash
pip install playwright discord.py anthropic curl_cffi
playwright install chromium
```
