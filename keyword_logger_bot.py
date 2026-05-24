print("DEBUG: script started")

import os
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import discord
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

# ====== TIMEZONE ======

try:
    from zoneinfo import ZoneInfo
    try:
        TORONTO_TZ = ZoneInfo("America/Toronto")
    except Exception:
        TORONTO_TZ = timezone.utc
except Exception:
    TORONTO_TZ = timezone.utc

# ====== ENVIRONMENT VARIABLES ======

TOKEN = os.environ["DISCORD_TOKEN"]
MONGODB_URI = os.environ["MONGODB_URI"]
PORT = int(os.environ.get("PORT", 10000))

# ====== CONFIG ======

WATCHED_CHANNEL_IDS = [
    1439856570061033565,
    1462285627000094896,
    1477839908562407485,
]

LIVE_LOG_CHANNEL_ID = 1459297868861931715
WEEKLY_RECAP_CHANNEL_ID = 1507958570640216245

KEYWORDS = [
    "close", "closed", "closing",
    "sell", "selling", "sold",
    "cut", "cutting",
    "exit", "exited", "out",
    "scale out", "scaled out",
    "reduce", "reduced",
    "trim", "trimmed", "trimming",
    "all out", "flat",
    "invalidation", "invalid", "invalidated",
    "kill", "killed",
    "stopped", "stopped out", "stop loss", "sl hit",
    "take profit", "taking profit",
    "tp", "tp1", "tp 1", "tp2", "tp 2", "tp3", "tp 3",
    "tp hit", "tp1 hit", "tp2 hit", "tp3 hit",
    "first trim", "second trim", "third trim",
    "profit", "profits", "winner", "win", "wins",
    "green", "green day", "gains", "gain", "gaining", "in the green",
    "loss", "losses", "loser", "red", "red day",
    "down", "small loss", "slight loss",
    "up", "ripped", "pumped", "mooning", "spike", "runner", "flush", "dumped",
]

PARTIAL_TAGS = ["(partial)", "[partial]", "partial", "trim", "trimmed", "trimming", "haircut"]
FULL_EXIT_TAGS = ["(full)", "[full]", "full", "all out", "closed", "sold the rest", "flat"]
TP1_TAGS = ["tp1", "tp 1", "first trim", "first tp"]
TP2_TAGS = ["tp2", "tp 2", "second trim", "second tp"]
TP3_TAGS = ["tp3", "tp 3", "third trim", "third tp"]
DAYTRADE_TAGS = ["daytrade", "day trade", "intraday", "scalp"]
SWING_TAGS = ["swing", "swing trade"]

last_weekly_recap_key = None

# ====== HTTP HEALTH SERVER FOR RENDER ======

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/", "/health"]:
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is running")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Health server running on port {PORT}")
    server.serve_forever()

# ====== DISCORD & MONGO ======

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client["TradeRecap"]
trades_col = db["trades"]

# ====== EVENTS ======

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

    try:
        await trades_col.create_index("message_id", unique=True)
        await trades_col.create_index("timestamp")
        print("Mongo indexes ensured")
    except Exception as e:
        print(f"Index creation warning: {e}")

    if not weekly_recap_loop.is_running():
        weekly_recap_loop.start()

# ====== HELPERS ======

def classify_exit_type(text: str) -> str:
    t = text.lower()

    if any(tag in t for tag in PARTIAL_TAGS):
        exit_type = "Partial trim"
    elif any(tag in t for tag in FULL_EXIT_TAGS):
        exit_type = "Full exit"
    else:
        exit_type = "Exit"

    if any(tag in t for tag in TP3_TAGS):
        tp = "Take profit level 3"
    elif any(tag in t for tag in TP2_TAGS):
        tp = "Take profit level 2"
    elif any(tag in t for tag in TP1_TAGS):
        tp = "Take profit level 1"
    elif "take profit" in t or "tp" in t:
        tp = "Take profit"
    else:
        tp = None

    if any(tag in t for tag in DAYTRADE_TAGS):
        style = "Daytrade"
    elif any(tag in t for tag in SWING_TAGS):
        style = "Swing trade"
    else:
        style = None

    parts = [exit_type]
    if tp:
        parts.append(tp)
    if style:
        parts.append(style)

    return " | ".join(parts)

def extract_symbol_and_core_line(text: str):
    patterns = [
        r"([A-Za-z]{1,6}\s+\d{1,6}[CPcp]?)\s*@\s*([\d\.]+)(?:\s+(-?\d+(?:\.\d+)?%))?",
        r"\$?([A-Za-z]{1,6})\b",
    ]

    for p in patterns:
        match = re.search(p, text, re.IGNORECASE)
        if match:
            symbol = match.group(1).upper().strip()
            return symbol, text.strip()

    return None, text.strip()

def parse_explicit_pct(text: str):
    t = text.lower()
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", t)
    if not m:
        return None, None

    pct = float(m.group(1))

    if any(x in t for x in ["loss", "red", "down", "dumped", "flush"]):
        direction = "loss"
        if pct > 0:
            pct = -pct
    elif any(x in t for x in ["profit", "win", "green", "gains", "gain", "gaining", "up", "ripped", "pumped", "mooning"]):
        direction = "profit"
    else:
        direction = None

    return round(pct, 2), direction

def parse_entry_exit_pct(text: str):
    t = text.lower()
    entry_match = re.search(r"entry\s*@?\s*([\d\.]+)", t)
    exit_match = re.search(r"(?:exit|out at|sold at|closed at)\s*@?\s*([\d\.]+)", t)

    if not entry_match or not exit_match:
        return None, None

    try:
        entry = float(entry_match.group(1))
        exit_price = float(exit_match.group(1))
    except ValueError:
        return None, None

    if entry <= 0:
        return None, None

    pct = ((exit_price - entry) / entry) * 100
    direction = "profit" if pct > 0 else "loss" if pct < 0 else "breakeven"
    return round(pct, 2), direction

def infer_result_direction(text: str):
    t = text.lower()
    if any(x in t for x in ["profit", "profits", "win", "winner", "green", "gains", "gain", "gaining", "in the green", "up", "ripped", "pumped", "mooning"]):
        return "profit"
    if any(x in t for x in ["loss", "losses", "loser", "red", "small loss", "slight loss", "down", "dumped", "flush"]):
        return "loss"
    return None

def build_pnl_text(pct, direction):
    if pct is not None and direction:
        if direction == "breakeven" or pct == 0:
            return "⚪ Breakeven (0.00%)", discord.Color.light_grey()
        sign = "+" if pct > 0 else ""
        emoji = "✅" if pct > 0 else "🔴"
        color = discord.Color.green() if pct > 0 else discord.Color.red()
        return f"{emoji} {sign}{pct:.2f}% {direction}", color

    if direction == "profit":
        return "✅ Profit", discord.Color.green()
    if direction == "loss":
        return "🔴 Loss", discord.Color.red()

    return "⚪ Result unknown", discord.Color.light_grey()

def get_today_bounds_local():
    now_local = datetime.now(TORONTO_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local

def get_week_bounds_local():
    now_local = datetime.now(TORONTO_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now_local.weekday())
    end_local = start_local + timedelta(days=7)
    return start_local, end_local

def to_utc_range(start_local: datetime, end_local: datetime):
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

def make_daily_embed(title: str, trades: List[Dict]) -> discord.Embed:
    total_pct = 0.0
    counted = 0
    wins = 0
    losses = 0
    breakevens = 0
    lines = []

    for t in trades:
        pct = t.get("pct")
        symbol = t.get("symbol", "N/A")
        summary = t.get("summary", "N/A")

        if isinstance(pct, (int, float)):
            total_pct += pct
            counted += 1
            if pct > 0:
                wins += 1
            elif pct < 0:
                losses += 1
            else:
                breakevens += 1

        pct_text = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "n/a"
        lines.append(f"• `{symbol}` — {summary} — **{pct_text}**")

    avg_pct = (total_pct / counted) if counted else 0.0
    wl_trades = wins + losses
    winrate = (wins / wl_trades * 100.0) if wl_trades > 0 else 0.0

    embed = discord.Embed(
        title=title,
        color=discord.Color.gold(),
        timestamp=datetime.now(TORONTO_TZ)
    )
    embed.add_field(name="Net PnL", value=f"{total_pct:+.2f}%", inline=True)
    embed.add_field(name="Average per Trade", value=f"{avg_pct:+.2f}%", inline=True)
    embed.add_field(name="Winrate", value=f"{winrate:.2f}% ({wins}W / {losses}L / {breakevens} BE)", inline=True)

    trade_text = "\n".join(lines) if lines else "_No logged trades in this period._"
    if len(trade_text) > 1024:
        trade_text = trade_text[:1000] + "\n…and more"

    embed.add_field(name="Trades", value=trade_text, inline=False)
    embed.set_footer(text="Daily PnL recap")
    return embed

def make_weekly_embed(title: str, trades: List[Dict], start_local: datetime, end_local: datetime) -> discord.Embed:
    total_pct = 0.0
    counted = 0
    wins = 0
    losses = 0
    breakevens = 0
    best_trade = None
    worst_trade = None
    day_pnls: Dict[str, float] = {}
    ticker_pnls: Dict[str, float] = {}
    lines = []

    for t in trades:
        pct = t.get("pct")
        symbol = t.get("symbol", "N/A")
        summary = t.get("summary", "N/A")
        ts = t.get("timestamp")

        if isinstance(pct, (int, float)):
            total_pct += pct
            counted += 1
            if pct > 0:
                wins += 1
            elif pct < 0:
                losses += 1
            else:
                breakevens += 1

            if best_trade is None or pct > best_trade["pct"]:
                best_trade = {"symbol": symbol, "pct": pct}
            if worst_trade is None or pct < worst_trade["pct"]:
                worst_trade = {"symbol": symbol, "pct": pct}

            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts_local = ts.replace(tzinfo=timezone.utc).astimezone(TORONTO_TZ)
                else:
                    ts_local = ts.astimezone(TORONTO_TZ)
                day_key = ts_local.strftime("%a %Y-%m-%d")
                day_pnls[day_key] = day_pnls.get(day_key, 0.0) + pct

            ticker_pnls[symbol] = ticker_pnls.get(symbol, 0.0) + pct

        pct_text = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "n/a"
        lines.append(f"• `{symbol}` — {summary} — **{pct_text}**")

    avg_trade = (total_pct / counted) if counted else 0.0
    avg_daily = (sum(day_pnls.values()) / len(day_pnls)) if day_pnls else 0.0
    wl_trades = wins + losses
    winrate = (wins / wl_trades * 100.0) if wl_trades > 0 else 0.0

    best_day = max(day_pnls.items(), key=lambda x: x[1]) if day_pnls else None
    worst_day = min(day_pnls.items(), key=lambda x: x[1]) if day_pnls else None
    best_ticker = max(ticker_pnls.items(), key=lambda x: x[1]) if ticker_pnls else None
    total_days_with_trades = len(day_pnls)

    embed = discord.Embed(
        title=title,
        description=f"Week: **{start_local.date()}** to **{(end_local - timedelta(days=1)).date()}**",
        color=discord.Color.blue(),
        timestamp=datetime.now(TORONTO_TZ)
    )

    embed.add_field(name="Total PnL", value=f"{total_pct:+.2f}%", inline=True)
    embed.add_field(name="Winrate", value=f"{winrate:.2f}%", inline=True)
    embed.add_field(name="Trades Logged", value=str(len(trades)), inline=True)

    embed.add_field(name="Average Trade", value=f"{avg_trade:+.2f}%", inline=True)
    embed.add_field(name="Average Daily PnL", value=f"{avg_daily:+.2f}%", inline=True)
    embed.add_field(name="Days Traded", value=str(total_days_with_trades), inline=True)

    embed.add_field(
        name="Record",
        value=f"{wins} Wins / {losses} Losses / {breakevens} Breakeven",
        inline=False
    )

    extra_stats = []
    if best_trade:
        extra_stats.append(f"Best trade: `{best_trade['symbol']}` {best_trade['pct']:+.2f}%")
    if worst_trade:
        extra_stats.append(f"Worst trade: `{worst_trade['symbol']}` {worst_trade['pct']:+.2f}%")
    if best_day:
        extra_stats.append(f"Best day: {best_day[0]} {best_day[1]:+.2f}%")
    if worst_day:
        extra_stats.append(f"Worst day: {worst_day[0]} {worst_day[1]:+.2f}%")
    if best_ticker:
        extra_stats.append(f"Top ticker: `{best_ticker[0]}` {best_ticker[1]:+.2f}%")

    if extra_stats:
        embed.add_field(name="Highlights", value="\n".join(extra_stats), inline=False)

    if day_pnls:
        day_lines = [f"{day}: {pnl:+.2f}%" for day, pnl in sorted(day_pnls.items())]
        day_text = "\n".join(day_lines)
        if len(day_text) > 1024:
            day_text = day_text[:1000] + "\n…and more"
        embed.add_field(name="Daily Breakdown", value=day_text, inline=False)

    trade_text = "\n".join(lines) if lines else "_No logged trades in this period._"
    if len(trade_text) > 1024:
        trade_text = trade_text[:1000] + "\n…and more"
    embed.add_field(name="Trades", value=trade_text, inline=False)

    embed.set_footer(text="Weekly PnL recap")
    return embed

async def fetch_trades_between(start_utc: datetime, end_utc: datetime) -> List[Dict]:
    trades: List[Dict] = []
    cursor = trades_col.find(
        {"timestamp": {"$gte": start_utc, "$lt": end_utc}},
        sort=[("timestamp", 1)]
    )
    async for doc in cursor:
        trades.append({
            "message_id": doc.get("message_id"),
            "symbol": doc.get("symbol", "N/A"),
            "summary": doc.get("summary", "N/A"),
            "pct": doc.get("pct"),
            "direction": doc.get("direction"),
            "classification": doc.get("classification"),
            "channel": doc.get("channel", "unknown"),
            "timestamp": doc.get("timestamp"),
            "user_id": doc.get("user_id"),
        })
    return trades

async def send_live_log(message: discord.Message, symbol: Optional[str], core_line: str, classification: str, pnl_text: str, color: discord.Color):
    log_channel = bot.get_channel(LIVE_LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(LIVE_LOG_CHANNEL_ID)
        except Exception:
            print("ERROR: could not fetch live log channel")
            return

    embed = discord.Embed(
        title="📊 Trade Close Logged",
        color=color,
        timestamp=message.created_at,
    )
    embed.add_field(name="Trader", value=message.author.mention, inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)

    if symbol:
        embed.add_field(name="Ticker / Contract", value=f"`{symbol}`", inline=True)

    embed.add_field(name="Summary", value=f"`{core_line[:1000]}`", inline=False)
    embed.add_field(name="Classification", value=classification or "n/a", inline=False)
    embed.add_field(name="PnL", value=pnl_text, inline=False)

    if message.guild:
        msg_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
        embed.add_field(name="Jump to Message", value=f"[Open in Discord]({msg_link})", inline=False)

    original_preview = message.content[:180] if message.content else "(no text content)"
    embed.set_footer(text=f"Original: {original_preview}")

    await log_channel.send(embed=embed)

# ====== MESSAGE LISTENER ======

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    try:
        if message.channel.id in WATCHED_CHANNEL_IDS:
            content = (message.content or "").strip()
            content_lower = content.lower()

            if content and any(keyword in content_lower for keyword in KEYWORDS):
                existing = await trades_col.find_one({"message_id": message.id})
                if existing is None:
                    symbol, core_line = extract_symbol_and_core_line(content)
                    classification = classify_exit_type(content)

                    pct, direction = parse_explicit_pct(content)
                    if pct is None:
                        pct, parsed_direction = parse_entry_exit_pct(content)
                        if direction is None:
                            direction = parsed_direction

                    if direction is None:
                        direction = infer_result_direction(content)

                    pnl_text, color = build_pnl_text(pct, direction)

                    trade_doc = {
                        "message_id": message.id,
                        "guild_id": message.guild.id if message.guild else None,
                        "channel_id": message.channel.id,
                        "channel": message.channel.name if hasattr(message.channel, "name") else "unknown",
                        "user_id": message.author.id,
                        "username": str(message.author),
                        "symbol": symbol or "N/A",
                        "summary": core_line,
                        "pct": pct,
                        "direction": direction,
                        "classification": classification,
                        "raw_content": content,
                        "timestamp": message.created_at.astimezone(timezone.utc),
                    }

                    try:
                        await trades_col.insert_one(trade_doc)
                        await send_live_log(message, symbol, core_line, classification, pnl_text, color)
                        print(f"Logged trade from {message.author} in #{message.channel}")
                    except DuplicateKeyError:
                        print(f"Duplicate skipped for message_id={message.id}")
                    except Exception as e:
                        print(f"ERROR inserting/logging trade: {e}")
                        traceback.print_exc()

    except Exception as e:
        print(f"ERROR in on_message: {e}")
        traceback.print_exc()
    finally:
        await bot.process_commands(message)

# ====== COMMANDS ======

@bot.command(name="daily")
async def daily_summary(ctx: commands.Context):
    try:
        start_local, end_local = get_today_bounds_local()
        start_utc, end_utc = to_utc_range(start_local, end_local)

        trades = await fetch_trades_between(start_utc, end_utc)

        if not trades:
            await ctx.send("No trades logged yet for today.")
            return

        embed = make_daily_embed(
            f"📒 Daily PnL Summary — {start_local.date()}",
            trades
        )

        live_channel = bot.get_channel(LIVE_LOG_CHANNEL_ID)
        if live_channel is None:
            try:
                live_channel = await bot.fetch_channel(LIVE_LOG_CHANNEL_ID)
            except Exception:
                live_channel = ctx.channel

        await live_channel.send(embed=embed)

    except Exception as e:
        print(f"ERROR in !daily: {e}")
        traceback.print_exc()
        await ctx.send("Error generating daily summary. Check Render logs.")

@bot.command(name="weekly")
async def weekly_summary(ctx: commands.Context):
    try:
        start_local, end_local = get_week_bounds_local()
        start_utc, end_utc = to_utc_range(start_local, end_local)

        trades = await fetch_trades_between(start_utc, end_utc)

        if not trades:
            await ctx.send("No trades logged yet for this week.")
            return

        embed = make_weekly_embed(
            "🗓️ Weekly PnL Recap",
            trades,
            start_local,
            end_local
        )

        weekly_channel = bot.get_channel(WEEKLY_RECAP_CHANNEL_ID)
        if weekly_channel is None:
            try:
                weekly_channel = await bot.fetch_channel(WEEKLY_RECAP_CHANNEL_ID)
            except Exception:
                weekly_channel = ctx.channel

        await weekly_channel.send(embed=embed)

    except Exception as e:
        print(f"ERROR in !weekly: {e}")
        traceback.print_exc()
        await ctx.send("Error generating weekly summary. Check Render logs.")

# ====== AUTOMATIC WEEKLY RECAP ======

@tasks.loop(minutes=1)
async def weekly_recap_loop():
    global last_weekly_recap_key

    try:
        now = datetime.now(TORONTO_TZ)

        if now.weekday() == 4 and now.hour == 20 and now.minute == 0:
            recap_key = f"{now.isocalendar().year}-W{now.isocalendar().week}"

            if recap_key == last_weekly_recap_key:
                return

            start_local, end_local = get_week_bounds_local()
            start_utc, end_utc = to_utc_range(start_local, end_local)

            trades = await fetch_trades_between(start_utc, end_utc)

            weekly_channel = bot.get_channel(WEEKLY_RECAP_CHANNEL_ID)
            if weekly_channel is None:
                try:
                    weekly_channel = await bot.fetch_channel(WEEKLY_RECAP_CHANNEL_ID)
                except Exception:
                    print("ERROR: could not fetch weekly recap channel")
                    return

            if trades:
                embed = make_weekly_embed(
                    "🗓️ Automatic Weekly PnL Recap",
                    trades,
                    start_local,
                    end_local
                )
                await weekly_channel.send(embed=embed)
            else:
                await weekly_channel.send("No trades were logged for this week.")

            last_weekly_recap_key = recap_key

    except Exception as e:
        print(f"ERROR in weekly_recap_loop: {e}")
        traceback.print_exc()

@weekly_recap_loop.before_loop
async def before_weekly_recap_loop():
    await bot.wait_until_ready()

# ====== OPTIONAL DEBUG COMMANDS ======

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send("Pong.")

@bot.command(name="health")
async def health(ctx: commands.Context):
    await ctx.send(
        f"Watching: {WATCHED_CHANNEL_IDS}\n"
        f"Live logs: {LIVE_LOG_CHANNEL_ID}\n"
        f"Weekly logs: {WEEKLY_RECAP_CHANNEL_ID}"
    )

# ====== STARTUP ======

threading.Thread(target=run_health_server, daemon=True).start()

print("DEBUG: about to run bot")

try:
    bot.run(TOKEN)
except Exception as e:
    print("ERROR starting bot:")
    print(str(e))
    traceback.print_exc()
    raise