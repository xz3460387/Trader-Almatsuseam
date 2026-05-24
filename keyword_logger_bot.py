import os
import logging
import traceback
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import re
from collections import Counter, defaultdict

import discord
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient

# ------------- CONFIG -------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

DB_NAME = os.getenv("MONGO_DB_NAME", "TradeRecap")
TRADES_COLLECTION_NAME = os.getenv("MONGO_COLLECTION_NAME", "trades")

# Channels where trades are auto-detected from messages
WATCHED_CHANNEL_IDS = [
    1439856570061033565,
    1462285627000094896,
    1477839908562407485,
]

# Live recaps (each time a trade is logged)
LIVE_RECAP_CHANNEL_ID = 1459297868861931715

# Weekly recap destination
WEEKLY_RECAP_CHANNEL_ID = 1507876090956353726

# Long-term stats destination (for !stats, !monthly, !ticker, !streaks)
LONG_TERM_STATS_CHANNEL_ID = 1507899410246275152

# Commentaries and analytics destination (pattern-detection text, debug info)
ANALYTICS_CHANNEL_ID = 1507899492140060723

COMMAND_PREFIX = "!"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trade_logger_bot")

DEBUG_IGNORED_MESSAGES = False  # toggled by !debugon / !debugoff


# ------------- MONGO -------------

mongo_client: AsyncIOMotorClient = AsyncIOMotorClient(MONGO_URI) if MONGO_URI else None
db = mongo_client[DB_NAME] if mongo_client else None
trades_col = db[TRADES_COLLECTION_NAME] if db else None


# ------------- PARSING HELPERS -------------

pct_regex = re.compile(r"([-+]?\d+(\.\d+)?)\s*%")
ticker_regex = re.compile(r"\b[A-Z]{2,6}\b")

# Basic options pattern like "TSLA 220c 5/23" or "TSLA 220C 05/23"
options_pattern = re.compile(
    r"\b(?P<symbol>[A-Z]{2,6})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s*"
    r"(?P<cp>[cCpP])\b"
    r"(?:\s+|[@ ])"
    r"(?P<expiry>\d{1,2}/\d{1,2}(?:/\d{2,4})?)?"
)

# Entry / exit patterns: "in @ 1.20 out @ 1.85" or "entry 1.20 exit 1.85"
entry_pattern = re.compile(
    r"(?:in|entry|avg|average)\s*(?:@|at)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
exit_pattern = re.compile(
    r"(?:out|exit|closed at|sold at)\s*(?:@|at)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def extract_pct(content: str) -> Optional[float]:
    m = pct_regex.search(content)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def guess_direction(pct: Optional[float], content: str) -> Optional[str]:
    lower = content.lower()
    if pct is not None:
        if pct > 0:
            return "win"
        if pct < 0:
            return "loss"
    if any(k in lower for k in ["tp", "took profit", "secured", "bagged"]):
        return "win"
    if any(k in lower for k in ["sl", "stopped", "cut", "stopped out", "stop hit"]):
        return "loss"
    return None


def guess_classification(content: str) -> Optional[str]:
    lower = content.lower()
    if "scalp" in lower:
        return "scalp"
    if "swing" in lower:
        return "swing"
    if "lotto" in lower or "loto" in lower:
        return "lotto"
    return None


def guess_symbol_and_option_fields(content: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Try to pull ticker, contract type, strike, expiry from the message.
    """
    fields: Dict[str, Any] = {
        "contract_type": None,  # "options" or "shares"
        "strike": None,
        "expiry": None,
        "option_type": None,
    }

    # Try options-style pattern first
    m = options_pattern.search(content)
    if m:
        symbol = m.group("symbol").upper()
        strike = m.group("strike")
        cp = m.group("cp")
        expiry = m.group("expiry")

        fields["contract_type"] = "options"
        fields["strike"] = float(strike)
        fields["expiry"] = expiry
        fields["option_type"] = "call" if cp.lower() == "c" else "put"
        return symbol, fields

    # Fallback: generic ticker guess
    tokens = ticker_regex.findall(content)
    blacklist = {"TP", "SL", "ATM", "ITM", "OTM", "USD"}
    symbol = None
    for t in tokens:
        if t not in blacklist:
            symbol = t.upper()
            break

    # Very rough contract_type guess
    lower = content.lower()
    if any(k in lower for k in ["calls", "puts", "contract", "contracts"]):
        fields["contract_type"] = "options"
    elif any(k in lower for k in ["shares", "equity", "stock"]):
        fields["contract_type"] = "shares"

    return symbol, fields


def extract_entry_exit(content: str) -> Tuple[Optional[float], Optional[float]]:
    entry = None
    exit_ = None

    m_entry = entry_pattern.search(content)
    if m_entry:
        try:
            entry = float(m_entry.group(1))
        except ValueError:
            entry = None

    m_exit = exit_pattern.search(content)
    if m_exit:
        try:
            exit_ = float(m_exit.group(1))
        except ValueError:
            exit_ = None

    return entry, exit_


def parse_trade_message_with_debug(
    message: discord.Message,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    Parse a potential trade message, returning (trade_doc or None, debug_reasons list).
    debug_reasons contains tags explaining what was missing / why it was ignored.
    """
    reasons: List[str] = []

    if message.author.bot:
        reasons.append("author_is_bot")
        return None, reasons

    if message.channel.id not in WATCHED_CHANNEL_IDS:
        reasons.append("channel_not_watched")
        return None, reasons

    content = message.content.strip()
    lower = content.lower()

    # Keywords to qualify as a "close/recap" style message
    keywords = ["closed", "took profit", "tp", "sl", "stopped", "cut", "%"]
    if not any(k in lower for k in keywords):
        reasons.append("no_close_keywords")
        return None, reasons

    pct = extract_pct(content)
    if pct is None:
        reasons.append("no_percentage_found")

    direction = guess_direction(pct, content)
    if direction is None:
        reasons.append("no_direction_inferred")

    classification = guess_classification(content)
    if classification is None:
        reasons.append("no_classification_inferred")

    symbol, extra_fields = guess_symbol_and_option_fields(content)
    if symbol is None:
        reasons.append("no_ticker_found")

    entry_price, exit_price = extract_entry_exit(content)
    if entry_price is None:
        reasons.append("no_entry_price_found")
    if exit_price is None:
        reasons.append("no_exit_price_found")

    now_utc = datetime.utcnow()

    trade_doc: Dict[str, Any] = {
        "message_id": message.id,
        "channel_id": message.channel.id,
        "channel_name": getattr(message.channel, "name", "unknown"),
        "author_id": message.author.id,
        "author_name": str(message.author),
        "content": content,
        "summary": content,
        "pct": pct,
        "direction": direction,
        "classification": classification,
        "symbol": symbol,
        "jump_url": message.jump_url,
        "entry_price": entry_price,
        "exit_price": exit_price,
        # core timestamps: BOTH stored as naive UTC for simple comparisons
        "timestamp": message.created_at.replace(tzinfo=None),
        "created_at": now_utc,
    }

    trade_doc.update(extra_fields)

    return trade_doc, reasons


def parse_trade_message(message: discord.Message) -> Optional[Dict[str, Any]]:
    trade, _ = parse_trade_message_with_debug(message)
    return trade


# ------------- EMBED HELPERS -------------

def format_pct(p: Optional[float]) -> str:
    if isinstance(p, (int, float)):
        return f"{p:+.2f}%"
    return "N/A"


def format_trade_line(trade: Dict[str, Any]) -> str:
    symbol = trade.get("symbol") or "N/A"
    pct = format_pct(trade.get("pct"))
    direction = trade.get("direction") or "N/A"
    classification = trade.get("classification") or ""
    channel = trade.get("channel_name") or trade.get("channel") or "unknown"
    jump_url = trade.get("jump_url")

    tags = []
    if classification:
        tags.append(classification)
    contract_type = trade.get("contract_type")
    if contract_type:
        tags.append(contract_type)
    if trade.get("option_type"):
        tags.append(trade["option_type"])

    tag_str = f" `[{', '.join(tags)}]`" if tags else ""
    base = f"**{symbol}** {pct} `{direction}`{tag_str} · #{channel}"
    if jump_url:
        base = f"[{base}]({jump_url})"

    return base


def summarize_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    pct_values = [t["pct"] for t in trades if isinstance(t.get("pct"), (int, float))]
    total_pnl = sum(pct_values) if pct_values else 0.0
    avg_pnl = (total_pnl / len(pct_values)) if pct_values else 0.0

    wins = [t for t in trades if t.get("direction") == "win"]
    losses = [t for t in trades if t.get("direction") == "loss"]

    best_trade = None
    worst_trade = None
    if pct_values:
        best_trade = max(
            [t for t in trades if isinstance(t.get("pct"), (int, float))],
            key=lambda x: x["pct"],
        )
        worst_trade = min(
            [t for t in trades if isinstance(t.get("pct"), (int, float))],
            key=lambda x: x["pct"],
        )

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
    }


def compute_streaks(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute longest win/loss streak and current streak.
    """
    # Filter to win/loss only and sort by time
    seq = [
        t for t in trades
        if t.get("direction") in ("win", "loss")
    ]
    seq.sort(key=lambda t: t.get("created_at") or t.get("timestamp") or datetime.utcnow())

    longest_win = 0
    longest_loss = 0
    current_type = None
    current_len = 0

    for t in seq:
        d = t.get("direction")
        if d not in ("win", "loss"):
            current_type = None
            current_len = 0
            continue

        if d == current_type:
            current_len += 1
        else:
            current_type = d
            current_len = 1

        if current_type == "win":
            longest_win = max(longest_win, current_len)
        else:
            longest_loss = max(longest_loss, current_len)

    return {
        "longest_win": longest_win,
        "longest_loss": longest_loss,
        "current_type": current_type,
        "current_len": current_len,
    }


def make_daily_embed(date_label: str, trades: List[Dict[str, Any]]) -> discord.Embed:
    stats = summarize_trades(trades)

    embed = discord.Embed(
        title=f"📒 Daily Trade Recap – {date_label}",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Keyword Logger · Daily Recap")

    total = stats["total_trades"]
    wins = stats["wins"]
    losses = stats["losses"]
    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    embed.add_field(
        name="Overview",
        value=(
            f"Trades: **{total}** · Wins: **{wins}** · Losses: **{losses}**\n"
            f"Win rate: **{win_rate:.1f}%**\n"
            f"Total PnL: **{stats['total_pnl']:+.2f}%** · "
            f"Avg PnL: **{stats['avg_pnl']:+.2f}%**"
        ),
        inline=False,
    )

    best = stats["best_trade"]
    worst = stats["worst_trade"]
    lines = []
    if best:
        lines.append(f"🏆 Best: {format_trade_line(best)}")
    if worst:
        lines.append(f"💀 Worst: {format_trade_line(worst)}")
    if lines:
        embed.add_field(name="Highlights", value="\n".join(lines), inline=False)

    # Detailed list (truncated if massive)
    detail_lines = [f"• {format_trade_line(t)}" for t in trades]
    desc = "\n".join(detail_lines)
    if len(desc) > 3800:
        desc = desc[:3800] + "\n… (truncated)"
    embed.description = desc or "No trades logged."

    return embed


def make_weekly_embed(start_date: datetime, end_date: datetime, trades: List[Dict[str, Any]]) -> discord.Embed:
    stats = summarize_trades(trades)

    date_range = f"{start_date.date().isoformat()} → {end_date.date().isoformat()}"
    embed = discord.Embed(
        title="🗓️ Weekly PnL Recap",
        description=f"Week of **{date_range}**",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Keyword Logger · Weekly Recap")

    total = stats["total_trades"]
    wins = stats["wins"]
    losses = stats["losses"]
    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    embed.add_field(
        name="Overview",
        value=(
            f"Trades: **{total}** · Wins: **{wins}** · Losses: **{losses}**\n"
            f"Win rate: **{win_rate:.1f}%**\n"
            f"Total PnL: **{stats['total_pnl']:+.2f}%** · "
            f"Avg PnL: **{stats['avg_pnl']:+.2f}%**"
        ),
        inline=False,
    )

    # Group by day
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        ts = t.get("created_at") or t.get("timestamp") or datetime.utcnow()
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except Exception:
                ts = datetime.utcnow()
        date_key = ts.date().isoformat()
        by_day.setdefault(date_key, []).append(t)

    # Each day gets its own field, with per-trade % and jump links
    for date_key in sorted(by_day.keys()):
        day_trades = by_day[date_key]
        day_stats = summarize_trades(day_trades)

        header = (
            f"Trades: **{day_stats['total_trades']}** · "
            f"PnL: **{day_stats['total_pnl']:+.2f}%**"
        )

        lines = [header, ""]
        for t in day_trades:
            line = f"• {format_trade_line(t)}"
            lines.append(line)

        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1000] + "\n… (truncated)"

        embed.add_field(name=date_key, value=value, inline=False)

    best = stats["best_trade"]
    worst = stats["worst_trade"]
    highlights = []
    if best:
        highlights.append(f"🏆 Best: {format_trade_line(best)}")
    if worst:
        highlights.append(f"💀 Worst: {format_trade_line(worst)}")
    if highlights:
        embed.add_field(name="Highlights", value="\n".join(highlights), inline=False)

    return embed


def make_live_recap_embed(trade: Dict[str, Any]) -> discord.Embed:
    symbol = trade.get("symbol") or "N/A"
    pct = format_pct(trade.get("pct"))
    direction = trade.get("direction") or "N/A"
    classification = trade.get("classification") or ""
    author_name = trade.get("author_name") or "unknown"
    channel = trade.get("channel_name") or "unknown"

    title = f"📡 Live Trade Logged – {symbol}"
    color = discord.Color.green() if direction == "win" else discord.Color.red()

    embed = discord.Embed(
        title=title,
        description=trade.get("summary") or trade.get("content") or "",
        color=color,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="PnL", value=pct, inline=True)
    embed.add_field(name="Direction", value=direction, inline=True)
    if classification:
        embed.add_field(name="Type", value=classification, inline=True)

    if trade.get("contract_type"):
        embed.add_field(name="Contract", value=trade["contract_type"], inline=True)
    if trade.get("strike"):
        embed.add_field(name="Strike", value=str(trade["strike"]), inline=True)
    if trade.get("expiry"):
        embed.add_field(name="Expiry", value=str(trade["expiry"]), inline=True)

    if trade.get("entry_price") is not None:
        embed.add_field(name="Entry", value=str(trade["entry_price"]), inline=True)
    if trade.get("exit_price") is not None:
        embed.add_field(name="Exit", value=str(trade["exit_price"]), inline=True)

    embed.add_field(name="Trader", value=author_name, inline=True)
    embed.add_field(name="Channel", value=f"#{channel}", inline=True)
    if trade.get("jump_url"):
        embed.add_field(name="Jump", value=f"[Go to message]({trade['jump_url']})", inline=False)

    embed.set_footer(text="Keyword Logger · Live Recap")
    return embed


def make_debug_embed(message: discord.Message, reasons: List[str]) -> discord.Embed:
    embed = discord.Embed(
        title="🔍 Ignored Message Debug",
        description=message.content or "(no content)",
        color=discord.Color.orange(),
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Author", value=str(message.author), inline=True)
    embed.add_field(name="Channel", value=f"#{getattr(message.channel, 'name', 'unknown')}", inline=True)
    embed.add_field(name="Message ID", value=str(message.id), inline=False)
    if reasons:
        embed.add_field(name="Reasons", value=", ".join(reasons), inline=False)
    embed.set_footer(text="Keyword Logger · Debug")
    return embed


def get_channel(channel_id: int) -> Optional[discord.TextChannel]:
    ch = bot.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    return None


# ------------- BOT EVENTS -------------

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not mongo_client or not trades_col:
        logger.error("MongoDB is not configured correctly (MONGO_URI/DB/COLLECTION).")
    else:
        logger.info(f"Connected to MongoDB DB={DB_NAME} Collection={TRADES_COLLECTION_NAME}")

    if not weekly_scheduler.is_running():
        weekly_scheduler.start()


@bot.event
async def on_message(message: discord.Message):
    global DEBUG_IGNORED_MESSAGES

    # Always let commands run first
    await bot.process_commands(message)

    try:
        if not trades_col:
            return

        trade_doc, debug_reasons = parse_trade_message_with_debug(message)
        if not trade_doc:
            if DEBUG_IGNORED_MESSAGES and message.channel.id in WATCHED_CHANNEL_IDS:
                analytics_ch = get_channel(ANALYTICS_CHANNEL_ID) or message.channel
                try:
                    await analytics_ch.send(embed=make_debug_embed(message, debug_reasons))
                except Exception:
                    logger.exception("Failed to send debug info")
            return

        # Duplicate detection (same message_id already logged)
        existing = await trades_col.find_one({"message_id": trade_doc["message_id"]})
        if existing:
            logger.info(f"Duplicate trade ignored for message_id={trade_doc['message_id']}")
            return

        await trades_col.insert_one(trade_doc)
        logger.info(
            f"Logged trade from {trade_doc['author_name']} "
            f"({trade_doc.get('symbol', 'N/A')} {trade_doc.get('pct')})"
        )

        # Live recap to dedicated channel
        live_ch = get_channel(LIVE_RECAP_CHANNEL_ID)
        if live_ch:
            embed = make_live_recap_embed(trade_doc)
            await live_ch.send(embed=embed)

        # Light confirmation in the source channel
        try:
            confirmation = f"Trade logged: {trade_doc.get('symbol') or 'N/A'} {format_pct(trade_doc.get('pct'))}"
            await message.add_reaction("📒")
            await message.channel.send(confirmation, delete_after=10)
        except Exception:
            logger.exception("Failed to send confirmation")

    except Exception:
        logger.exception("Error while handling message / logging trade")


# ------------- COMMANDS -------------

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send("Pong.")


@bot.command(name="health")
async def health(ctx: commands.Context):
    try:
        mongo_ok = False
        if trades_col:
            await trades_col.estimated_document_count()
            mongo_ok = True
        watched = ", ".join(f"<#{cid}>" for cid in WATCHED_CHANNEL_IDS)
        msg = (
            f"**Bot health check**\n"
            f"Discord: ✅\n"
            f"MongoDB: {'✅' if mongo_ok else '❌'}\n"
            f"Watched channels: {watched}\n"
            f"Live recap: <#{LIVE_RECAP_CHANNEL_ID}>\n"
            f"Weekly recap: <#{WEEKLY_RECAP_CHANNEL_ID}>\n"
            f"Stats: <#{LONG_TERM_STATS_CHANNEL_ID}>\n"
            f"Analytics: <#{ANALYTICS_CHANNEL_ID}>"
        )
        await ctx.send(msg)
    except Exception:
        logger.exception("health command failed")
        await ctx.send("health check failed, see logs.")


@bot.command(name="debugon")
@commands.has_permissions(administrator=True)
async def debug_on(ctx: commands.Context):
    global DEBUG_IGNORED_MESSAGES
    DEBUG_IGNORED_MESSAGES = True
    await ctx.send("Debug mode **enabled** – ignored trade messages in watched channels will be explained in analytics.")


@bot.command(name="debugoff")
@commands.has_permissions(administrator=True)
async def debug_off(ctx: commands.Context):
    global DEBUG_IGNORED_MESSAGES
    DEBUG_IGNORED_MESSAGES = False
    await ctx.send("Debug mode **disabled**.")


@bot.command(name="daily")
async def daily_summary(ctx: commands.Context):
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        now_utc = datetime.utcnow()
        start_of_day = datetime(now_utc.year, now_utc.month, now_utc.day)
        end_of_day = start_of_day + timedelta(days=1)

        cursor = trades_col.find(
            {
                "created_at": {
                    "$gte": start_of_day,
                    "$lt": end_of_day,
                }
            }
        )
        trades: List[Dict[str, Any]] = []
        async for doc in cursor:
            doc["channel_name"] = doc.get("channel_name") or doc.get("channel") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet for today.")
            return

        date_label = start_of_day.strftime("%Y-%m-%d")
        embed = make_daily_embed(date_label, trades)

        target = get_channel(LIVE_RECAP_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in daily_summary")
        await ctx.send("Error while generating daily summary. Check logs.")


@bot.command(name="weekly")
async def weekly_summary(ctx: commands.Context):
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        now_utc = datetime.utcnow()
        days_since_monday = now_utc.weekday()  # Monday=0
        start_of_week = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=days_since_monday)
        end_of_week = start_of_week + timedelta(days=7)

        cursor = trades_col.find(
            {
                "created_at": {
                    "$gte": start_of_week,
                    "$lt": end_of_week,
                }
            }
        )
        trades: List[Dict[str, Any]] = []
        async for doc in cursor:
            doc["channel_name"] = doc.get("channel_name") or doc.get("channel") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet for this week.")
            return

        embed = make_weekly_embed(start_of_week, min(end_of_week, datetime.utcnow()), trades)

        weekly_ch = get_channel(WEEKLY_RECAP_CHANNEL_ID) or ctx.channel
        await weekly_ch.send(embed=embed)

    except Exception:
        logger.exception("ERROR in weekly_summary")
        await ctx.send("Error while generating weekly summary. Check logs.")


@bot.command(name="monthly")
async def monthly_summary(ctx: commands.Context):
    """
    Month-to-date stats and recap, sent to long-term stats channel.
    """
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        now_utc = datetime.utcnow()
        start_of_month = datetime(now_utc.year, now_utc.month, 1)

        cursor = trades_col.find(
            {
                "created_at": {
                    "$gte": start_of_month,
                    "$lt": now_utc,
                }
            }
        )
        trades: List[Dict[str, Any]] = []
        async for doc in cursor:
            doc["channel_name"] = doc.get("channel_name") or doc.get("channel") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet this month.")
            return

        embed = make_weekly_embed(start_of_month, now_utc, trades)
        embed.title = "📆 Month-To-Date PnL Recap"

        target = get_channel(LONG_TERM_STATS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in monthly_summary")
        await ctx.send("Error while generating monthly summary. Check logs.")


@bot.command(name="stats")
async def stats_command(ctx: commands.Context):
    """
    Overall lifetime stats (sent to long-term stats channel) + pattern commentary to analytics.
    """
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        cursor = trades_col.find({})
        trades: List[Dict[str, Any]] = []
        async for doc in cursor:
            doc["channel_name"] = doc.get("channel_name") or doc.get("channel") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet.")
            return

        stats = summarize_trades(trades)
        total = stats["total_trades"]
        wins = stats["wins"]
        losses = stats["losses"]
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        counts = Counter()
        pnl_by_ticker = defaultdict(float)

        for t in trades:
            sym = (t.get("symbol") or "N/A").upper()
            counts[sym] += 1
            if isinstance(t.get("pct"), (int, float)):
                pnl_by_ticker[sym] += t["pct"]

        top_count = counts.most_common(3)
        top_pnl = sorted(pnl_by_ticker.items(), key=lambda kv: kv[1], reverse=True)[:3]

        embed = discord.Embed(
            title="📊 Lifetime Trading Stats",
            color=discord.Color.purple(),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="Keyword Logger · Long-Term Stats")

        embed.add_field(
            name="Overview",
            value=(
                f"Trades: **{total}** · Wins: **{wins}** · Losses: **{losses}**\n"
                f"Win rate: **{win_rate:.1f}%**\n"
                f"Total PnL: **{stats['total_pnl']:+.2f}%** · "
                f"Avg PnL: **{stats['avg_pnl']:+.2f}%**"
            ),
            inline=False,
        )

        if top_count:
            lines = [f"• {sym}: **{cnt}** trades" for sym, cnt in top_count]
            embed.add_field(name="Most Traded Tickers", value="\n".join(lines), inline=True)

        if top_pnl:
            lines = [f"• {sym}: **{pnl:+.2f}%**" for sym, pnl in top_pnl]
            embed.add_field(name="Top PnL Tickers", value="\n".join(lines), inline=True)

        # Best / worst individual trades
        best = stats["best_trade"]
        worst = stats["worst_trade"]
        highlights = []
        if best:
            highlights.append(f"🏆 Best: {format_trade_line(best)}")
        if worst:
            highlights.append(f"💀 Worst: {format_trade_line(worst)}")
        if highlights:
            embed.add_field(name="Highlights", value="\n".join(highlights), inline=False)

        target = get_channel(LONG_TERM_STATS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

        # Pattern-analysis commentary to analytics channel
        analytics_ch = get_channel(ANALYTICS_CHANNEL_ID)
        if analytics_ch:
            # Scalp vs swing
            scalps = [t for t in trades if t.get("classification") == "scalp"]
            swings = [t for t in trades if t.get("classification") == "swing"]

            def wr(lst: List[Dict[str, Any]]) -> float:
                if not lst:
                    return 0.0
                w = len([t for t in lst if t.get("direction") == "win"])
                return w / len(lst) * 100.0

            def avg_p(lst: List[Dict[str, Any]]) -> float:
                vals = [t["pct"] for t in lst if isinstance(t.get("pct"), (int, float))]
                return sum(vals) / len(vals) if vals else 0.0

            scalp_wr = wr(scalps)
            swing_wr = wr(swings)
            scalp_avg = avg_p(scalps)
            swing_avg = avg_p(swings)

            # Day-of-week (0=Mon)
            dow_stats = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
            for t in trades:
                ts = t.get("created_at") or t.get("timestamp") or datetime.utcnow()
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts)
                    except Exception:
                        ts = datetime.utcnow()
                d = ts.weekday()
                dow_stats[d]["trades"] += 1
                if isinstance(t.get("pct"), (int, float)):
                    dow_stats[d]["pnl"] += t["pct"]

            worst_day = None
            worst_pnl = None
            for d, s in dow_stats.items():
                if worst_pnl is None or s["pnl"] < worst_pnl:
                    worst_pnl = s["pnl"]
                    worst_day = d

            dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            worst_day_name = dow_names[worst_day] if worst_day is not None else "N/A"

            # Highest-frequency ticker
            most_traded = top_count[0][0] if top_count else "N/A"

            # Stops vs no stops
            with_stop = []
            without_stop = []
            for t in trades:
                text = (t.get("content") or "").lower()
                if any(k in text for k in ["sl", "stop", "stopped out", "stop hit"]):
                    with_stop.append(t)
                else:
                    without_stop.append(t)

            with_stop_avg = avg_p(with_stop)
            without_stop_avg = avg_p(without_stop)

            commentary_lines = [
                f"Scalps: {len(scalps)} trades, {scalp_wr:.1f}% win rate, avg {scalp_avg:+.2f}%.",
                f"Swings: {len(swings)} trades, {swing_wr:.1f}% win rate, avg {swing_avg:+.2f}%.",
                f"Worst day by total PnL so far: **{worst_day_name}** ({worst_pnl:+.2f}% total).",
                f"Most traded ticker: **{most_traded}**.",
                f"Trades mentioning a stop: avg {with_stop_avg:+.2f}%; without explicit stop: {without_stop_avg:+.2f}%.",
            ]
            await analytics_ch.send("📈 **Pattern snapshot**\n" + "\n".join(commentary_lines))

    except Exception:
        logger.exception("ERROR in stats_command")
        await ctx.send("Error while generating stats. Check logs.")


@bot.command(name="ticker")
async def ticker_command(ctx: commands.Context, symbol: str):
    """
    Per-ticker analytics: win rate, PnL, best/worst, last N trades.
    """
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        sym = symbol.upper()
        cursor = trades_col.find({"symbol": sym})
        trades: List[Dict[str, Any]] = []
        async for doc in cursor:
            doc["channel_name"] = doc.get("channel_name") or doc.get("channel") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send(f"No trades logged yet for {sym}.")
            return

        stats = summarize_trades(trades)
        total = stats["total_trades"]
        wins = stats["wins"]
        losses = stats["losses"]
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        embed = discord.Embed(
            title=f"📌 Ticker Stats – {sym}",
            color=discord.Color.teal(),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="Keyword Logger · Ticker Analytics")

        embed.add_field(
            name="Overview",
            value=(
                f"Trades: **{total}** · Wins: **{wins}** · Losses: **{losses}**\n"
                f"Win rate: **{win_rate:.1f}%**\n"
                f"Total PnL: **{stats['total_pnl']:+.2f}%** · "
                f"Avg PnL: **{stats['avg_pnl']:+.2f}%**"
            ),
            inline=False,
        )

        best = stats["best_trade"]
        worst = stats["worst_trade"]
        if best or worst:
            lines = []
            if best:
                lines.append(f"🏆 Best: {format_trade_line(best)}")
            if worst:
                lines.append(f"💀 Worst: {format_trade_line(worst)}")
            embed.add_field(name="Highlights", value="\n".join(lines), inline=False)

        # Show last up to 10 trades
        trades_sorted = sorted(
            trades,
            key=lambda t: t.get("created_at") or t.get("timestamp") or datetime.utcnow(),
            reverse=True,
        )
        last_trades = trades_sorted[:10]
        detail_lines = [f"• {format_trade_line(t)}" for t in last_trades]
        desc = "\n".join(detail_lines)
        if len(desc) > 3800:
            desc = desc[:3800] + "\n… (truncated)"
        embed.description = desc

        target = get_channel(LONG_TERM_STATS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in ticker_command")
        await ctx.send("Error while generating ticker stats. Check logs.")


@bot.command(name="streaks")
async def streaks_command(ctx: commands.Context):
    """
    Show longest win/loss streaks and current streak.
    """
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        cursor = trades_col.find({})
        trades: List[Dict[str, Any]] = []
        async for doc in cursor:
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet.")
            return

        s = compute_streaks(trades)

        embed = discord.Embed(
            title="🔥 Streak Stats",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="Keyword Logger · Streaks")

        current = (
            f"Current streak: **{s['current_len']} {s['current_type'] or 'none'}**"
            if s["current_len"] > 0
            else "Current streak: none"
        )
        embed.add_field(
            name="Streaks",
            value=(
                f"Longest win streak: **{s['longest_win']}**\n"
                f"Longest loss streak: **{s['longest_loss']}**\n"
                f"{current}"
            ),
            inline=False,
        )

        target = get_channel(LONG_TERM_STATS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in streaks_command")
        await ctx.send("Error while generating streak stats. Check logs.")


@bot.command(name="rawcount")
async def rawcount(ctx: commands.Context, scope: str = "all"):
    """
    Show how many trades are in DB (all / today / week).
    """
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        scope = scope.lower()
        now_utc = datetime.utcnow()

        if scope in ("today", "day"):
            start = datetime(now_utc.year, now_utc.month, now_utc.day)
            end = start + timedelta(days=1)
            q = {"created_at": {"$gte": start, "$lt": end}}
            label = "today"
        elif scope in ("week", "weekly"):
            days_since_monday = now_utc.weekday()
            start = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=days_since_monday)
            end = start + timedelta(days=7)
            q = {"created_at": {"$gte": start, "$lt": end}}
            label = "this week"
        else:
            q = {}
            label = "all time"

        count = await trades_col.count_documents(q)
        await ctx.send(f"Logged trades {label}: **{count}**")

    except Exception:
        logger.exception("ERROR in rawcount")
        await ctx.send("Error while counting trades. Check logs.")


@bot.command(name="logtrade")
async def logtrade(ctx: commands.Context, symbol: str, pct: float, direction: str = None, classification: str = None, *, note: str = ""):
    """
    Manually log a trade if parsing fails.
    Usage: !logtrade TSLA 12.5 win scalp optional free-text note...
    """
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        sym = symbol.upper()
        d = direction.lower() if direction else None
        if d not in ("win", "loss", None):
            d = None
        c = classification.lower() if classification else None

        now_utc = datetime.utcnow()

        trade_doc: Dict[str, Any] = {
            "message_id": ctx.message.id,
            "channel_id": ctx.channel.id,
            "channel_name": getattr(ctx.channel, "name", "unknown"),
            "author_id": ctx.author.id,
            "author_name": str(ctx.author),
            "content": note or f"Manual log {sym} {pct:+.2f}%",
            "summary": note or f"{sym} {pct:+.2f}% {d or ''} {c or ''}",
            "pct": float(pct),
            "direction": d,
            "classification": c,
            "symbol": sym,
            "jump_url": ctx.message.jump_url,
            "entry_price": None,
            "exit_price": None,
            "timestamp": now_utc,
            "created_at": now_utc,
        }

        await trades_col.insert_one(trade_doc)

        embed = make_live_recap_embed(trade_doc)
        live_ch = get_channel(LIVE_RECAP_CHANNEL_ID) or ctx.channel
        await live_ch.send(embed=embed)

        await ctx.send(f"Manual trade logged for **{sym}** {pct:+.2f}%.")

    except Exception:
        logger.exception("ERROR in logtrade")
        await ctx.send("Error while logging manual trade. Check logs.")


@bot.command(name="edittrade")
async def edittrade(ctx: commands.Context, message_id: int, *fields: str):
    """
    Edit fields on a logged trade.
    Usage: !edittrade <message_id> pct=15.2 direction=win classification=scalp symbol=TSLA
    """
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        doc = await trades_col.find_one({"message_id": message_id})
        if not doc:
            await ctx.send("No logged trade found with that message_id.")
            return

        updates: Dict[str, Any] = {}
        for f in fields:
            if "=" not in f:
                continue
            key, val = f.split("=", 1)
            key = key.lower()
            val = val.strip()

            if key in ("pct", "strike", "entry_price", "exit_price"):
                try:
                    updates[key] = float(val)
                except ValueError:
                    continue
            elif key in ("symbol", "classification", "direction", "contract_type", "expiry", "option_type"):
                updates[key] = val.upper() if key == "symbol" else val.lower()
            elif key in ("summary", "content"):
                updates[key] = val

        if not updates:
            await ctx.send("No valid fields to update.")
            return

        await trades_col.update_one({"message_id": message_id}, {"$set": updates})
        updated = await trades_col.find_one({"message_id": message_id})

        embed = make_live_recap_embed(updated)
        embed.title = "✏️ Trade Updated"
        await ctx.send(embed=embed)

    except Exception:
        logger.exception("ERROR in edittrade")
        await ctx.send("Error while editing trade. Check logs.")


@bot.command(name="deletetrade")
async def deletetrade(ctx: commands.Context, message_id: int):
    """
    Delete a logged trade by its original message_id.
    """
    try:
        if not trades_col:
            await ctx.send("Database not configured.")
            return

        res = await trades_col.delete_one({"message_id": message_id})
        if res.deleted_count == 0:
            await ctx.send("No logged trade found with that message_id.")
        else:
            await ctx.send(f"Deleted logged trade with message_id `{message_id}`.")

    except Exception:
        logger.exception("ERROR in deletetrade")
        await ctx.send("Error while deleting trade. Check logs.")


@bot.command(name="helpbot")
async def helpbot(ctx: commands.Context):
    """
    Simple help so people know what exists.
    """
    embed = discord.Embed(
        title="📚 Keyword Logger Bot – Commands",
        color=discord.Color.teal(),
        timestamp=datetime.utcnow(),
    )
    embed.add_field(
        name="Recaps",
        value=(
            "`!daily` – Daily recap for today\n"
            "`!weekly` – Weekly recap (Mon–Sun)\n"
            "`!monthly` – Month-to-date recap"
        ),
        inline=False,
    )
    embed.add_field(
        name="Analytics",
        value=(
            "`!stats` – Lifetime stats & patterns\n"
            "`!ticker TSLA` – Per-ticker stats\n"
            "`!streaks` – Win/loss streaks\n"
            "`!rawcount [all|today|week]` – Count logged trades"
        ),
        inline=False,
    )
    embed.add_field(
        name="Logging & Maintenance",
        value=(
            "`!logtrade SYMBOL PCT [direction] [classification] [note...]`\n"
            "`!edittrade <message_id> field=value ...`\n"
            "`!deletetrade <message_id>`\n"
            "`!health` – Bot + DB health"
        ),
        inline=False,
    )
    embed.add_field(
        name="Debug (admins)",
        value="`!debugon` / `!debugoff` – Show why messages were ignored in watched channels.",
        inline=False,
    )
    embed.set_footer(text="Watched channels: " + ", ".join(f"#{cid}" for cid in WATCHED_CHANNEL_IDS))
    await ctx.send(embed=embed)


# ------------- SCHEDULED TASKS -------------

@tasks.loop(hours=24)
async def weekly_scheduler():
    """
    Runs once per day; if it's Friday (weekday == 4), auto-post weekly recap.
    """
    try:
        now_utc = datetime.utcnow()
        if now_utc.weekday() != 4:  # Friday
            return

        if not trades_col:
            logger.warning("weekly_scheduler: trades_col is None")
            return

        days_since_monday = now_utc.weekday()
        start_of_week = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=days_since_monday)
        end_of_week = start_of_week + timedelta(days=7)

        cursor = trades_col.find(
            {
                "created_at": {
                    "$gte": start_of_week,
                    "$lt": end_of_week,
                }
            }
        )
        trades: List[Dict[str, Any]] = []
        async for doc in cursor:
            doc["channel_name"] = doc.get("channel_name") or doc.get("channel") or "unknown"
            trades.append(doc)

        if not trades:
            logger.info("weekly_scheduler: no trades for this week.")
            return

        embed = make_weekly_embed(start_of_week, min(end_of_week, datetime.utcnow()), trades)
        weekly_ch = get_channel(WEEKLY_RECAP_CHANNEL_ID)
        if weekly_ch:
            await weekly_ch.send(embed=embed)
        else:
            logger.warning("weekly_scheduler: weekly channel not found")

    except Exception:
        logger.exception("ERROR in weekly_scheduler")


@weekly_scheduler.before_loop
async def before_weekly_scheduler():
    await bot.wait_until_ready()


# ------------- MAIN -------------

def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN env var is not set")
    if not MONGO_URI:
        logger.warning("MONGO_URI env var is not set; MongoDB features will not work.")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()