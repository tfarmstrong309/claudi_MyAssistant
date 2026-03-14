import discord
import asyncio
import pathlib
import shlex
import sys

sys.path.insert(0, str(pathlib.Path.home() / "querytracker"))
from common import discord_chunks

_token_path = pathlib.Path.home() / ".discord_token"
if not _token_path.exists():
    raise SystemExit(f"ERROR: Discord token file not found: {_token_path}")
TOKEN = _token_path.read_text().strip()

CHANNELS = {
    "general":   1474885371916587223,
    "updates":   1474906208593776773,
    "logs":      1474906273823850548,
    "tasks":     1474906312478298126,
    "agenthunt": 1475195593432236163,
}

BASE = pathlib.Path.home() / "discord-bot"
INBOX   = BASE / "inbox.log"
OUTBOX  = BASE / "outbox.queue"

INBOX.touch(exist_ok=True)
OUTBOX.touch(exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# --- Command handlers ---

async def handle_wx(message, args, decoded=False):
    """Fetch and post weather for given airport identifier(s)."""
    if not args:
        await message.channel.send("Usage: `/wx <ICAO or airport name>` — e.g. `/wx KJFK` or `/wx JFK`")
        return

    cmd = ["python3", str(pathlib.Path.home() / "wx" / "wx.py")]
    if decoded:
        cmd.append("--decoded")
    cmd.extend(shlex.split(args))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        output = (stdout.decode().strip() or stderr.decode().strip() or "No data returned.")
    except asyncio.TimeoutError:
        output = "Request timed out."
    except Exception as e:
        output = f"Error: {e}"

    for chunk in discord_chunks(output, code_block=True):
        await message.channel.send(chunk)

async def handle_qt(message, args, **_):
    """Search QueryTracker agents."""
    if not args:
        await message.channel.send(
            "Usage: `/qt [--name <name>] [--genre <genre>] [--profiles] [--limit N] [--agent /agent/ID]`\n"
            "Defaults: open agents only, genres Women's Fiction / Romance / Historical\n"
            "Flags: `--include-closed` `--all-genres`"
        )
        return

    # Always request Discord markdown format from qt.py
    cmd = ["python3", str(pathlib.Path.home() / "querytracker" / "qt.py"), "--discord"] + shlex.split(args)

    await message.channel.send("🔍 Searching QueryTracker… (this may take a minute)")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        timeout = 300 if "--profiles" in args else 60
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode().strip()
        if not output:
            output = stderr.decode().strip() or "No results."
    except asyncio.TimeoutError:
        await message.channel.send("Search timed out. Try `--limit 3` or fewer agents.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return

    # qt.py separates per-agent messages with \x00MSG\x00
    # Send each as its own Discord message, splitting further if over 1900 chars
    sections = output.split("\x00MSG\x00")
    for section in sections:
        section = section.strip()
        if not section:
            continue
        for chunk in discord_chunks(section):
            await message.channel.send(chunk)


async def handle_agents(message, args, **_):
    """Query the agent database with natural language via Claude API."""
    if not args:
        await message.channel.send(
            "Usage: `/agents <question>` — e.g. `/agents which open agents in Historical haven't been queried?`\n"
            "Or manage submissions: `/agents submit /agent/ID --method QueryManager`\n"
            "Status values: `pending` `rejected` `full_request` `partial_request` `offered` `withdrawn`\n"
            "Commands: `submit`, `status`, `note`, `submissions`, `query`"
        )
        return

    # Pass through raw args to db.py — supports both 'ask' (default) and subcommands
    db = str(pathlib.Path.home() / "querytracker" / "db.py")
    first = args.split()[0] if args else ""
    known_cmds = {
        "submit", "status", "note", "submissions", "query", "agents", "ask",
        "score", "rank", "keywords", "para", "boilerplate", "history", "export",
    }
    if first in known_cmds:
        cmd = ["python3", db] + shlex.split(args)
    else:
        # Free-text question → natural language ask
        cmd = ["python3", db, "ask", args]

    await message.channel.send("🗄️ Querying agent database…")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode().strip() or stderr.decode().strip() or "No results."
    except asyncio.TimeoutError:
        await message.channel.send("Query timed out.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return

    for chunk in discord_chunks(output, code_block=True):
        await message.channel.send(chunk)


_DB = str(pathlib.Path.home() / "querytracker" / "db.py")


async def _run_db(message, cmd_args: list, timeout: int = 30, code_block: bool = True):
    """Run a db.py subcommand and post the output to Discord."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", _DB, *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode().strip() or stderr.decode().strip() or "(no output)"
    except asyncio.TimeoutError:
        await message.channel.send("Timed out.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return

    for chunk in discord_chunks(output, code_block=code_block):
        await message.channel.send(chunk)


async def handle_rank(message, args, **_):
    """Show scored and ranked agents (top 3 per firm)."""
    extra = shlex.split(args) if args else []
    await _run_db(message, ["rank"] + extra)


async def handle_score(message, args, **_):
    """Score / rescore all agents with keyword + Claude semantic scoring."""
    await message.channel.send("⏳ Scoring agents… (may take a few minutes)")
    extra = ["--force"] if "--force" in args else []
    await _run_db(message, ["score"] + extra, timeout=300)


async def handle_keywords(message, args, **_):
    """Get or set the keyword list used for scoring."""
    if not args:
        await _run_db(message, ["keywords", "get"])
    else:
        await _run_db(message, ["keywords", "set", args])


async def handle_para(message, args, **_):
    """Get or set the query paragraph used for semantic scoring."""
    if not args:
        await _run_db(message, ["para", "get"], code_block=False)
    else:
        await _run_db(message, ["para", "set", args])


async def handle_pm(message, args, **_):
    """Look up Publishers Marketplace profiles for agents."""
    pm_script = str(pathlib.Path.home() / "querytracker" / "pm_lookup.py")
    if not args:
        await message.channel.send(
            "Usage: `/pm --name \"Lori Galvin\"` | `/pm --agent /agent/7674` | "
            "`/pm --all-open [--limit N]` | add `--force` to re-scrape"
        )
        return
    extra = shlex.split(args)
    cmd = ["python3", pm_script] + extra
    await message.channel.send("🔍 Fetching Publishers Marketplace data…")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        output = stdout.decode().strip() or stderr.decode().strip() or "(no output)"
    except asyncio.TimeoutError:
        await message.channel.send("PM lookup timed out.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return
    for chunk in discord_chunks(output, code_block=True):
        await message.channel.send(chunk)


async def handle_mswl(message, args, **_):
    """Look up an agent's Manuscript Wishlist entry."""
    script = str(pathlib.Path.home() / "querytracker" / "mswl_lookup.py")
    if not args:
        await message.channel.send(
            "Usage: `/mswl --name \"Agent Name\"` | `/mswl --agent /agent/ID` | "
            "`/mswl --all-open [--limit N]` | add `--force` to re-scrape"
        )
        return
    cmd = ["python3", script] + shlex.split(args)
    await message.channel.send("🔍 Looking up MSWL…")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode().strip() or stderr.decode().strip() or "(no output)"
    except asyncio.TimeoutError:
        await message.channel.send("MSWL lookup timed out.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return
    for chunk in discord_chunks(output, code_block=True):
        await message.channel.send(chunk)


async def handle_website(message, args, **_):
    """Look up an agent's bio on their agency website."""
    script = str(pathlib.Path.home() / "querytracker" / "agent_website.py")
    if not args:
        await message.channel.send(
            "Usage: `/website --name \"Agent Name\"` | `/website --agent /agent/ID` | "
            "`/website --all-open [--limit N]` | add `--force` to re-scrape"
        )
        return
    cmd = ["python3", script] + shlex.split(args)
    await message.channel.send("🌐 Fetching agency website bio…")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode().strip() or stderr.decode().strip() or "(no output)"
    except asyncio.TimeoutError:
        await message.channel.send("Website lookup timed out.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return
    for chunk in discord_chunks(output, code_block=True):
        await message.channel.send(chunk)


async def handle_discover(message, args, **_):
    """Run the cross-source agent discovery pipeline (QT + MSWL + PM)."""
    script = str(pathlib.Path.home() / "querytracker" / "discover_agents.py")
    await message.channel.send(
        "🔭 Starting discovery pipeline… (this takes several minutes)"
    )
    cmd = ["python3", script] + (shlex.split(args) if args else [])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        output = stdout.decode().strip() or stderr.decode().strip() or "(no output)"
    except asyncio.TimeoutError:
        await message.channel.send("Discovery pipeline timed out.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return
    for chunk in discord_chunks(output):
        await message.channel.send(chunk)


async def handle_refresh(message, args, **_):
    """Refresh agent open/closed status and query rates from QueryTracker."""
    script = str(pathlib.Path.home() / "querytracker" / "status_refresh.py")
    await message.channel.send("🔄 Refreshing agent statuses…")
    cmd = ["python3", script] + (shlex.split(args) if args else [])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        output = stdout.decode().strip() or stderr.decode().strip() or "(no output)"
    except asyncio.TimeoutError:
        await message.channel.send("Status refresh timed out.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return
    for chunk in discord_chunks(output, code_block=True):
        await message.channel.send(chunk)


async def handle_export(message, args, **_):
    """Export agent data to CSV or JSON and send as a file attachment."""
    extra = shlex.split(args) if args else []
    _DB  = str(pathlib.Path.home() / "querytracker" / "db.py")
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", _DB, "export", *extra,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode().strip()
        if not output:
            await message.channel.send(stderr.decode().strip() or "Export failed.")
            return
    except asyncio.TimeoutError:
        await message.channel.send("Export timed out.")
        return
    except Exception as e:
        await message.channel.send(f"Error: {e}")
        return

    # Last line is "Exported N agents to: /path/to/file"
    last_line = output.splitlines()[-1]
    if "Exported" in last_line and "to:" in last_line:
        file_path = last_line.split("to:", 1)[1].strip()
        try:
            await message.channel.send(
                content=last_line,
                file=discord.File(file_path)
            )
        except Exception as e:
            await message.channel.send(f"{last_line}\n(Could not attach file: {e})")
    else:
        await message.channel.send(output)


_BOILERPLATE_FIELDS = ["synopsis", "bio", "comps", "hook", "wordcount", "genre", "series", "publications"]


def make_boilerplate_handler(field: str):
    """Return a Discord handler that gets or sets one boilerplate field."""
    async def handler(message, args, **_):
        if not args:
            await _run_db(message, ["boilerplate", field], code_block=False)
        else:
            await _run_db(message, ["boilerplate", field, args])
    handler.__name__ = f"handle_{field}"
    return handler


COMMANDS = {
    "/wx":       handle_wx,
    "/qt":       handle_qt,
    "/agents":   handle_agents,
    "/rank":     handle_rank,
    "/score":    handle_score,
    "/keywords": handle_keywords,
    "/para":     handle_para,
    "/pm":       handle_pm,
    "/mswl":     handle_mswl,
    "/website":  handle_website,
    "/discover": handle_discover,
    "/refresh":  handle_refresh,
    "/export":   handle_export,
    **{f"/{f}": make_boilerplate_handler(f) for f in _BOILERPLATE_FIELDS},
}

async def dispatch_command(message):
    """Check if message is a bot command and handle it."""
    content = message.content.strip()

    # In #agenthunt, every message is implicitly a /qt command
    if message.channel.id == CHANNELS["agenthunt"] and not content.lower().startswith("/"):
        args = content
        decoded = "--translate" in args or "--decoded" in args
        args = args.replace("--translate", "").replace("--decoded", "").strip()
        await handle_qt(message, args, decoded=decoded)
        return True

    for cmd, handler in COMMANDS.items():
        if content.lower().startswith(cmd):
            args = content[len(cmd):].strip()
            decoded = "--translate" in args or "--decoded" in args
            args = args.replace("--translate", "").replace("--decoded", "").strip()
            await handler(message, args, decoded=decoded)
            return True
    return False

# --- Outbox watcher ---

async def watch_outbox():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            lines = OUTBOX.read_text().splitlines()
            if lines:
                OUTBOX.write_text("")
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if "|" in line and line.split("|")[0] in CHANNELS:
                        channel_name, msg = line.split("|", 1)
                    else:
                        channel_name, msg = "general", line
                    channel = client.get_channel(CHANNELS[channel_name])
                    if channel:
                        await channel.send(msg)
                    await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Outbox error: {e}")
        await asyncio.sleep(2)

# --- Events ---

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    channel = client.get_channel(CHANNELS["logs"])
    if channel:
        await channel.send("Bot online and ready.")

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.channel.id not in CHANNELS.values():
        return

    # Try to handle as a command first.
    # #agenthunt routes all messages (no "/" prefix required) so always dispatch there.
    if message.content.startswith("/") or message.channel.id == CHANNELS["agenthunt"]:
        handled = await dispatch_command(message)
        if handled:
            return

    # Log to inbox
    channel_name = next((k for k, v in CHANNELS.items() if v == message.channel.id), "unknown")
    entry = f"[{message.created_at.strftime('%Y-%m-%d %H:%M:%S')}] #{channel_name} {message.author.name}: {message.content}\n"
    with open(INBOX, "a") as f:
        f.write(entry)
    print(entry.strip())

async def main():
    async with client:
        asyncio.ensure_future(watch_outbox())
        await client.start(TOKEN)

asyncio.run(main())
