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

DISCORD_TOKEN = ""
MONGO_URI = ""

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
# STORAGE / CLEANUP SETTINGS
# ============================================================

MAX_DB_SIZE_MB = 512
AUTO_CLEANUP_ENABLED = True
AUTO_CLEANUP_INTERVAL_HOURS = 12

# Conservative document count guardrails to stay well below 512 MB
TARGET_MAX_DOCUMENTS = 25000
TRIM_TO_DOCUMENTS = 22000

# Also delete very old trades automatically
MAX_TRADE_AGE_DAYS = 180

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

    if any(k in lower for k in strong_loss_words):
        return "loss"

    if any(k in lower for k in strong_win_words):
        return "win"

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
        return "Take Profit 1"
    if re.search(r"\btp\s*2\b", lower) or "tp2" in lower:
        return "Take Profit 2"
    if re.search(r"\btp\s*3\b", lower) or "tp3" in lower:
        return "Take Profit 3"
    if re.search(r"\btp\s*4\b", lower) or "tp4" in lower:
        return "Take Profit 4"

    partial_keywords = [
        "partial", "trim", "trimmed", "trimming", "scaled", "scale out",
        "took some", "locked some", "took half", "half out", "1/2", "1/3", "2/3"
    ]
    if any(k in lower for k in partial_keywords):
        return "Partial Trim"

    if "runner" in lower or "runners" in lower:
        if direction == "win":
            return "Runner Profit"
        if direction == "loss":
            return "Runner Stopped"
        return "Runner"

    stop_keywords = ["sl", "stop", "stopped", "stop hit", "cut", "invalid", "invalidation", "loss"]
    if any(k in lower for k in stop_keywords):
        if any(k in lower for k in ["be", "break even", "breakeven", "b/e"]):
            return "Break-Even Stop"
        return "Stopped Out"

    full_tp_keywords = [
        "all out", "closed remaining", "closed rest", "flat", "full tp",
        "tp hit", "took full", "took profit", "closed for", "sold all", "closed whole"
    ]
    if any(k in lower for k in full_tp_keywords):
        if direction == "win":
            return "Full Take Profit"
        if direction == "loss":
            return "Full Cut"
        return "Full Close"

    if direction == "win":
        return "Take Profit"
    if direction == "loss":
        return "Loss Close"

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

    blacklist = {"TP", "TP1", "TP2", "TP3", "TP4", "SL", "ATM", "ITM", "OTM", "USD", "PDT", "PNL", "BE"}
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
        "closed", "took profit", "tp", "tp1", "tp2", "tp3", "tp4", "sl", "stopped",
        "cut", "%", "sold", "trim", "partial", "runner", "profit", "loss"
    ]
    if not any(k in lower for k in close_keywords):
        reasons.append("no_close_keywords")
        return None, reasons

    raw_pct = extract_pct(content)
    pct = raw_pct
    direction = infer_direction(content, raw_pct)

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


def format_style_label(style: Optional[str]) -> Optional[str]:
    if not style:
        return None
    mapping = {
        "scalp": "Scalp",
        "swing": "Swing",
        "lotto": "Lotto",
    }
    return mapping.get(style.lower(), style.title())


def format_trade_line(trade: Dict[str, Any]) -> str:
    symbol = trade.get("symbol") or "N/A"
    pct = format_pct(trade.get("pct"))
    direction = (trade.get("direction") or "N/A").title()
    channel = trade.get("channel_name") or "unknown"
    jump_url = trade.get("jump_url")

    tags = []
    if trade.get("exit_label"):
        tags.append(trade["exit_label"])
    style_label = format_style_label(trade.get("classification"))
    if style_label:
        tags.append(style_label)
    if trade.get("contract_type"):
        tags.append(str(trade["contract_type"]).title())
    if trade.get("option_type"):
        tags.append(str(trade["option_type"]).title())

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


async def fetch_all_trades(query: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    if trades_col is None:
        return []

    trades: List[Dict[str, Any]] = []
    async for doc in trades_col.find(query or {}):
        doc["channel_name"] = doc.get("channel_name") or "unknown"
        trades.append(doc)
    return trades


async def estimate_storage_stats() -> Dict[str, Any]:
    if db is None:
        return {"ok": False, "reason": "db_not_configured"}

    try:
        stats = await db.command("dbStats")
        data_size = stats.get("dataSize", 0)
        storage_size = stats.get("storageSize", 0)
        index_size = stats.get("indexSize", 0)
        total_size = data_size + index_size
        return {
            "ok": True,
            "data_size_bytes": data_size,
            "storage_size_bytes": storage_size,
            "index_size_bytes": index_size,
            "total_estimated_bytes": total_size,
            "total_estimated_mb": total_size / (1024 * 1024),
        }
    except Exception:
        logger.exception("Failed to read dbStats")
        return {"ok": False, "reason": "dbStats_failed"}


def make_daily_embed(date_label: str, trades: List[Dict[str, Any]]) -> discord.Embed:
    s = summarize_trades(trades)
    total = s["total_trades"]
    win_rate = (s["wins"] / total * 100.0) if total > 0 else 0.0

    embed = discord.Embed(
        title=f"📒 Daily Trade Recap • {date_label}",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Keyword Logger • Daily Recap")
    embed.add_field(
        name="Overview",
        value=(
            f"Trades: **{total}** • Wins: **{s['wins']}** • Losses: **{s['losses']}**\n"
            f"Win Rate: **{win_rate:.1f}%**\n"
            f"Total PnL: **{s['total_pnl']:+.2f}%** • Avg PnL: **{s['avg_pnl']:+.2f}%**"
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
    embed.set_footer(text="Keyword Logger • Weekly Recap")
    embed.add_field(
        name="Overview",
        value=(
            f"Trades: **{total}** • Wins: **{s['wins']}** • Losses: **{s['losses']}**\n"
            f"Win Rate: **{win_rate:.1f}%**\n"
            f"Total PnL: **{s['total_pnl']:+.2f}%** • Avg PnL: **{s['avg_pnl']:+.2f}%**"
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
        lines = [f"Trades: **{day_s['total_trades']}** • PnL: **{day_s['total_pnl']:+.2f}%**", ""]
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
        title=f"📊 Trade Close Logged • {symbol}",
        color=discord.Color.green() if direction == "win" else discord.Color.red(),
        timestamp=datetime.utcnow(),
    )

    trader_value = f"<@{trade.get('author_id')}>" if trade.get("author_id") else (trade.get("author_name") or "unknown")
    channel_value = f"<#{trade.get('channel_id')}>" if trade.get("channel_id") else f"#{trade.get('channel_name') or 'unknown'}"

    embed.add_field(name="Trader", value=trader_value, inline=True)
    embed.add_field(name="Channel", value=channel_value, inline=True)

    style_label = format_style_label(trade.get("classification"))
    classification_parts = []
    if trade.get("exit_label"):
        classification_parts.append(trade["exit_label"])
    if style_label:
        classification_parts.append(style_label)
    if classification_parts:
        embed.add_field(name="Classification", value=" • ".join(classification_parts), inline=False)

    pct_str = format_pct(pct)
    if direction == "win":
        pnl_value = f"✅ {pct_str} Profit"
    elif direction == "loss":
        pnl_value = f"❌ {pct_str} Loss"
    else:
        pnl_value = f"➖ {pct_str}"
    embed.add_field(name="PnL", value=pnl_value, inline=False)

    details = []
    if trade.get("contract_type"):
        details.append(f"Contract: {str(trade['contract_type']).title()}")
    if trade.get("option_type"):
        details.append(f"Option: {str(trade['option_type']).title()}")
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

    embed.add_field(
        name="Summary",
        value=trade.get("summary") or trade.get("content") or "No summary provided.",
        inline=False,
    )

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
    embed.set_footer(text="Keyword Logger • Debug")
    return embed


def make_storage_embed(stats: Dict[str, Any], count: int) -> discord.Embed:
    used_mb = stats.get("total_estimated_mb", 0.0)
    pct_used = (used_mb / MAX_DB_SIZE_MB * 100.0) if MAX_DB_SIZE_MB > 0 else 0.0

    embed = discord.Embed(
        title="💾 Storage Estimate",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Documents", value=str(count), inline=True)
    embed.add_field(name="Estimated Used", value=f"{used_mb:.2f} MB", inline=True)
    embed.add_field(name="512 MB Limit", value=f"{pct_used:.1f}%", inline=True)
    embed.add_field(
        name="Breakdown",
        value=(
            f"Data: {stats.get('data_size_bytes', 0) / (1024 * 1024):.2f} MB\n"
            f"Indexes: {stats.get('index_size_bytes', 0) / (1024 * 1024):.2f} MB\n"
            f"Storage: {stats.get('storage_size_bytes', 0) / (1024 * 1024):.2f} MB"
        ),
        inline=False,
    )
    embed.set_footer(text="Estimate only — Atlas or disk usage may differ slightly.")
    return embed


def make_analytics_embed(trades: List[Dict[str, Any]], title: str = "📈 Pattern Analytics") -> discord.Embed:
    s = summarize_trades(trades)
    total = s["total_trades"]
    win_rate = (s["wins"] / total * 100.0) if total > 0 else 0.0

    embed = discord.Embed(
        title=title,
        color=discord.Color.dark_teal(),
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Keyword Logger • Analytics")

    embed.add_field(
        name="Overview",
        value=(
            f"Trades: **{total}**\n"
            f"Wins: **{s['wins']}** • Losses: **{s['losses']}**\n"
            f"Win Rate: **{win_rate:.1f}%**\n"
            f"Total PnL: **{s['total_pnl']:+.2f}%** • Avg PnL: **{s['avg_pnl']:+.2f}%**"
        ),
        inline=False,
    )

    scalps = [t for t in trades if t.get("classification") == "scalp"]
    swings = [t for t in trades if t.get("classification") == "swing"]
    lottos = [t for t in trades if t.get("classification") == "lotto"]

    def calc_wr(lst: List[Dict[str, Any]]) -> float:
        if not lst:
            return 0.0
        wins = len([t for t in lst if t.get("direction") == "win"])
        return wins / len(lst) * 100.0

    def calc_avg(lst: List[Dict[str, Any]]) -> float:
        vals = [t["pct"] for t in lst if isinstance(t.get("pct"), (int, float))]
        return sum(vals) / len(vals) if vals else 0.0

    style_lines = [
        f"Scalps: **{len(scalps)}** trades • WR **{calc_wr(scalps):.1f}%** • Avg **{calc_avg(scalps):+.2f}%**",
        f"Swings: **{len(swings)}** trades • WR **{calc_wr(swings):.1f}%** • Avg **{calc_avg(swings):+.2f}%**",
        f"Lottos: **{len(lottos)}** trades • WR **{calc_wr(lottos):.1f}%** • Avg **{calc_avg(lottos):+.2f}%**",
    ]
    embed.add_field(name="By Style", value="\n".join(style_lines), inline=False)

    exit_groups = defaultdict(list)
    for t in trades:
        label = t.get("exit_label") or "Unknown"
        exit_groups[label].append(t)

    top_exit_lines = []
    for label, lst in sorted(exit_groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]:
        top_exit_lines.append(
            f"{label}: **{len(lst)}** trades • WR **{calc_wr(lst):.1f}%** • Avg **{calc_avg(lst):+.2f}%**"
        )
    if top_exit_lines:
        embed.add_field(name="By Exit Pattern", value="\n".join(top_exit_lines), inline=False)

    ticker_counts = Counter()
    ticker_pnl = defaultdict(float)
    for t in trades:
        sym = (t.get("symbol") or "N/A").upper()
        ticker_counts[sym] += 1
        if isinstance(t.get("pct"), (int, float)):
            ticker_pnl[sym] += t["pct"]

    most_traded = ticker_counts.most_common(5)
    if most_traded:
        embed.add_field(
            name="Most Traded",
            value="\n".join(f"{sym}: **{cnt}** trades" for sym, cnt in most_traded),
            inline=True,
        )

    best_tickers = sorted(ticker_pnl.items(), key=lambda kv: kv[1], reverse=True)[:5]
    if best_tickers:
        embed.add_field(
            name="Best Tickers",
            value="\n".join(f"{sym}: **{pnl:+.2f}%**" for sym, pnl in best_tickers),
            inline=True,
        )

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

    if dow_stats:
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        ordered = sorted(dow_stats.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
        best_day = ordered[0]
        worst_day = ordered[-1]
        embed.add_field(
            name="Day Patterns",
            value=(
                f"Best Day: **{day_names[best_day[0]]}** ({best_day[1]['pnl']:+.2f}%)\n"
                f"Worst Day: **{day_names[worst_day[0]]}** ({worst_day[1]['pnl']:+.2f}%)"
            ),
            inline=False,
        )

    streaks = compute_streaks(trades)
    current_label = streaks["current_type"].title() if streaks["current_type"] else "None"
    embed.add_field(
        name="Streaks",
        value=(
            f"Longest Win Streak: **{streaks['longest_win']}**\n"
            f"Longest Loss Streak: **{streaks['longest_loss']}**\n"
            f"Current: **{streaks['current_len']} {current_label}**"
        ),
        inline=False,
    )

    best = s["best_trade"]
    worst = s["worst_trade"]
    lines = []
    if best:
        lines.append(f"🏆 Best: {format_trade_line(best)}")
    if worst:
        lines.append(f"💀 Worst: {format_trade_line(worst)}")
    if lines:
        embed.add_field(name="Highlights", value="\n".join(lines), inline=False)

    return embed


def get_channel(channel_id: int) -> Optional[discord.TextChannel]:
    ch = bot.get_channel(channel_id)
    return ch if isinstance(ch, discord.TextChannel) else None


async def run_auto_cleanup(reason: str = "scheduled") -> Dict[str, Any]:
    if trades_col is None:
        return {"ok": False, "reason": "db_not_configured"}

    deleted_total = 0

    cutoff = datetime.utcnow() - timedelta(days=MAX_TRADE_AGE_DAYS)
    age_result = await trades_col.delete_many({"created_at": {"$lt": cutoff}})
    deleted_total += age_result.deleted_count

    total_count = await trades_col.count_documents({})
    if total_count > TARGET_MAX_DOCUMENTS:
        to_remove = total_count - TRIM_TO_DOCUMENTS
        cursor = trades_col.find({}, {"_id": 1}).sort("created_at", 1).limit(to_remove)
        ids_to_delete = []
        async for doc in cursor:
            ids_to_delete.append(doc["_id"])
        if ids_to_delete:
            trim_result = await trades_col.delete_many({"_id": {"$in": ids_to_delete}})
            deleted_total += trim_result.deleted_count

    total_count_after = await trades_col.count_documents({})
    stats = await estimate_storage_stats()

    return {
        "ok": True,
        "reason": reason,
        "deleted_total": deleted_total,
        "count_after": total_count_after,
        "stats": stats,
    }


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

    if AUTO_CLEANUP_ENABLED and not cleanup_scheduler.is_running():
        cleanup_scheduler.start()

    if not analytics_scheduler.is_running():
        analytics_scheduler.start()


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

        trades = await fetch_all_trades({"created_at": {"$gte": start_of_day, "$lt": end_of_day}})
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

        trades = await fetch_all_trades({"created_at": {"$gte": start, "$lt": end}})
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

        trades = await fetch_all_trades({"created_at": {"$gte": start, "$lt": now_utc}})
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

        trades = await fetch_all_trades()
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
        embed.set_footer(text="Keyword Logger • Long-Term Stats")
        embed.add_field(
            name="Overview",
            value=(
                f"Trades: **{total}** • Wins: **{wins}** • Losses: **{losses}**\n"
                f"Win Rate: **{win_rate:.1f}%**\n"
                f"Total PnL: **{s['total_pnl']:+.2f}%** • Avg PnL: **{s['avg_pnl']:+.2f}%**"
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


@bot.command(name="analytics")
async def analytics_command(ctx: commands.Context, scope: str = "all", value: str = None):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        scope = scope.lower()
        now_utc = datetime.utcnow()
        query: Dict[str, Any] = {}
        title = "📈 Pattern Analytics"

        if scope in ("today", "day"):
            start = datetime(now_utc.year, now_utc.month, now_utc.day)
            end = start + timedelta(days=1)
            query = {"created_at": {"$gte": start, "$lt": end}}
            title = f"📈 Pattern Analytics • {start.date().isoformat()}"
        elif scope in ("week", "weekly"):
            start = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=now_utc.weekday())
            end = start + timedelta(days=7)
            query = {"created_at": {"$gte": start, "$lt": end}}
            title = "📈 Pattern Analytics • Week"
        elif scope in ("month", "monthly"):
            start = datetime(now_utc.year, now_utc.month, 1)
            query = {"created_at": {"$gte": start, "$lt": now_utc}}
            title = "📈 Pattern Analytics • Month"
        elif scope == "ticker" and value:
            query = {"symbol": value.upper()}
            title = f"📈 Pattern Analytics • {value.upper()}"
        elif scope == "date" and value:
            try:
                start = datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                await ctx.send("Use `!analytics date YYYY-MM-DD`.")
                return
            end = start + timedelta(days=1)
            query = {"created_at": {"$gte": start, "$lt": end}}
            title = f"📈 Pattern Analytics • {value}"
        else:
            title = "📈 Pattern Analytics • All Time"

        trades = await fetch_all_trades(query)
        if not trades:
            await ctx.send("No trades found for that analytics scope.")
            return

        embed = make_analytics_embed(trades, title=title)
        target = get_channel(ANALYTICS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in analytics_command")
        await ctx.send("Error generating analytics.")


@bot.command(name="ticker")
async def ticker_command(ctx: commands.Context, symbol: str):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        sym = symbol.upper()
        trades = await fetch_all_trades({"symbol": sym})

        if not trades:
            await ctx.send(f"No trades logged yet for {sym}.")
            return

        s = summarize_trades(trades)
        total = s["total_trades"]
        wins = s["wins"]
        losses = s["losses"]
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        embed = discord.Embed(title=f"📌 Ticker Stats • {sym}", color=discord.Color.teal(), timestamp=datetime.utcnow())
        embed.set_footer(text="Keyword Logger • Ticker Analytics")
        embed.add_field(
            name="Overview",
            value=(
                f"Trades: **{total}** • Wins: **{wins}** • Losses: **{losses}**\n"
                f"Win Rate: **{win_rate:.1f}%**\n"
                f"Total PnL: **{s['total_pnl']:+.2f}%** • Avg PnL: **{s['avg_pnl']:+.2f}%**"
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

        trades = await fetch_all_trades()
        if not trades:
            await ctx.send("No trades logged yet.")
            return

        s = compute_streaks(trades)
        embed = discord.Embed(title="🔥 Streak Stats", color=discord.Color.orange(), timestamp=datetime.utcnow())
        embed.set_footer(text="Keyword Logger • Streaks")
        current_label = s["current_type"].title() if s["current_type"] else "None"
        current = f"Current Streak: **{s['current_len']} {current_label}**" if s["current_len"] > 0 else "Current Streak: none"
        embed.add_field(
            name="Streaks",
            value=(
                f"Longest Win Streak: **{s['longest_win']}**\n"
                f"Longest Loss Streak: **{s['longest_loss']}**\n"
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


@bot.command(name="storage")
async def storage_command(ctx: commands.Context):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        count = await trades_col.count_documents({})
        stats = await estimate_storage_stats()
        if not stats.get("ok"):
            await ctx.send(f"Could not estimate storage: {stats.get('reason')}")
            return

        embed = make_storage_embed(stats, count)
        target = get_channel(ANALYTICS_CHANNEL_ID) or ctx.channel
        await target.send(embed=embed)

    except Exception:
        logger.exception("ERROR in storage_command")
        await ctx.send("Error reading storage stats.")


@bot.command(name="cleanup")
@commands.has_permissions(administrator=True)
async def cleanup_command(ctx: commands.Context):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        result = await run_auto_cleanup(reason="manual_cleanup")
        if not result.get("ok"):
            await ctx.send("Cleanup failed.")
            return

        stats = result.get("stats", {})
        used_mb = stats.get("total_estimated_mb", 0.0) if stats.get("ok") else 0.0
        await ctx.send(
            f"Cleanup complete.\n"
            f"Deleted: **{result['deleted_total']}**\n"
            f"Remaining docs: **{result['count_after']}**\n"
            f"Estimated size: **{used_mb:.2f} MB**"
        )

    except Exception:
        logger.exception("ERROR in cleanup_command")
        await ctx.send("Error running cleanup.")


@bot.command(name="deleteday")
@commands.has_permissions(administrator=True)
async def delete_day_command(ctx: commands.Context, day_str: str):
    try:
        if trades_col is None:
            await ctx.send("Database not configured.")
            return

        try:
            start = datetime.strptime(day_str, "%Y-%m-%d")
        except ValueError:
            await ctx.send("Use format: `!deleteday YYYY-MM-DD`")
            return

        end = start + timedelta(days=1)
        count = await trades_col.count_documents({"created_at": {"$gte": start, "$lt": end}})
        if count == 0:
            await ctx.send(f"No logs found for **{day_str}**.")
            return

        res = await trades_col.delete_many({"created_at": {"$gte": start, "$lt": end}})
        await ctx.send(f"Deleted **{res.deleted_count}** trade logs from **{day_str}**.")

        analytics_ch = get_channel(ANALYTICS_CHANNEL_ID)
        if analytics_ch is not None:
            await analytics_ch.send(f"🗑️ Manual daily purge completed for **{day_str}** • Removed **{res.deleted_count}** logs.")

    except Exception:
        logger.exception("ERROR in delete_day_command")
        await ctx.send("Error deleting that day of logs.")


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
            elif key in ("symbol", "classification", "direction", "contract_type", "expiry", "option_type"):
                updates[key] = val.upper() if key == "symbol" else val.lower()
            elif key == "exit_label":
                updates[key] = val
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
    embed = discord.Embed(title="📚 Keyword Logger Bot • Commands", color=discord.Color.teal(), timestamp=datetime.utcnow())
    embed.add_field(name="Recaps", value="`!daily` `!weekly` `!monthly`", inline=False)
    embed.add_field(
        name="Analytics",
        value=(
            "`!stats`\n"
            "`!analytics [all|today|week|month|ticker SYMBOL|date YYYY-MM-DD]`\n"
            "`!ticker TSLA`\n"
            "`!streaks`\n"
            "`!rawcount [all|today|week]`\n"
            "`!storage`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Logging / Cleanup",
        value=(
            "`!logtrade SYMBOL PCT [win|loss] [scalp|swing|lotto] [note...]`\n"
            "`!edittrade message_id field=value ...`\n"
            "`!deletetrade message_id`\n"
            "`!deleteday YYYY-MM-DD` *(admin)*\n"
            "`!cleanup` *(admin)*\n"
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

        trades = await fetch_all_trades({"created_at": {"$gte": start, "$lt": end}})
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

        trades = await fetch_all_trades({"created_at": {"$gte": start, "$lt": end}})
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


@tasks.loop(hours=AUTO_CLEANUP_INTERVAL_HOURS)
async def cleanup_scheduler():
    try:
        if trades_col is None or not AUTO_CLEANUP_ENABLED:
            return

        result = await run_auto_cleanup(reason="scheduled_cleanup")
        if not result.get("ok"):
            return

        analytics_ch = get_channel(ANALYTICS_CHANNEL_ID)
        if analytics_ch is not None and (result["deleted_total"] > 0 or result["stats"].get("ok")):
            used_mb = result["stats"].get("total_estimated_mb", 0.0) if result["stats"].get("ok") else 0.0
            await analytics_ch.send(
                f"🧹 Auto cleanup check complete.\n"
                f"Deleted: **{result['deleted_total']}**\n"
                f"Remaining docs: **{result['count_after']}**\n"
                f"Estimated size: **{used_mb:.2f} MB / {MAX_DB_SIZE_MB} MB**"
            )

    except Exception:
        logger.exception("ERROR in cleanup_scheduler")


@cleanup_scheduler.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


@tasks.loop(hours=24)
async def analytics_scheduler():
    try:
        if trades_col is None:
            return

        now_utc = datetime.utcnow()
        start = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=7)
        trades = await fetch_all_trades({"created_at": {"$gte": start, "$lt": now_utc}})

        if not trades:
            logger.info("analytics_scheduler: no recent trades.")
            return

        ch = get_channel(ANALYTICS_CHANNEL_ID)
        if ch is not None:
            await ch.send(embed=make_analytics_embed(trades, title="📈 7-Day Pattern Analytics"))

    except Exception:
        logger.exception("ERROR in analytics_scheduler")


@analytics_scheduler.before_loop
async def before_analytics():
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