print("DEBUG: script started")

import os
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient

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

LOG_CHANNEL_ID = 1459297868861931715
WEEKLY_RECAP_CHANNEL_ID = 1507876090956353726

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
    "green", "green day", "gains", "in the green",
    "loss", "losses", "loser", "red", "red day",
    "down", "small loss", "slight loss",
]

PARTIAL_TAGS = ["(partial)", "[partial]", "partial", "trim", "trimmed", "trimming", "haircut"]
FULL_EXIT_TAGS = ["(full)", "[full]", "full", "all out", "closed", "sold the rest", "flat"]
TP1_TAGS = ["tp1", "tp 1", "first trim", "first tp"]
TP2_TAGS = ["tp2", "tp 2", "second trim", "second tp"]
TP3_TAGS = ["tp3", "tp 3", "third trim", "third tp"]
DAYTRADE_TAGS = ["daytrade", "day trade", "intraday", "scalp"]
SWING_TAGS = ["swing", "swing trade"]

daily_trades = []
weekly_trades = []
last_weekly_recap_key = None

# ====== HTTP HEALTH SERVER FOR RENDER ======

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/health":
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
    pattern = re.compile(
        r"([A-Za-z]{2,6}\s+\d{2,5}\w?)\s*@\s*([\d\.]+)(?:\s+(-?\d+(?:\.\d+)?%)(?:\s+(profit|loss))?)?",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        symbol = match.group(1).upper()
        return symbol, match.group(0)
    return None, text.strip()

def parse_explicit_pct(text: str):
    t = text.lower()
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", t)
    if not m:
        return None, None

    pct = float(m.group(1))
    if "loss" in t or "red" in t or "down" in t:
        direction = "loss"
        if pct > 0:
            pct = -pct
    elif "profit" in t or "win" in t or "green" in t or "gains" in t:
        direction = "profit"
    else:
        direction = None

    return round(pct, 2), direction

def parse_entry_exit_pct(text: str):
    t = text.lower()
    entry_match = re.search(r"entry\s*@?\s*([\d\.]+)", t)
    exit_match = re.search(r"(?:exit|out at)\s*@?\s*([\d\.]+)", t)

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
    if any(x in t for x in ["profit", "profits", "win", "winner", "green", "gains", "in the green"]):
        return "profit"
    if any(x in t for x in ["loss", "losses", "loser", "red", "small loss", "slight loss", "down"]):
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

def make_summary_embed(title: str, trades: list[dict], color: discord.Color) -> discord.Embed:
    total_pct = 0.0
    counted = 0
    wins = 0
    losses = 0
    breakevens = 0
    lines = []

    for t in trades:
        pct = t["pct"]
        symbol = t["symbol"]
        summary = t["summary"]

        if pct is not None:
            total_pct += pct
            counted += 1
            if pct > 0:
                wins += 1
            elif pct < 0:
                losses += 1
            else:
                breakevens += 1

        pct_text = f"{pct:+.2f}%" if pct is not None else "n/a"
        lines.append(f"• `{symbol}` — {summary} — **{pct_text}**")

    avg_pct = (total_pct / counted) if counted else 0.0
    wl_trades = wins + losses
    winrate = (wins / wl_trades * 100.0) if wl_trades > 0 else 0.0

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Net PnL", value=f"{total_pct:+.2f}%", inline=True)
    embed.add_field(name="Average per Trade", value=f"{avg_pct:+.2f}%", inline=True)
    embed.add_field(name="Winrate", value=f"{winrate:.2f}% ({wins}W / {losses}L, {breakevens} BE)", inline=False)

    trade_text = "\n".join(lines) if lines else "_No logged trades in this period._"
    if len(trade_text) > 1024:
        trade_text = trade_text[:1000] + "\n…and more"
    embed.add_field(name="Trades", value=trade_text, inline=False)

    embed.set_footer(text="PnL recap")
    embed.timestamp = datetime.now(TORONTO_TZ)
    return embed

# ====== MESSAGE LISTENER ======

@bot.listen("on_message")
async def trade_logger(message: discord.Message):
    if message.author.bot:
        return

    if message.channel.id not in WATCHED_CHANNEL_IDS:
        return

    content = message.content
    content_lower = content.lower()

    if not any(keyword in content_lower for keyword in KEYWORDS):
        return

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        return

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

    embed = discord.Embed(
        title="📊 Trade Close Logged",
        color=color,
        timestamp=message.created_at,
    )
    embed.add_field(name="Trader", value=message.author.mention, inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    if symbol:
        embed.add_field(name="Ticker / Contract", value=f"`{symbol}`", inline=True)

    embed.add_field(name="Summary", value=f"`{core_line}`", inline=False)
    embed.add_field(name="Classification", value=classification or "n/a", inline=False)
    embed.add_field(name="PnL", value=pnl_text, inline=False)

    if message.guild:
        msg_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
        embed.add_field(name="Jump to Message", value=f"[Open in Discord]({msg_link})", inline=False)

    embed.set_footer(text=f"Original: {content[:180]}")
    await log_channel.send(embed=embed)

    trade_data = {
        "symbol": symbol or "N/A",
        "summary": core_line,
        "pct": pct,
        "direction": direction,
        "classification": classification,
        "channel": message.channel.name,
    }

    await trades_col.insert_one({
        "symbol": trade_data["symbol"],
        "summary": trade_data["summary"],
        "pct": trade_data["pct"],
        "direction": trade_data["direction"],
        "classification": trade_data["classification"],
        "channel": trade_data["channel"],
        "user_id": message.author.id,
        "timestamp": message.created_at,
    })

    daily_trades.append(trade_data)
    weekly_trades.append(trade_data)

    print(f"Logged trade from {message.author} in #{message.channel.name}")

# ====== COMMANDS ======

@bot.command(name="daily")
async def daily_summary(ctx: commands.Context):
    if not daily_trades:
        await ctx.send("No trades logged yet for today.")
        return

    embed = make_summary_embed("📒 Daily PnL Summary", daily_trades, discord.Color.gold())
    await ctx.send(embed=embed)
    daily_trades.clear()

@bot.command(name="weekly")
async def weekly_summary(ctx: commands.Context):
    if not weekly_trades:
        await ctx.send("No trades logged yet for this week.")
        return

    weekly_channel = bot.get_channel(WEEKLY_RECAP_CHANNEL_ID) or ctx.channel
    embed = make_summary_embed("🗓️ Weekly PnL Recap", weekly_trades, discord.Color.blue())
    await weekly_channel.send(embed=embed)

# ====== AUTOMATIC WEEKLY RECAP ======

@tasks.loop(minutes=1)
async def weekly_recap_loop():
    global last_weekly_recap_key, weekly_trades

    now = datetime.now(TORONTO_TZ)

    if now.weekday() == 4 and now.hour == 20 and now.minute == 0:
        recap_key = f"{now.isocalendar().year}-W{now.isocalendar().week}"

        if recap_key == last_weekly_recap_key:
            return

        weekly_channel = bot.get_channel(WEEKLY_RECAP_CHANNEL_ID)
        if weekly_channel is None:
            return

        embed = make_summary_embed("🗓️ Automatic Weekly PnL Recap", weekly_trades, discord.Color.blue())
        await weekly_channel.send(embed=embed)

        last_weekly_recap_key = recap_key
        weekly_trades = []

@weekly_recap_loop.before_loop
async def before_weekly_recap_loop():
    await bot.wait_until_ready()

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