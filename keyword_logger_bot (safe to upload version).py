import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import re
from collections import Counter, defaultdict

import discord
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient

# ============================================================
# FILL THESE IN BEFORE RUNNING
# ============================================================

DISCORD_TOKEN = 
MONGO_URI = 

DB_NAME = "TradeRecap"
TRADES_COLLECTION_NAME = "trades"

WATCHED_CHANNEL_IDS = [
    1439856570061033565,
    1462285627000094896,
    1477839908562407485,
]

LIVE_RECAP_CHANNEL_ID = 1459297868861931715
DAILY_RECAP_CHANNEL_ID = 1459297868861931715
WEEKLY_RECAP_CHANNEL_ID = 1507958570640216245
LONG_TERM_STATS_CHANNEL_ID = 1508577446700777674
ANALYTICS_CHANNEL_ID = 1508577544075612220

COMMAND_PREFIX = "!"

# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trade_logger_bot")

DEBUG_IGNORED_MESSAGES = False

# ============================================================
# MONGO
# ============================================================

_use_mongo = bool(MONGO_URI and "YOUR_MONGODB_URI_HERE" not in MONGO_URI)
mongo_client = AsyncIOMotorClient(MONGO_URI) if _use_mongo else None
db = mongo_client[DB_NAME] if mongo_client is not None else None
trades_col = db[TRADES_COLLECTION_NAME] if db is not None else None

# ============================================================
# PARSING HELPERS
# ============================================================

pct_regex = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")
ticker_regex = re.compile(r"\b[A-Z]{2,6}\b")

options_pattern = re.compile(
    r"\b(?P<symbol>[A-Z]{2,6})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s*"
    r"(?P<cp>[cCpP])\b"
    r"(?:\s+|[@ ])"
    r"(?P<expiry>\d{1,2}/\d{1,2}(?:/\d{2,4})?)?"
)

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


def infer_direction(content: str, pct: Optional[float]) -> Optional[str]:
    lower = content.lower()

    strong_loss_words = [
        "loss", "lost", "cut", "stopped", "stopped out", "stop hit",
        "sl hit", "sl", "red", "bleed", "bleeding"
    ]
    strong_win_words = [
        "profit", "green", "tp", "tp1", "tp2", "tp3",
        "took profit", "secured", "bagged", "trimmed green"
    ]

    # Explicit loss wording takes priority
    if any(k in lower for k in strong_loss_words):
        return "loss"

    # Explicit profit wording next
    if any(k in lower for k in strong_win_words):
        return "win"

    # Finally rely on signed percentage
    if pct is not None:
        if pct < 0:
            return "loss"
        if pct > 0:
            return "win"

    return None


def infer_play_style(content: str) -> Optional[str]:
    lower = content.lower()
    if "scalp" in lower:
        return "scalp"
    if "swing" in lower:
        return "swing"
    if "lotto" in lower or "loto" in lower:
        return "lotto"
    return None


def infer_exit_label(content: str, direction: Optional[str]) -> Optional[str]:
    lower = content.lower()

    if re.search(r"\btp\s*1\b", lower) or "tp1" in lower:
        return "TP 1"
    if re.search(r"\btp\s*2\b", lower) or "tp2" in lower:
        return "TP 2"
    if re.search(r"\btp\s*3\b", lower) or "tp3" in lower:
        return "TP 3"

    partial_keywords = [
        "partial", "trim", "trimmed", "trimming", "scaled", "scale out",
        "took some", "locked some", "took half", "half out", "1/2", "1/3", "2/3"
    ]
    if any(k in lower for k in partial_keywords):
        return "Partial"

    if "runner" in lower or "runners" in lower:
        if direction == "win":
            return "Runner profit"
        if direction == "loss":
            return "Runner stopped"
        return "Runner"

    stop_keywords = ["sl", "stop", "stopped", "stop hit", "cut", "invalid", "invalidation", "loss"]
    if any(k in lower for k in stop_keywords):
        if any(k in lower for k in ["be", "break even", "breakeven", "b/e"]):
            return "Break-even stop"
        return "Stopped out"

    full_tp_keywords = [
        "all out", "closed remaining", "closed rest", "flat", "full tp",
        "tp hit", "took full", "took profit", "closed for", "sold all", "closed whole"
    ]
    if any(k in lower for k in full_tp_keywords):
        if direction == "win":
            return "Full take profit"
        if direction == "loss":
            return "Full cut"
        return "Full close"

    if direction == "win":
        return "Take profit"
    if direction == "loss":
        return "Loss close"

    return None


def guess_symbol_and_option_fields(content: str) -> Tuple[Optional[str], Dict[str, Any]]:
    fields: Dict[str, Any] = {
        "contract_type": None,
        "strike": None,
        "expiry": None,
        "option_type": None,
    }

    m = options_pattern.search(content)
    if m:
        fields["contract_type"] = "options"
        fields["strike"] = float(m.group("strike"))
        fields["expiry"] = m.group("expiry")
        fields["option_type"] = "call" if m.group("cp").lower() == "c" else "put"
        return m.group("symbol").upper(), fields

    blacklist = {"TP", "TP1", "TP2", "TP3", "SL", "ATM", "ITM", "OTM", "USD", "PDT", "PNL", "BE"}
    symbol = next((t.upper() for t in ticker_regex.findall(content) if t.upper() not in blacklist), None)

    lower = content.lower()
    if any(k in lower for k in ["calls", "puts", "contract", "contracts", "call", "put", "cons"]):
        fields["contract_type"] = "options"
    elif any(k in lower for k in ["shares", "equity", "stock"]):
        fields["contract_type"] = "shares"

    return symbol, fields


def extract_entry_exit(content: str) -> Tuple[Optional[float], Optional[float]]:
    entry = None
    exit_ = None

    m = entry_pattern.search(content)
    if m:
        try:
            entry = float(m.group(1))
        except ValueError:
            entry = None

    m = exit_pattern.search(content)
    if m:
        try:
            exit_ = float(m.group(1))
        except ValueError:
            exit_ = None

    return entry, exit_


def parse_trade_message_with_debug(message: discord.Message) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    reasons: List[str] = []

    if message.author.bot:
        reasons.append("author_is_bot")
        return None, reasons

    if message.channel.id not in WATCHED_CHANNEL_IDS:
        reasons.append("channel_not_watched")
        return None, reasons

    content = (message.content or "").strip()
    lower = content.lower()

    if not content:
        reasons.append("empty_message")
        return None, reasons

    close_keywords = [
        "closed", "took profit", "tp", "tp1", "tp2", "tp3", "sl", "stopped",
        "cut", "%", "sold", "trim", "partial", "runner", "profit", "loss"
    ]
    if not any(k in lower for k in close_keywords):
        reasons.append("no_close_keywords")
        return None, reasons

    raw_pct = extract_pct(content)
    pct = raw_pct
    direction = infer_direction(content, raw_pct)

    # Force negative pct if text clearly says loss and pct is unsigned positive
    if direction == "loss" and isinstance(raw_pct, (int, float)) and raw_pct > 0:
        pct = -abs(raw_pct)

    play_style = infer_play_style(content)
    exit_label = infer_exit_label(content, direction)
    symbol, extra_fields = guess_symbol_and_option_fields(content)
    entry_price, exit_price = extract_entry_exit(content)

    if pct is None:
        reasons.append("no_percentage_found")
    if direction is None:
        reasons.append("no_direction_inferred")
    if play_style is None:
        reasons.append("no_play_style_inferred")
    if exit_label is None:
        reasons.append("no_exit_label_inferred")
    if symbol is None:
        reasons.append("no_ticker_found")
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
        "classification": play_style,
        "exit_label": exit_label,
        "symbol": symbol,
        "jump_url": message.jump_url,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "timestamp": message.created_at.replace(tzinfo=None),
        "created_at": now_utc,
    }
    trade_doc.update(extra_fields)
    return trade_doc, reasons


def format_pct(p: Optional[float]) -> str:
    return f"{p:+.2f}%" if isinstance(p, (int, float)) else "N/A"


def format_trade_line(trade: Dict[str, Any]) -> str:
    symbol = trade.get("symbol") or "N/A"
    pct = format_pct(trade.get("pct"))
    direction = trade.get("direction") or "N/A"
    channel = trade.get("channel_name") or "unknown"
    jump_url = trade.get("jump_url")

    tags = []
    if trade.get("exit_label"):
        tags.append(trade["exit_label"])
    if trade.get("classification"):
        tags.append(trade["classification"])
    if trade.get("contract_type"):
        tags.append(trade["contract_type"])
    if trade.get("option_type"):
        tags.append(trade["option_type"])

    tag_str = f" `[{', '.join(tags)}]`" if tags else ""
    base = f"**{symbol}** {pct} `{direction}`{tag_str} · #{channel}"
    return f"[{base}]({jump_url})" if jump_url else base


def summarize_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    numeric = [t for t in trades if isinstance(t.get("pct"), (int, float))]
    pct_vals = [t["pct"] for t in numeric]
    total_pnl = sum(pct_vals) if pct_vals else 0.0

    return {
        "total_trades": len(trades),
        "wins": len([t for t in trades if t.get("direction") == "win"]),
        "losses": len([t for t in trades if t.get("direction") == "loss"]),
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / len(pct_vals) if pct_vals else 0.0,
        "best_trade": max(numeric, key=lambda x: x["pct"]) if numeric else None,
        "worst_trade": min(numeric, key=lambda x: x["pct"]) if numeric else None,
    }


def compute_streaks(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = sorted(
        [t for t in trades if t.get("direction") in ("win", "loss")],
        key=lambda t: t.get("created_at") or t.get("timestamp") or datetime.utcnow(),
    )

    longest_win = 0
    longest_loss = 0
    current_len = 0
    current_type = None

    for t in seq:
        d = t.get("direction")
        if d == current_type:
            current_len += 1
        else:
            current_type = d
            current_len = 1

        if current_type == "win":
            longest_win = max(longest_win, current_len)
        elif current_type == "loss":
            longest_loss = max(longest_loss, current_len)

    return {
        "longest_win": longest_win,
        "longest_loss": longest_loss,
        "current_type": current_type,
        "current_len": current_len,
    }


def make_daily_embed(date_label: str, trades: List[Dict[str, Any]]) -> discord.Embed:
    s = summarize_trades(trades)
    total = s["total_trades"]
    win_rate = (s["wins"] / total * 100.0) if total > 0 else 0.0

    embed = discord.Embed(
        title=f"📒 Daily Trade Recap – {date_label}",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Keyword Logger · Daily Recap")
    embed.add_field(
        name="Overview",
        value=(
            f"Trades: **{total}** · Wins: **{s['wins']}** · Losses: **{s['losses']}**\n"
            f"Win rate: **{win_rate:.1f}%**\n"
            f"Total PnL: **{s['total_pnl']:+.2f}%** · Avg PnL: **{s['avg_pnl']:+.2f}%**"
        ),
        inline=False,
    )

    lines = []
    if s["best_trade"]:
        lines.append(f"🏆 Best: {format_trade_line(s['best_trade'])}")
    if s["worst_trade"]:
        lines.append(f"💀 Worst: {format_trade_line(s['worst_trade'])}")
    if lines:
        embed.add_field(name="Highlights", value="\n".join(lines), inline=False)

    desc = "\n".join(f"• {format_trade_line(t)}" for t in trades)
    embed.description = (desc[:3800] + "\n… (truncated)") if len(desc) > 3800 else (desc or "No trades logged.")
    return embed


def make_weekly_embed(start_date: datetime, end_date: datetime, trades: List[Dict[str, Any]]) -> discord.Embed:
    s = summarize_trades(trades)
    total = s["total_trades"]
    win_rate = (s["wins"] / total * 100.0) if total > 0 else 0.0

    embed = discord.Embed(
        title="🗓️ Weekly PnL Recap",
        description=f"Week of **{start_date.date().isoformat()} → {end_date.date().isoformat()}**",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Keyword Logger · Weekly Recap")
    embed.add_field(
        name="Overview",
        value=(
            f"Trades: **{total}** · Wins: **{s['wins']}** · Losses: **{s['losses']}**\n"
            f"Win rate: **{win_rate:.1f}%**\n"
            f"Total PnL: **{s['total_pnl']:+.2f}%** · Avg PnL: **{s['avg_pnl']:+.2f}%**"
        ),
        inline=False,
    )

    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        ts = t.get("created_at") or t.get("timestamp") or datetime.utcnow()
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except Exception:
                ts = datetime.utcnow()
        by_day.setdefault(ts.date().isoformat(), []).append(t)

    for date_key in sorted(by_day):
        day_trades = by_day[date_key]
        day_s = summarize_trades(day_trades)
        lines = [f"Trades: **{day_s['total_trades']}** · PnL: **{day_s['total_pnl']:+.2f}%**", ""]
        lines += [f"• {format_trade_line(t)}" for t in day_trades]
        value = "\n".join(lines)
        embed.add_field(
            name=date_key,
            value=(value[:1000] + "\n… (truncated)") if len(value) > 1024 else value,
            inline=False,
        )

    lines = []
    if s["best_trade"]:
        lines.append(f"🏆 Best: {format_trade_line(s['best_trade'])}")
    if s["worst_trade"]:
        lines.append(f"💀 Worst: {format_trade_line(s['worst_trade'])}")
    if lines:
        embed.add_field(name="Highlights", value="\n".join(lines), inline=False)

    return embed


def make_live_recap_embed(trade: Dict[str, Any]) -> discord.Embed:
    symbol = trade.get("symbol") or "N/A"
    pct = trade.get("pct")
    direction = trade.get("direction") or "N/A"

    embed = discord.Embed(
        title=f"📊 Trade Close Logged – {symbol}",
        color=discord.Color.green() if direction == "win" else discord.Color.red(),
        timestamp=datetime.utcnow(),
    )

    trader_value = f"<@{trade.get('author_id')}>" if trade.get("author_id") else (trade.get("author_name") or "unknown")
    channel_value = f"<#{trade.get('channel_id')}>" if trade.get("channel_id") else f"#{trade.get('channel_name') or 'unknown'}"

    embed.add_field(name="Trader", value=trader_value, inline=True)
    embed.add_field(name="Channel", value=channel_value, inline=True)
    embed.add_field(name="Summary", value=trade.get("summary") or trade.get("content") or "No summary provided.", inline=False)

    classification_parts = []
    if trade.get("exit_label"):
        classification_parts.append(trade["exit_label"])
    if trade.get("classification"):
        classification_parts.append(trade["classification"].capitalize())
    if classification_parts:
        embed.add_field(name="Classification", value=" · ".join(classification_parts), inline=False)

    pct_str = format_pct(pct)
    if direction == "win":
        pnl_value = f"✅ {pct_str} profit"
    elif direction == "loss":
        pnl_value = f"❌ {pct_str} loss"
    else:
        pnl_value = f"➖ {pct_str}"
    embed.add_field(name="PnL", value=pnl_value, inline=False)

    details = []
    if trade.get("contract_type"):
        details.append(f"Contract: {trade['contract_type']}")
    if trade.get("option_type"):
        details.append(f"Option: {trade['option_type']}")
    if trade.get("strike") is not None:
        details.append(f"Strike: {trade['strike']}")
    if trade.get("expiry"):
        details.append(f"Expiry: {trade['expiry']}")
    if trade.get("entry_price") is not None:
        details.append(f"Entry: {trade['entry_price']}")
    if trade.get("exit_price") is not None:
        details.append(f"Exit: {trade['exit_price']}")
    if details:
        embed.add_field(name="Details", value="\n".join(details), inline=False)

    if trade.get("jump_url"):
        embed.add_field(name="Jump to Message", value=f"[Open in Discord]({trade['jump_url']})", inline=False)

    original = trade.get("content") or ""
    if len(original) > 90:
        original = original[:90] + "…"

    ts = trade.get("timestamp") or trade.get("created_at") or datetime.utcnow()
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except Exception:
            ts = datetime.utcnow()

    embed.set_footer(text=f"Original: {original} • {ts.strftime('%Y-%m-%d %I:%M %p UTC')}")
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
    return ch if isinstance(ch, discord.TextChannel) else None


# ============================================================
# BOT EVENTS
# ============================================================

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    for cid in WATCHED_CHANNEL_IDS + [
        LIVE_RECAP_CHANNEL_ID,
        DAILY_RECAP_CHANNEL_ID,
        WEEKLY_RECAP_CHANNEL_ID,
        LONG_TERM_STATS_CHANNEL_ID,
        ANALYTICS_CHANNEL_ID,
    ]:
        ch = get_channel(cid)
        if ch is not None:
            logger.info(f"✅ #{ch.name} ({cid})")
        else:
            logger.warning(f"⚠️ channel not found: {cid}")

    if trades_col is None:
        logger.warning("MongoDB not configured – DB features disabled.")
    else:
        logger.info(f"MongoDB connected: {DB_NAME}.{TRADES_COLLECTION_NAME}")

    if not weekly_scheduler.is_running():
        weekly_scheduler.start()

    if not daily_scheduler.is_running():
        daily_scheduler.start()


@bot.event
async def on_message(message: discord.Message):
    global DEBUG_IGNORED_MESSAGES
    await bot.process_commands(message)

    try:
        if trades_col is None:
            return

        trade_doc, debug_reasons = parse_trade_message_with_debug(message)

        if trade_doc is None:
            if DEBUG_IGNORED_MESSAGES and message.channel.id in WATCHED_CHANNEL_IDS:
                analytics_ch = get_channel(ANALYTICS_CHANNEL_ID) or message.channel
                try:
                    await analytics_ch.send(embed=make_debug_embed(message, debug_reasons))
                except Exception:
                    logger.exception("Failed to send debug embed")
            return

        existing = await trades_col.find_one({"message_id": trade_doc["message_id"]})
        if existing is not None:
            logger.info(f"Duplicate ignored: {trade_doc['message_id']}")
            return

        await trades_col.insert_one(trade_doc)
        logger.info(f"Logged: {trade_doc['author_name']} {trade_doc.get('symbol', '?')} {trade_doc.get('pct')}")

        live_ch = get_channel(LIVE_RECAP_CHANNEL_ID)
        if live_ch is not None:
            await live_ch.send(embed=make_live_recap_embed(trade_doc))

        try:
            await message.add_reaction("📒")
            await message.channel.send(
                f"Trade logged: {trade_doc.get('symbol') or 'N/A'} {format_pct(trade_doc.get('pct'))}",
                delete_after=10,
            )
        except Exception:
            logger.exception("Failed to send confirmation")

    except Exception:
        logger.exception("Error in on_message")


# ============================================================
# COMMANDS
# ============================================================

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send("Pong.")


@bot.command(name="health")
async def health(ctx: commands.Context):
    try:
        mongo_ok = False
        if trades_col is not None:
            await trades_col.estimated_document_count()
            mongo_ok = True

        watched = ", ".join(f"<#{cid}>" for cid in WATCHED_CHANNEL_IDS)
        await ctx.send(
            f"**Bot health check**\n"
            f"Discord: ✅\n"
            f"MongoDB: {'✅' if mongo_ok else '❌'}\n"
            f"Watched: {watched}\n"
            f"Live/Daily: <#{LIVE_RECAP_CHANNEL_ID}>\n"
            f"Weekly: <#{WEEKLY_RECAP_CHANNEL_ID}>\n"
            f"Stats: <#{LONG_TERM_STATS_CHANNEL_ID}>\n"
            f"Analytics: <#{ANALYTICS_CHANNEL_ID}>"
        )
    except Exception:
        logger.exception("health failed")
        await ctx.send("health check failed – see logs.")


@bot.command(name="debugon")
@commands.has_permissions(administrator=True)
async def debug_on(ctx: commands.Context):
    global DEBUG_IGNORED_MESSAGES
    DEBUG_IGNORED_MESSAGES = True
    await ctx.send("Debug mode **enabled**.")


@bot.command(name="debugoff")
@commands.has_permissions(administrator=True)
async def debug_off(ctx: commands.Context):
    global DEBUG_IGNORED_MESSAGES
    DEBUG_IGNORED_MESSAGES = False
    await ctx.send("Debug mode **disabled**.")


@bot.command(name="daily")
async def daily_summary(ctx: commands.Context):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        now_utc = datetime.utcnow()
        start_of_day = datetime(now_utc.year, now_utc.month, now_utc.day)
        end_of_day = start_of_day + timedelta(days=1)

        trades: List[Dict[str, Any]] = []
        async for doc in trades_col.find({"created_at": {"$gte": start_of_day, "$lt": end_of_day}}):
            doc["channel_name"] = doc.get("channel_name") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet for today.")
            return

        target = get_channel(DAILY_RECAP_CHANNEL_ID) or ctx.channel
        await target.send(embed=make_daily_embed(start_of_day.strftime("%Y-%m-%d"), trades))

    except Exception:
        logger.exception("ERROR in daily_summary")
        await ctx.send("Error generating daily summary.")


@bot.command(name="weekly")
async def weekly_summary(ctx: commands.Context):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        now_utc = datetime.utcnow()
        start = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=now_utc.weekday())
        end = start + timedelta(days=7)

        trades: List[Dict[str, Any]] = []
        async for doc in trades_col.find({"created_at": {"$gte": start, "$lt": end}}):
            doc["channel_name"] = doc.get("channel_name") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet for this week.")
            return

        weekly_ch = get_channel(WEEKLY_RECAP_CHANNEL_ID) or ctx.channel
        await weekly_ch.send(embed=make_weekly_embed(start, min(end, datetime.utcnow()), trades))

    except Exception:
        logger.exception("ERROR in weekly_summary")
        await ctx.send("Error generating weekly summary.")


@bot.command(name="monthly")
async def monthly_summary(ctx: commands.Context):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        now_utc = datetime.utcnow()
        start = datetime(now_utc.year, now_utc.month, 1)

        trades: List[Dict[str, Any]] = []
        async for doc in trades_col.find({"created_at": {"$gte": start, "$lt": now_utc}}):
            doc["channel_name"] = doc.get("channel_name") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet this month.")
            return

        embed = make_weekly_embed(start, now_utc, trades)
        embed.title = "📆 Month-To-Date PnL Recap"

        target = get_channel(LONG_TERM_STATS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in monthly_summary")
        await ctx.send("Error generating monthly summary.")


@bot.command(name="stats")
async def stats_command(ctx: commands.Context):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        trades: List[Dict[str, Any]] = []
        async for doc in trades_col.find({}):
            doc["channel_name"] = doc.get("channel_name") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet.")
            return

        s = summarize_trades(trades)
        total = s["total_trades"]
        wins = s["wins"]
        losses = s["losses"]
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        counts = Counter()
        pnl_by = defaultdict(float)
        for t in trades:
            sym = (t.get("symbol") or "N/A").upper()
            counts[sym] += 1
            if isinstance(t.get("pct"), (int, float)):
                pnl_by[sym] += t["pct"]

        top_count = counts.most_common(3)
        top_pnl = sorted(pnl_by.items(), key=lambda kv: kv[1], reverse=True)[:3]

        embed = discord.Embed(title="📊 Lifetime Trading Stats", color=discord.Color.purple(), timestamp=datetime.utcnow())
        embed.set_footer(text="Keyword Logger · Long-Term Stats")
        embed.add_field(
            name="Overview",
            value=(
                f"Trades: **{total}** · Wins: **{wins}** · Losses: **{losses}**\n"
                f"Win rate: **{win_rate:.1f}%**\n"
                f"Total PnL: **{s['total_pnl']:+.2f}%** · Avg PnL: **{s['avg_pnl']:+.2f}%**"
            ),
            inline=False,
        )
        if top_count:
            embed.add_field(name="Most Traded Tickers", value="\n".join(f"• {sym}: **{cnt}** trades" for sym, cnt in top_count), inline=True)
        if top_pnl:
            embed.add_field(name="Top PnL Tickers", value="\n".join(f"• {sym}: **{pnl:+.2f}%**" for sym, pnl in top_pnl), inline=True)

        lines = []
        if s["best_trade"]:
            lines.append(f"🏆 Best: {format_trade_line(s['best_trade'])}")
        if s["worst_trade"]:
            lines.append(f"💀 Worst: {format_trade_line(s['worst_trade'])}")
        if lines:
            embed.add_field(name="Highlights", value="\n".join(lines), inline=False)

        target = get_channel(LONG_TERM_STATS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in stats_command")
        await ctx.send("Error generating stats.")


@bot.command(name="ticker")
async def ticker_command(ctx: commands.Context, symbol: str):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        sym = symbol.upper()
        trades: List[Dict[str, Any]] = []
        async for doc in trades_col.find({"symbol": sym}):
            doc["channel_name"] = doc.get("channel_name") or "unknown"
            trades.append(doc)

        if not trades:
            await ctx.send(f"No trades logged yet for {sym}.")
            return

        s = summarize_trades(trades)
        total = s["total_trades"]
        wins = s["wins"]
        losses = s["losses"]
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        embed = discord.Embed(title=f"📌 Ticker Stats – {sym}", color=discord.Color.teal(), timestamp=datetime.utcnow())
        embed.set_footer(text="Keyword Logger · Ticker Analytics")
        embed.add_field(
            name="Overview",
            value=(
                f"Trades: **{total}** · Wins: **{wins}** · Losses: **{losses}**\n"
                f"Win rate: **{win_rate:.1f}%**\n"
                f"Total PnL: **{s['total_pnl']:+.2f}%** · Avg PnL: **{s['avg_pnl']:+.2f}%**"
            ),
            inline=False,
        )

        lines = []
        if s["best_trade"]:
            lines.append(f"🏆 Best: {format_trade_line(s['best_trade'])}")
        if s["worst_trade"]:
            lines.append(f"💀 Worst: {format_trade_line(s['worst_trade'])}")
        if lines:
            embed.add_field(name="Highlights", value="\n".join(lines), inline=False)

        sorted_t = sorted(trades, key=lambda t: t.get("created_at") or t.get("timestamp") or datetime.utcnow(), reverse=True)
        desc = "\n".join(f"• {format_trade_line(t)}" for t in sorted_t[:10])
        embed.description = (desc[:3800] + "\n… (truncated)") if len(desc) > 3800 else desc

        target = get_channel(LONG_TERM_STATS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in ticker_command")
        await ctx.send("Error generating ticker stats.")


@bot.command(name="streaks")
async def streaks_command(ctx: commands.Context):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        trades: List[Dict[str, Any]] = []
        async for doc in trades_col.find({}):
            trades.append(doc)

        if not trades:
            await ctx.send("No trades logged yet.")
            return

        s = compute_streaks(trades)
        embed = discord.Embed(title="🔥 Streak Stats", color=discord.Color.orange(), timestamp=datetime.utcnow())
        embed.set_footer(text="Keyword Logger · Streaks")
        current = f"Current streak: **{s['current_len']} {s['current_type']}**" if s["current_len"] > 0 else "Current streak: none"
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
        await ctx.send("Error generating streak stats.")


@bot.command(name="rawcount")
async def rawcount(ctx: commands.Context, scope: str = "all"):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        scope = scope.lower()
        now_utc = datetime.utcnow()

        if scope in ("today", "day"):
            start = datetime(now_utc.year, now_utc.month, now_utc.day)
            q = {"created_at": {"$gte": start, "$lt": start + timedelta(days=1)}}
            label = "today"
        elif scope in ("week", "weekly"):
            start = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=now_utc.weekday())
            q = {"created_at": {"$gte": start, "$lt": start + timedelta(days=7)}}
            label = "this week"
        else:
            q = {}
            label = "all time"

        count = await trades_col.count_documents(q)
        await ctx.send(f"Logged trades {label}: **{count}**")

    except Exception:
        logger.exception("ERROR in rawcount")
        await ctx.send("Error counting trades.")


@bot.command(name="logtrade")
async def logtrade(ctx: commands.Context, symbol: str, pct: float, direction: str = None, classification: str = None, *, note: str = ""):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        sym = symbol.upper()
        d = direction.lower() if direction else None
        if d not in ("win", "loss", None):
            d = None
        c = classification.lower() if classification else None

        if d == "loss" and pct > 0:
            pct = -abs(pct)

        now = datetime.utcnow()
        exit_label = infer_exit_label(note or "", d)

        doc: Dict[str, Any] = {
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
            "exit_label": exit_label,
            "symbol": sym,
            "jump_url": ctx.message.jump_url,
            "entry_price": None,
            "exit_price": None,
            "timestamp": now,
            "created_at": now,
        }

        await trades_col.insert_one(doc)
        live_ch = get_channel(LIVE_RECAP_CHANNEL_ID) or ctx.channel
        await live_ch.send(embed=make_live_recap_embed(doc))
        await ctx.send(f"Manual trade logged for **{sym}** {pct:+.2f}%.")

    except Exception:
        logger.exception("ERROR in logtrade")
        await ctx.send("Error logging manual trade.")


@bot.command(name="edittrade")
async def edittrade(ctx: commands.Context, message_id: int, *fields: str):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        doc = await trades_col.find_one({"message_id": message_id})
        if doc is None:
            await ctx.send("No trade found with that message_id.")
            return

        updates: Dict[str, Any] = {}
        for f in fields:
            if "=" not in f:
                continue
            key, val = f.split("=", 1)
            key = key.lower().strip()
            val = val.strip()

            if key in ("pct", "strike", "entry_price", "exit_price"):
                try:
                    num = float(val)
                    if key == "pct" and doc.get("direction") == "loss" and num > 0:
                        num = -abs(num)
                    updates[key] = num
                except Exception:
                    pass
            elif key in ("symbol", "classification", "direction", "contract_type", "expiry", "option_type", "exit_label"):
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
        await ctx.send("Error editing trade.")


@bot.command(name="deletetrade")
async def deletetrade(ctx: commands.Context, message_id: int):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        res = await trades_col.delete_one({"message_id": message_id})
        if res.deleted_count == 0:
            await ctx.send("No trade found with that message_id.")
        else:
            await ctx.send(f"Deleted trade `{message_id}`.")

    except Exception:
        logger.exception("ERROR in deletetrade")
        await ctx.send("Error deleting trade.")


@bot.command(name="helpbot")
async def helpbot(ctx: commands.Context):
    embed = discord.Embed(title="📚 Keyword Logger Bot – Commands", color=discord.Color.teal(), timestamp=datetime.utcnow())
    embed.add_field(name="Recaps", value="`!daily` `!weekly` `!monthly`", inline=False)
    embed.add_field(name="Analytics", value="`!stats` `!ticker TSLA` `!streaks` `!rawcount [all|today|week]`", inline=False)
    embed.add_field(
        name="Logging",
        value=(
            "`!logtrade SYMBOL PCT [win|loss] [scalp|swing|lotto] [note...]`\n"
            "`!edittrade message_id field=value ...`\n"
            "`!deletetrade message_id`\n"
            "`!health`"
        ),
        inline=False,
    )
    embed.add_field(name="Debug (admins)", value="`!debugon` `!debugoff`", inline=False)
    embed.set_footer(text="Watched: " + " | ".join(str(c) for c in WATCHED_CHANNEL_IDS))
    await ctx.send(embed=embed)


# ============================================================
# SCHEDULED TASKS
# ============================================================

@tasks.loop(hours=24)
async def weekly_scheduler():
    try:
        now_utc = datetime.utcnow()
        if now_utc.weekday() != 4:
            return
        if trades_col is None:
            return

        start = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=now_utc.weekday())
        end = start + timedelta(days=7)

        trades: List[Dict[str, Any]] = []
        async for doc in trades_col.find({"created_at": {"$gte": start, "$lt": end}}):
            doc["channel_name"] = doc.get("channel_name") or "unknown"
            trades.append(doc)

        if not trades:
            logger.info("weekly_scheduler: no trades.")
            return

        ch = get_channel(WEEKLY_RECAP_CHANNEL_ID)
        if ch is not None:
            await ch.send(embed=make_weekly_embed(start, min(end, datetime.utcnow()), trades))
        else:
            logger.warning("weekly_scheduler: channel not found")

    except Exception:
        logger.exception("ERROR in weekly_scheduler")


@weekly_scheduler.before_loop
async def before_weekly():
    await bot.wait_until_ready()


@tasks.loop(hours=24)
async def daily_scheduler():
    try:
        if trades_col is None:
            return

        now_utc = datetime.utcnow()
        start = datetime(now_utc.year, now_utc.month, now_utc.day)
        end = start + timedelta(days=1)

        trades: List[Dict[str, Any]] = []
        async for doc in trades_col.find({"created_at": {"$gte": start, "$lt": end}}):
            doc["channel_name"] = doc.get("channel_name") or "unknown"
            trades.append(doc)

        if not trades:
            logger.info("daily_scheduler: no trades.")
            return

        ch = get_channel(DAILY_RECAP_CHANNEL_ID)
        if ch is not None:
            await ch.send(embed=make_daily_embed(start.strftime("%Y-%m-%d"), trades))
        else:
            logger.warning("daily_scheduler: channel not found")

    except Exception:
        logger.exception("ERROR in daily_scheduler")


@daily_scheduler.before_loop
async def before_daily():
    await bot.wait_until_ready()


# ============================================================
# MAIN
# ============================================================

def main():
    if not DISCORD_TOKEN or "YOUR_DISCORD_BOT_TOKEN_HERE" in DISCORD_TOKEN:
        raise RuntimeError("Paste your real Discord bot token into DISCORD_TOKEN at the top of the script.")
    if not MONGO_URI or "YOUR_MONGODB_URI_HERE" in MONGO_URI:
        logger.warning("MONGO_URI is not set correctly – DB features will not work.")

    logger.info("Starting bot...")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()