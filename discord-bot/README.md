# discord-bot

Discord bot that provides a chat interface to the querytracker system. All major agent research operations can be triggered via slash-style commands from any configured channel.

## Commands

### Agent Research
| Command | Description |
|---------|-------------|
| `/qt [args]` | Search QueryTracker agents by name, genre, or get full profiles |
| `/agents <question>` | Natural language query against the agent database via Claude |
| `/rank` | Show scored and ranked agents (top 3 per firm) |
| `/score [--force]` | Re-score all agents with keyword + semantic analysis |
| `/refresh [args]` | Refresh open/closed status and query rates from QueryTracker |
| `/discover [args]` | Run the full cross-source agent discovery pipeline |

### Agent Enrichment
| Command | Description |
|---------|-------------|
| `/pm [args]` | Look up Publishers Marketplace deal history for an agent |
| `/mswl [args]` | Look up an agent's Manuscript Wishlist entry |
| `/website [args]` | Scrape an agent's bio from their agency website |

### Query Letter
| Command | Description |
|---------|-------------|
| `/synopsis [text]` | Get or set your synopsis |
| `/bio [text]` | Get or set your author bio |
| `/hook [text]` | Get or set your hook line (250 char limit) |
| `/comps [text]` | Get or set your comp titles |
| `/genre [text]` | Get or set your genre |
| `/wordcount [N]` | Get or set your word count |
| `/series [text]` | Get or set series info |
| `/publications [text]` | Get or set publication credits |
| `/keywords [text]` | Get or set the keyword list used for scoring |
| `/para [text]` | Get or set the query paragraph used for semantic scoring |

### Data & Scoring
| Command | Description |
|---------|-------------|
| `/export [--format csv\|json] [--open]` | Export agent data as a file attachment |

### Other
| Command | Description |
|---------|-------------|
| `/wx <ICAO>` | Fetch aviation weather METAR/TAF |

## Special Channels

- **#agenthunt** — every message is automatically treated as a `/qt` command (no prefix needed)

## Agent Lookup Flags

For `/pm`, `/mswl`, `/website`, and `/refresh`:
```
--name "First Last"     target a specific agent by name
--agent /agent/NNN      target a specific agent by QT path
--all-open [--limit N]  run on all open agents (optionally capped)
--force                 re-scrape even if cached data exists
```

## Setup

### Credentials
```
~/.discord_token        Discord bot token (one line)
~/.querytracker_creds   QueryTracker login (username line 1, password line 2)
~/.pm_creds             Publishers Marketplace login (username / password)
~/.anthropic_api_key    Claude API key
```

### Systemd Service
The bot runs as a user systemd service:
```bash
systemctl --user start discord-bot.service
systemctl --user status discord-bot.service
systemctl --user restart discord-bot.service
```

### Outbox Queue
External scripts can post to Discord by writing lines to `~/discord-bot/outbox.queue`:
```
general|Your message here
updates|Another message
```
Channel prefix is optional — defaults to `#general`.

## Files

```
bot.py          Main bot — command handlers and Discord event loop
monitor.py      Watches inbox.log and responds to messages
read.py         Read utility for inbox
send.py         Send utility for outbox
inbox.log       Incoming messages log (gitignored)
outbox.queue    Outgoing message queue (gitignored)
```
