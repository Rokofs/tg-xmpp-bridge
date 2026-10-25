import asyncio
import itertools
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from html import escape as _html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import slixmpp
from slixmpp import JID
from slixmpp.exceptions import IqError, IqTimeout
from slixmpp.plugins.xep_0363.http_upload import UploadServiceNotFound, FileTooBig, HTTPError

try:
    import discord
except ImportError:
    discord = None

from telegram import ReactionTypeEmoji, Update
from telegram.error import BadRequest
from telegram.ext import (
    AIORateLimiter,
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)
from telegram.request import HTTPXRequest


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_raw_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_CHAT_ID = int(_raw_chat_id) if _raw_chat_id else None

XMPP_JID = os.environ.get("XMPP_JID", "").strip() or None
XMPP_PASSWORD = os.environ.get("XMPP_PASSWORD", "").strip() or None
XMPP_PEER_JID = os.environ.get("XMPP_PEER_JID", "").strip() or None
XMPP_IS_MUC = os.environ.get("XMPP_IS_MUC", "false").strip().lower() == "true"
XMPP_MUC_NICK = os.environ.get("XMPP_MUC_NICK", "TelegramBridge")
XMPP_MUC_PASSWORD = os.environ.get("XMPP_MUC_PASSWORD", "").strip() or None
XMPP_ENABLED = bool(XMPP_JID and XMPP_PASSWORD and XMPP_PEER_JID)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip() or None
_raw_discord_channel = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
DISCORD_CHANNEL_ID = int(_raw_discord_channel) if _raw_discord_channel else None
DISCORD_ENABLED = bool(DISCORD_BOT_TOKEN)

BRIDGE_ENABLED = bool(TELEGRAM_CHAT_ID and (XMPP_ENABLED or DISCORD_ENABLED))

TELEGRAM_FORCE_IPV4 = os.environ.get("TELEGRAM_FORCE_IPV4", "false").strip().lower() == "true"

TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
TELEGRAM_USERBOT_SESSION = os.environ.get("TELEGRAM_USERBOT_SESSION", "").strip()
TELEGRAM_USERBOT_ENABLED = bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_USERBOT_SESSION)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None
DB_ENABLED = bool(DATABASE_URL)

EDIT_WINDOW_SECONDS = int(os.environ.get("EDIT_WINDOW_SECONDS", "300"))

MESSAGE_MAP_TTL_SECONDS = int(os.environ.get("MESSAGE_MAP_TTL_SECONDS", str(180 * 24 * 3600)))

RECENT_XMPP_TEXTS_LIMIT = int(os.environ.get("RECENT_XMPP_TEXTS_LIMIT", "2000"))

TELEGRAM_BOT_API_FILE_LIMIT_MB = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("tg_xmpp_bridge")
logging.getLogger("slixmpp").setLevel(logging.WARNING)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TMP_DIR = Path(tempfile.gettempdir()) / "tg_xmpp_bridge"
TMP_DIR.mkdir(parents=True, exist_ok=True)


_bridge_records: dict = {}
_tg_lookup: dict = {}
_xmpp_lookup: dict = {}
_discord_lookup: dict = {}


_COLOR_EMOJIS = [
    "🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "⚫️", "⚪️", "🟤",
    "🔺", "🔻", "🔸", "🔹", "🔶", "🔷", "🔳", "🔲",
    "🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "⬛️", "⬜️", "🟫",
]
_author_colors: dict = {}
_color_counter = itertools.count(0)


def _colored_author(key: str, display_name: str) -> str:
    if key not in _author_colors:
        idx = next(_color_counter) % len(_COLOR_EMOJIS)
        _author_colors[key] = _COLOR_EMOJIS[idx]
    return f"{_author_colors[key]} {display_name}"


def _own_xmpp_reply_jid() -> str:
    if XMPP_IS_MUC:
        return f"{XMPP_PEER_JID}/{XMPP_MUC_NICK}"
    return XMPP_PEER_JID


_db_pool = None


async def _init_db() -> None:
    global _db_pool
    if not DB_ENABLED:
        return
    try:
        import asyncpg
    except ImportError:
        logger.error(
            "DATABASE_URL is set but asyncpg is not installed (pip install asyncpg) — "
            "message links will be kept in memory only"
        )
        return
    try:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
        async with _db_pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bridge_records (
                    key TEXT PRIMARY KEY,
                    tg BIGINT,
                    xmpp TEXT,
                    discord BIGINT,
                    xmpp_to TEXT,
                    ts DOUBLE PRECISION NOT NULL
                )
                """
            )
            await conn.execute("ALTER TABLE bridge_records ADD COLUMN IF NOT EXISTS tg_human BOOLEAN")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recent_xmpp_texts (
                    id SERIAL PRIMARY KEY,
                    key TEXT NOT NULL,
                    body TEXT NOT NULL,
                    ts DOUBLE PRECISION NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
        logger.info("Database connected — message links will now survive bot restarts")
    except Exception:
        logger.exception("Could not connect to the database — running in memory only")
        _db_pool = None


def _db_save_setting(key: str, value: str) -> None:
    if _db_pool is None:
        return
    asyncio.create_task(_db_save_setting_impl(key, value))


async def _db_save_setting_impl(key: str, value: str) -> None:
    try:
        async with _db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bot_settings (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                key, value,
            )
    except Exception:
        logger.exception("Could not save setting %s to the database", key)


async def _db_load_setting(key: str) -> Optional[str]:
    if _db_pool is None:
        return None
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM bot_settings WHERE key = $1", key)
            return row["value"] if row else None
    except Exception:
        logger.exception("Could not load setting %s from the database", key)
        return None


async def _load_from_db() -> None:
    if _db_pool is None:
        return
    try:
        now = time.time()
        async with _db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, tg, xmpp, discord, xmpp_to, ts, tg_human FROM bridge_records")
            loaded = 0
            for row in rows:
                if now - row["ts"] > MESSAGE_MAP_TTL_SECONDS:
                    continue
                record = {
                    "tg": row["tg"], "xmpp": row["xmpp"], "discord": row["discord"],
                    "xmpp_to": row["xmpp_to"], "ts": row["ts"],
                    "tg_human": True if row["tg_human"] is None else row["tg_human"],
                }
                _bridge_records[row["key"]] = record
                _link_lookup(row["key"], record)
                loaded += 1

            text_rows = await conn.fetch(
                "SELECT key, body, ts FROM recent_xmpp_texts ORDER BY ts DESC LIMIT $1",
                RECENT_XMPP_TEXTS_LIMIT,
            )
            for row in reversed(text_rows):
                _recent_xmpp_texts.append((row["key"], row["body"], row["ts"]))
        logger.info(
            "Loaded from the database: %s message links, %s recent texts for quotes",
            loaded, len(text_rows),
        )
    except Exception:
        logger.exception("Could not load message links from the database")


def _db_save_record(key: str, record: dict) -> None:
    if _db_pool is None:
        return
    asyncio.create_task(_db_save_record_impl(key, record))


async def _db_save_record_impl(key: str, record: dict) -> None:
    try:
        async with _db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bridge_records (key, tg, xmpp, discord, xmpp_to, ts, tg_human)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (key) DO UPDATE SET
                    tg = EXCLUDED.tg, xmpp = EXCLUDED.xmpp, discord = EXCLUDED.discord,
                    xmpp_to = EXCLUDED.xmpp_to, ts = EXCLUDED.ts, tg_human = EXCLUDED.tg_human
                """,
                key, record.get("tg"), record.get("xmpp"), record.get("discord"),
                record.get("xmpp_to"), record["ts"], record.get("tg_human"),
            )
    except Exception:
        logger.exception("Could not save the message link to the database")


def _db_delete_record(key: str) -> None:
    if _db_pool is None:
        return
    asyncio.create_task(_db_delete_record_impl(key))


async def _db_delete_record_impl(key: str) -> None:
    try:
        async with _db_pool.acquire() as conn:
            await conn.execute("DELETE FROM bridge_records WHERE key = $1", key)
    except Exception:
        logger.exception("Could not delete the message link from the database")


def _db_save_recent_text(key: str, text: str, ts: float) -> None:
    if _db_pool is None:
        return
    asyncio.create_task(_db_save_recent_text_impl(key, text, ts))


async def _db_save_recent_text_impl(key: str, text: str, ts: float) -> None:
    try:
        async with _db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO recent_xmpp_texts (key, body, ts) VALUES ($1, $2, $3)",
                key, text, ts,
            )
            await conn.execute(
                """
                DELETE FROM recent_xmpp_texts WHERE id NOT IN (
                    SELECT id FROM recent_xmpp_texts ORDER BY ts DESC LIMIT $1
                )
                """,
                RECENT_XMPP_TEXTS_LIMIT,
            )
    except Exception:
        logger.exception("Could not save quote text to the database")


def _new_bridge_record(**ids) -> str:
    key = uuid.uuid4().hex
    record = {"tg": None, "xmpp": None, "discord": None, "xmpp_to": None, "ts": time.time(),
              "tg_human": ids.get("tg") is not None}
    record.update({k: v for k, v in ids.items() if v is not None})
    _bridge_records[key] = record
    _link_lookup(key, record)
    _db_save_record(key, record)
    return key


def _link_record(key: str, **ids) -> None:
    record = _bridge_records.get(key)
    if not record:
        return
    old_xmpp = record.get("xmpp")
    for k, v in ids.items():
        if v is not None:
            record[k] = v
    if old_xmpp is not None and record.get("xmpp") != old_xmpp:
        _xmpp_lookup.pop(old_xmpp, None)
    _link_lookup(key, record)
    _db_save_record(key, record)


def _link_lookup(key: str, record: dict) -> None:
    if record.get("tg") is not None:
        _tg_lookup[record["tg"]] = key
    if record.get("xmpp") is not None:
        _xmpp_lookup[record["xmpp"]] = key
    if record.get("discord") is not None:
        _discord_lookup[record["discord"]] = key


def _prune_bridge_records() -> None:
    now = time.time()
    stale = [k for k, v in _bridge_records.items() if now - v["ts"] > MESSAGE_MAP_TTL_SECONDS]
    for k in stale:
        record = _bridge_records.pop(k, None)
        if not record:
            continue
        if record.get("tg") is not None:
            _tg_lookup.pop(record["tg"], None)
            _non_editable_tg_messages.discard(record["tg"])
            _last_forward_check.pop(record["tg"], None)
        if record.get("xmpp") is not None:
            _xmpp_lookup.pop(record["xmpp"], None)
        if record.get("discord") is not None:
            _discord_lookup.pop(record["discord"], None)
        _db_delete_record(k)


def _forget_record_by_key(key: Optional[str]) -> None:
    if not key:
        return
    record = _bridge_records.pop(key, None)
    if not record:
        return
    if record.get("tg") is not None:
        _tg_lookup.pop(record["tg"], None)
        _non_editable_tg_messages.discard(record["tg"])
        _last_forward_check.pop(record["tg"], None)
    _db_delete_record(key)
    if record.get("xmpp") is not None:
        _xmpp_lookup.pop(record["xmpp"], None)
    if record.get("discord") is not None:
        _discord_lookup.pop(record["discord"], None)


async def _mirror_reactions(key: str, emojis: set, skip: str) -> None:
    record = _bridge_records.get(key)
    if not record:
        return

    if skip != "xmpp" and xmpp_bridge and record.get("xmpp"):
        try:
            await xmpp_bridge.send_reaction(record["xmpp"], emojis)
        except Exception:
            logger.exception("Could not forward the reaction to XMPP")

    if skip != "discord" and discord_bridge and record.get("discord"):
        try:
            await discord_bridge.set_reactions(record["discord"], emojis)
        except Exception:
            logger.exception("Could not forward the reaction to Discord")

    if skip != "tg" and telegram_app and record.get("tg"):
        try:
            await telegram_app.bot.set_message_reaction(
                chat_id=TELEGRAM_CHAT_ID,
                message_id=record["tg"],
                reaction=[ReactionTypeEmoji(e) for e in emojis] if emojis else [],
            )
        except Exception:
            logger.exception("Could not forward the reaction to Telegram")


async def _propagate_deletion(record: dict, skip: str) -> None:
    if skip != "tg" and record.get("tg") and record.get("tg_human", True):
        logger.warning(
            "Deletion from %s targets a user's original Telegram message (id=%s) — not deleting",
            skip, record["tg"],
        )
    elif skip != "tg" and telegram_app and record.get("tg"):
        try:
            await telegram_app.bot.delete_message(chat_id=TELEGRAM_CHAT_ID, message_id=record["tg"])
        except BadRequest as e:
            if "message to delete not found" in str(e).lower():
                pass
            else:
                logger.exception("Could not delete the message in Telegram")
        except Exception:
            logger.exception("Could not delete the message in Telegram")

    if skip != "xmpp" and xmpp_bridge and record.get("xmpp"):
        try:
            await xmpp_bridge.send_retraction_reliable(record["xmpp"])
        except Exception:
            logger.exception("Could not send the deletion to XMPP")

    if skip != "discord" and discord_bridge and record.get("discord"):
        try:
            await discord_bridge.delete_message(record["discord"])
        except Exception:
            logger.exception("Could not delete the message in Discord")


def _get_reply_targets(origin: str, replied_native_id) -> dict:
    if replied_native_id is None:
        return {}
    _prune_bridge_records()
    lookup = {"tg": _tg_lookup, "xmpp": _xmpp_lookup, "discord": _discord_lookup}[origin]
    key = lookup.get(replied_native_id)
    if not key:
        return {}
    record = _bridge_records.get(key, {})
    return {k: v for k, v in record.items() if k in ("tg", "xmpp", "discord", "xmpp_to") and v is not None}


def _get_reply_key(origin: str, replied_native_id) -> Optional[str]:
    if replied_native_id is None:
        return None
    _prune_bridge_records()
    lookup = {"tg": _tg_lookup, "xmpp": _xmpp_lookup, "discord": _discord_lookup}[origin]
    return lookup.get(replied_native_id)


def _get_xmpp_quote_preview(origin: str, replied_native_id) -> Optional[str]:
    key = _get_reply_key(origin, replied_native_id)
    if not key:
        return None
    _rec = _bridge_records.get(key)
    if _rec and _rec.get("xmpp"):
        return None
    for k, text, _ts in reversed(_recent_xmpp_texts):
        if k == key:
            if text.strip().startswith(">"):
                _quoted, remaining = _parse_manual_quote(text)
                if remaining:
                    text = remaining
            snippet = text.strip().replace("\n", " ")
            if len(snippet) > 150:
                snippet = snippet[:150].rstrip() + "…"
            return snippet
    return None


_recent_xmpp_texts: list = []


def _remember_xmpp_text(key: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    ts = time.time()
    _recent_xmpp_texts.append((key, text, ts))
    if len(_recent_xmpp_texts) > RECENT_XMPP_TEXTS_LIMIT:
        del _recent_xmpp_texts[: len(_recent_xmpp_texts) - RECENT_XMPP_TEXTS_LIMIT]
    _db_save_recent_text(key, text, ts)


def _find_key_by_quoted_text(quoted: str) -> Optional[str]:
    quoted = (quoted or "").strip()
    if not quoted:
        return None
    quoted_normalized = " ".join(quoted.split())
    now = time.time()
    for key, text, ts in reversed(_recent_xmpp_texts):
        if now - ts > MESSAGE_MAP_TTL_SECONDS:
            continue
        if key not in _bridge_records:
            continue
        text_normalized = " ".join(text.split())
        if quoted_normalized in text_normalized or text_normalized in quoted_normalized:
            return key
    return None


_MOBILE_QUOTE_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\(GMT[+-]?\d*\)$")


def _parse_manual_quote(body: str):
    quoted_lines: list = []
    other_lines: list = []

    for line in body.split("\n"):
        if line[:1] == ">" and line[1:2] == " " and line[2:3] != ">":
            content = line[2:].strip()
            if _MOBILE_QUOTE_TIMESTAMP_RE.match(content):
                if quoted_lines:
                    quoted_lines.pop()
            else:
                quoted_lines.append(content)
        else:
            other_lines.append(line)

    if not quoted_lines:
        return None, body

    quoted_text = " ".join(line for line in quoted_lines if line).strip()
    remaining = "\n".join(other_lines).strip()
    return quoted_text, remaining


def _strip_manual_quote_for_telegram(text: str) -> str:
    if not text:
        return text
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        if re.fullmatch(r"https?://\S+", line.strip()):
            continue
        lines.append(line)
    remaining = "\n".join(lines).strip()
    return remaining if remaining else text


_EMOJI_ONLY_RE = re.compile(
    r"^[\s\u2600-\u27BF\U0001F000-\U0001FFFF\u2190-\u21FF\u2300-\u23FF\uFE0F\u200D]+$"
)


def _is_emoji_only(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    return bool(_EMOJI_ONLY_RE.match(text))


def _extract_emojis(text: str) -> set:
    try:
        import emoji as emoji_lib
        found = {m["emoji"] for m in emoji_lib.emoji_list(text)}
        if found:
            return found
    except ImportError:
        pass
    return {text.strip()}


telegram_app: Optional[Application] = None
xmpp_bridge = None
discord_bridge = None


_DISCORD_MARKERS = {
    "b": "**", "strong": "**",
    "i": "*", "em": "*",
    "u": "__", "ins": "__",
    "s": "~~", "strike": "~~", "del": "~~",
}
_XMPP_MARKERS = {
    "b": "*", "strong": "*",
    "i": "_", "em": "_",
    "s": "~", "strike": "~", "del": "~",
}


def _format_from_telegram_html(html_text: str, target: str) -> str:
    markers = _DISCORD_MARKERS if target == "discord" else _XMPP_MARKERS

    class _Conv(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out = []
            self.tag_stack = []

        def handle_starttag(self, tag, attrs):
            attrs_dict = dict(attrs)
            if tag == "span" and attrs_dict.get("class") == "tg-spoiler":
                marker = "||" if target == "discord" else ""
                self.tag_stack.append(("span", marker))
                self.out.append(marker)
            elif tag == "code":
                self.tag_stack.append(("code", "`"))
                self.out.append("`")
            elif tag == "pre":
                self.tag_stack.append(("pre", "```"))
                self.out.append("```")
            elif tag == "a":
                href = attrs_dict.get("href", "")
                is_internal = href.startswith("tg://")
                self.tag_stack.append(("a", (href, is_internal)))
                if target == "discord" and not is_internal:
                    self.out.append("[")
            elif tag == "blockquote":
                self.tag_stack.append(("blockquote", ""))
                self.out.append("\n> ")
            elif tag in markers:
                marker = markers[tag]
                self.tag_stack.append((tag, marker))
                self.out.append(marker)
            else:
                self.tag_stack.append((tag, ""))

        def handle_endtag(self, tag):
            if not self.tag_stack:
                return
            _open_tag, marker = self.tag_stack.pop()
            if _open_tag == "a":
                href, is_internal = marker
                if is_internal:
                    pass
                elif target == "discord":
                    self.out.append(f"]({href})")
                elif href:
                    self.out.append(f" ({href})")
            elif _open_tag == "blockquote":
                pass
            else:
                self.out.append(marker)

        def handle_data(self, data):
            self.out.append(data)

    conv = _Conv()
    try:
        conv.feed(html_text)
        conv.close()
    except Exception:
        logger.exception("Could not parse Telegram formatting, sending as plain text")
        return html_text
    return "".join(conv.out)


def _format_tg_text(plain_text: Optional[str], html_text: Optional[str], target: str) -> str:
    if not html_text:
        return plain_text or ""
    return _format_from_telegram_html(html_text, target)


_MD_CODEBLOCK_RE = re.compile(r"```.*?```", re.S)
_MD_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _convert_markup(text: str, source: str, target: str) -> str:
    if not text or source == target:
        return _html_escape(text, quote=False) if target == "telegram" else text

    if target == "telegram":
        text = _html_escape(text, quote=False)

    code_stash = []

    def _stash_code(m):
        code_stash.append(m.group(0))
        return f"\x00{len(code_stash) - 1}\x00"

    text = _MD_CODEBLOCK_RE.sub(_stash_code, text)
    text = _MD_INLINE_CODE_RE.sub(_stash_code, text)

    link_stash = []

    def _stash_link(m):
        link_stash.append((m.group(1), m.group(2)))
        return f"\x01{len(link_stash) - 1}\x01"

    text = _MD_LINK_RE.sub(_stash_link, text)

    style_stash = []

    def _make_style_stasher(kind):
        def _sub(m):
            style_stash.append((kind, m.group(1)))
            return f"\x02{len(style_stash) - 1}\x02"
        return _sub

    if source == "discord":
        text = re.sub(r"\*\*(.+?)\*\*", _make_style_stasher("bold"), text, flags=re.S)
        text = re.sub(r"__(.+?)__", _make_style_stasher("underline"), text, flags=re.S)
        text = re.sub(r"~~(.+?)~~", _make_style_stasher("strike"), text, flags=re.S)
        text = re.sub(r"\|\|(.+?)\|\|", _make_style_stasher("spoiler"), text, flags=re.S)
        text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", _make_style_stasher("italic"), text)
    else:
        text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", _make_style_stasher("bold"), text)
        text = re.sub(r"(?<!_)_([^_\n]+?)_(?!_)", _make_style_stasher("italic"), text)
        text = re.sub(r"(?<!~)~([^~\n]+?)~(?!~)", _make_style_stasher("strike"), text)

    _TARGET_SYNTAX = {
        "telegram": {
            "bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"),
            "underline": ("<u>", "</u>"), "strike": ("<s>", "</s>"),
            "spoiler": ('<span class="tg-spoiler">', "</span>"),
        },
        "discord": {
            "bold": ("**", "**"), "italic": ("*", "*"),
            "underline": ("__", "__"), "strike": ("~~", "~~"),
            "spoiler": ("||", "||"),
        },
        "xmpp": {
            "bold": ("*", "*"), "italic": ("_", "_"), "strike": ("~", "~"),
        },
    }
    mapping = _TARGET_SYNTAX[target]
    for i, (kind, inner) in enumerate(style_stash):
        placeholder = f"\x02{i}\x02"
        if kind in mapping:
            open_m, close_m = mapping[kind]
            replacement = f"{open_m}{inner}{close_m}"
        else:
            replacement = inner
        text = text.replace(placeholder, replacement)

    for i, (label, url) in enumerate(link_stash):
        placeholder = f"\x01{i}\x01"
        if target == "telegram":
            replacement = f'<a href="{_html_escape(url, quote=True)}">{label}</a>'
        elif target == "discord":
            replacement = f"[{label}]({url})"
        else:
            replacement = url if label == url else f"{label} ({url})"
        text = text.replace(placeholder, replacement)

    for i, original in enumerate(code_stash):
        placeholder = f"\x00{i}\x00"
        if target == "telegram":
            if original.startswith("```"):
                inner = original.strip("`")
                replacement = f"<pre>{inner}</pre>"
            else:
                inner = original.strip("`")
                replacement = f"<code>{inner}</code>"
        else:
            replacement = original
        text = text.replace(placeholder, replacement)

    return text


def _convert_static_sticker_to_png(path: Path) -> Path:
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow is not installed — the static sticker will be sent as .webp")
        return path
    try:
        png_path = path.with_suffix(".png")
        with Image.open(path) as im:
            im.convert("RGBA").save(png_path, "PNG")
        path.unlink(missing_ok=True)
        return png_path
    except Exception:
        logger.exception("Could not convert the sticker to PNG, sending as is")
        return path


async def _download_discord_attachment(attachment) -> Path:
    import aiohttp

    suffix = Path(attachment.filename).suffix or ""
    local_path = TMP_DIR / f"discord_{attachment.id}{suffix}"
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            resp.raise_for_status()
            data = await resp.read()
    local_path.write_bytes(data)
    return local_path


async def _download_url_to_file(url: str) -> Path:
    import hashlib
    import mimetypes
    import aiohttp
    from urllib.parse import urlparse

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()

    suffix = Path(urlparse(url).path).suffix
    if not suffix and content_type:
        suffix = mimetypes.guess_extension(content_type) or ""

    name_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    local_path = TMP_DIR / f"xmpp_{name_hash}{suffix}"
    local_path.write_bytes(data)
    return local_path


class XMPPBridge(slixmpp.ClientXMPP):

    def __init__(self, jid: str, password: str):
        super().__init__(jid, password)

        self.add_event_handler("session_start", self.on_session_start)
        self.add_event_handler("message", self.on_xmpp_message)
        self.add_event_handler("reactions", self.on_xmpp_reactions)
        self.add_event_handler("message_retract", self.on_xmpp_retraction)
        self.add_event_handler("disconnected", self.on_xmpp_disconnected)

        self._pending_echo_futures: dict = {}

        self.register_plugin("xep_0030")
        self.register_plugin("xep_0115")
        self.register_plugin("xep_0128")
        self.register_plugin("xep_0199")
        self.register_plugin("xep_0066")
        self.register_plugin("xep_0363")
        self.register_plugin("xep_0045")
        self.register_plugin("xep_0308")
        self.register_plugin("xep_0334")
        self.register_plugin("xep_0444")
        self.register_plugin("xep_0461")
        self.register_plugin("xep_0359")
        self.register_plugin("xep_0424")

    async def on_session_start(self, event):
        self.send_presence()
        await self.get_roster()

        if XMPP_IS_MUC:
            self.plugin["xep_0045"].join_muc(XMPP_PEER_JID, XMPP_MUC_NICK, password=XMPP_MUC_PASSWORD)
            logger.info("XMPP: joined room %s as %s", XMPP_PEER_JID, XMPP_MUC_NICK)

        logger.info("XMPP: session started, signed in as %s", self.boundjid.bare)

    async def on_xmpp_disconnected(self, event):
        logger.warning("XMPP: connection lost (%s) — reconnecting in 5 s", event)
        await asyncio.sleep(5)
        try:
            self.connect()
        except Exception:
            logger.exception("XMPP: reconnect failed")

    def _is_from_peer(self, msg) -> bool:
        if XMPP_IS_MUC:
            if str(msg["mucnick"]) == XMPP_MUC_NICK:
                return False
            return str(msg["from"].bare) == XMPP_PEER_JID
        return str(msg["from"].bare) == XMPP_PEER_JID

    def _learn_own_stanza_id(self, msg) -> None:
        if not XMPP_IS_MUC:
            return
        client_id = msg["id"] or None
        if not client_id:
            return

        future = self._pending_echo_futures.pop(client_id, None)

        stanza_id_el = msg["stanza_id"]
        real_id = None
        if stanza_id_el and str(stanza_id_el["by"]) == XMPP_PEER_JID:
            real_id = stanza_id_el["id"] or None

        resolved_id = real_id if (real_id and real_id != client_id) else client_id

        if real_id and real_id != client_id:
            key = _xmpp_lookup.get(client_id)
            if key:
                _link_record(key, xmpp=real_id)

        if future is not None and not future.done():
            future.set_result(resolved_id)

    async def _send_and_confirm(self, stanza) -> str:
        client_id = stanza["id"]
        future = None
        if XMPP_IS_MUC:
            future = asyncio.get_event_loop().create_future()
            self._pending_echo_futures[client_id] = future
        stanza.send()
        if future is None:
            return client_id
        try:
            resolved = await asyncio.wait_for(future, timeout=8)
            return resolved
        except asyncio.TimeoutError:
            self._pending_echo_futures.pop(client_id, None)
            logger.warning(
                "No MUC echo received for message %s within 8 s — "
                "using the client id as is (replies to it may not work)",
                client_id,
            )
            return client_id

    async def on_xmpp_message(self, msg):
        if msg["type"] not in ("chat", "normal", "groupchat"):
            return
        if XMPP_IS_MUC and str(msg["mucnick"]) == XMPP_MUC_NICK:
            self._learn_own_stanza_id(msg)
            return
        if not self._is_from_peer(msg):
            return

        if msg["retract"]["id"]:
            return

        if msg["reactions"]["id"]:
            return

        body = (msg["body"] or "").strip()
        oob_url = msg["oob"]["url"] or None
        correction_id = msg["replace"]["id"] or None

        if not body and not oob_url:
            return

        sender = str(msg["mucnick"]) if XMPP_IS_MUC else str(msg["from"].bare)
        display_sender = _colored_author(f"xmpp:{sender}", sender)

        if correction_id:
            _prune_bridge_records()
            key = _xmpp_lookup.get(correction_id)
            record = _bridge_records.get(key) if key else None
            if not record or time.time() - record["ts"] > EDIT_WINDOW_SECONDS:
                logger.info(
                    "Correction of XMPP message %s: original not found or the "
                    "edit window expired — ignoring",
                    correction_id,
                )
                return

            text_tg = (
                f"{_html_escape(display_sender, quote=False)}: "
                f"{_convert_markup(_strip_manual_quote_for_telegram(body), 'xmpp', 'telegram')} (edited)"
            )
            text_discord = f"{display_sender}: {_convert_markup(body, 'xmpp', 'discord')}"

            if telegram_app and record.get("tg"):
                try:
                    await telegram_app.bot.edit_message_text(
                        chat_id=TELEGRAM_CHAT_ID, message_id=record["tg"], text=text_tg, parse_mode="HTML",
                    )
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.exception("Could not apply the correction in Telegram")
                except Exception:
                    logger.exception("Could not apply the correction in Telegram")
            if discord_bridge and record.get("discord"):
                try:
                    await discord_bridge.send_correction(record["discord"], text_discord)
                except Exception:
                    logger.exception("Could not apply the correction in Discord")
            return

        reply_id = msg["reply"]["id"] or None
        effective_body = body
        quote_matched_key = None

        if ">" in body:
            quoted_text, remaining_text = _parse_manual_quote(body)
            if quoted_text:
                if not reply_id:
                    quote_matched_key = _find_key_by_quoted_text(quoted_text)
                effective_body = remaining_text

        if (reply_id or quote_matched_key) and effective_body and not oob_url and _is_emoji_only(effective_body):
            reaction_key = _get_reply_key("xmpp", reply_id) if reply_id else quote_matched_key
            if reaction_key:
                await _mirror_reactions(reaction_key, _extract_emojis(effective_body), skip="xmpp")
                return

        if reply_id:
            reply_targets = _get_reply_targets("xmpp", reply_id)
        elif quote_matched_key:
            _record = _bridge_records.get(quote_matched_key, {})
            reply_targets = {
                k: v for k, v in _record.items() if k in ("tg", "xmpp", "discord", "xmpp_to") and v is not None
            }
        else:
            reply_targets = {}

        local_path = None
        if oob_url:
            try:
                local_path = await _download_url_to_file(oob_url)
            except Exception:
                logger.warning(
                    "Could not download the file from the OOB link (%s) — will send it as a plain link", oob_url
                )

        key = _new_bridge_record()
        client_id = msg["id"] or None
        canonical_id = client_id
        if XMPP_IS_MUC:
            sid_el = msg["stanza_id"]
            if sid_el and str(sid_el["by"]) == XMPP_PEER_JID and sid_el["id"]:
                canonical_id = sid_el["id"]
        if canonical_id:
            peer_reply_jid = f"{XMPP_PEER_JID}/{sender}" if XMPP_IS_MUC else XMPP_PEER_JID
            _link_record(key, xmpp=canonical_id, xmpp_to=peer_reply_jid)
        if client_id and client_id != canonical_id:
            _xmpp_lookup[client_id] = key
        _remember_xmpp_text(key, body)

        if local_path is not None:
            base = effective_body or ""
            if oob_url and base:
                base = base.replace(oob_url, "").strip()
            caption_tg = (
                f"{_html_escape(display_sender, quote=False)}: "
                f"{_convert_markup(_strip_manual_quote_for_telegram(base), 'xmpp', 'telegram')}"
                if base else _html_escape(display_sender, quote=False)
            )
            caption_discord = (
                f"{display_sender}: {_convert_markup(base, 'xmpp', 'discord')}" if base else display_sender
            )
            try:
                if telegram_app:
                    try:
                        sent = await _tg_send_file(
                            local_path, caption_tg, reply_to=reply_targets.get("tg"), parse_mode="HTML"
                        )
                        if sent is not None:
                            _link_record(key, tg=sent.message_id)
                    except Exception:
                        logger.exception("Could not forward media from XMPP to Telegram")
                if discord_bridge:
                    try:
                        discord_id = await discord_bridge.send_file(
                            local_path, caption=caption_discord, reply_to=reply_targets.get("discord")
                        )
                        _link_record(key, discord=discord_id)
                    except Exception:
                        logger.exception("Could not forward media from XMPP to Discord")
            finally:
                local_path.unlink(missing_ok=True)
            return

        if oob_url and not effective_body:
            text_tg = f"{_html_escape(display_sender, quote=False)}: {_html_escape(oob_url, quote=False)}"
            text_discord = f"{display_sender}: {oob_url}"
        else:
            converted_tg = _convert_markup(_strip_manual_quote_for_telegram(effective_body), "xmpp", "telegram")
            converted_discord = _convert_markup(effective_body, "xmpp", "discord")
            if oob_url and effective_body != oob_url:
                text_tg = (
                    f"{_html_escape(display_sender, quote=False)}: {converted_tg}\n"
                    f"{_html_escape(oob_url, quote=False)}"
                )
                text_discord = f"{display_sender}: {converted_discord}\n{oob_url}"
            else:
                text_tg = f"{_html_escape(display_sender, quote=False)}: {converted_tg}"
                text_discord = f"{display_sender}: {converted_discord}"

        if telegram_app:
            try:
                sent = await telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=text_tg, parse_mode="HTML",
                    reply_to_message_id=reply_targets.get("tg"),
                )
                _link_record(key, tg=sent.message_id)
            except Exception:
                logger.exception("Could not forward the message from XMPP to Telegram")

        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_text(text_discord, reply_to=reply_targets.get("discord"))
                _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Could not forward the message from XMPP to Discord")

    async def on_xmpp_reactions(self, msg):
        if not self._is_from_peer(msg):
            return
        target_id = msg["reactions"]["id"] or None
        if not target_id:
            return
        emojis = msg["reactions"].get_values()

        _prune_bridge_records()
        key = _xmpp_lookup.get(target_id)
        if not key:
            return
        await _mirror_reactions(key, emojis, skip="xmpp")

    async def send_text(
        self, text: str, reply_to: Optional[str] = None, reply_to_jid: Optional[str] = None
    ) -> str:
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=text, mtype=mtype)
        if reply_to:
            stanza["reply"]["id"] = reply_to
            stanza["reply"]["to"] = reply_to_jid or XMPP_PEER_JID
        return await self._send_and_confirm(stanza)

    async def send_correction(self, new_text: str, original_stanza_id: str) -> Optional[str]:
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        try:
            await self.send_retraction(original_stanza_id, attempts=2, delay=1.0)
        except Exception:
            logger.exception("Could not send a retraction before the edit in XMPP")

        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=new_text, mtype=mtype)
        stanza.send()
        return stanza["id"]

    async def send_reaction(self, target_stanza_id: str, emojis: set) -> None:
        from xml.etree import ElementTree as ET

        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        stanza = self.make_message(mto=XMPP_PEER_JID, mtype=mtype)

        reactions_el = ET.Element("{urn:xmpp:reactions:0}reactions")
        reactions_el.set("id", target_stanza_id)
        for emoji in emojis:
            reaction_el = ET.SubElement(reactions_el, "{urn:xmpp:reactions:0}reaction")
            reaction_el.text = emoji
        stanza.append(reactions_el)

        store_el = ET.Element("{urn:xmpp:hints}store")
        stanza.append(store_el)


        stanza.send()

    async def send_retraction_reliable(self, target_stanza_id: str) -> None:
        try:
            await self.send_retraction(target_stanza_id)
        except Exception:
            logger.exception("Could not send a retraction for the deletion in XMPP")
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        stanza = self.make_message(
            mto=XMPP_PEER_JID,
            mbody="/me retracted a previous message, but it's unsupported by your client.",
            mtype=mtype,
        )
        stanza["replace"]["id"] = target_stanza_id
        stanza.send()

    async def send_retraction(self, target_stanza_id: str, attempts: int = 3, delay: float = 2.0) -> None:
        from xml.etree import ElementTree as ET

        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        fallback_text = "/me retracted a previous message, but it's unsupported by your client."

        for i in range(attempts):
            stanza = self.make_message(mto=XMPP_PEER_JID, mtype=mtype)

            retract_el = ET.Element("{urn:xmpp:message-retract:1}retract")
            retract_el.set("id", target_stanza_id)
            stanza.append(retract_el)

            fallback_el = ET.Element("{urn:xmpp:fallback:0}fallback")
            fallback_el.set("for", "urn:xmpp:message-retract:1")
            stanza.append(fallback_el)

            stanza["body"] = fallback_text

            store_el = ET.Element("{urn:xmpp:hints}store")
            stanza.append(store_el)

            stanza.send()
            if i < attempts - 1:
                await asyncio.sleep(delay)

    async def on_xmpp_retraction(self, msg):
        if not self._is_from_peer(msg):
            return
        retract_id = msg["retract"]["id"] or None
        if not retract_id:
            return
        _prune_bridge_records()
        key = _xmpp_lookup.get(retract_id)
        if not key:
            return
        record = _bridge_records.get(key)
        if not record:
            return
        await _propagate_deletion(record, skip="xmpp")
        _forget_record_by_key(key)

    async def _manual_upload_with_browser_headers(self, filepath: Path) -> str:
        import aiohttp
        import mimetypes

        size = filepath.stat().st_size
        content_type = mimetypes.guess_type(str(filepath))[0] or "application/octet-stream"
        basename = filepath.name

        upload_plugin = self["xep_0363"]
        domain = getattr(upload_plugin, "upload_service", None)
        if not domain:
            found = await upload_plugin.find_upload_service()
            domain = found["from"] if found is not None else None
        if not domain:
            raise RuntimeError("could not determine the HTTP Upload service")

        slot_iq = await upload_plugin.request_slot(domain, basename, size, content_type)
        slot = slot_iq["http_upload_slot"]
        put_url = slot["put"]["url"]
        get_url = slot["get"]["url"]

        headers = {}
        try:
            for h in slot["put"]["headers"]:
                headers[h["name"]] = h["value"]
        except Exception:
            pass
        headers.setdefault("Content-Type", content_type)
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        with open(filepath, "rb") as f:
            data = f.read()

        async with aiohttp.ClientSession() as session:
            async with session.put(put_url, data=data, headers=headers) as resp:
                if resp.status not in (200, 201, 204):
                    body = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")

        return get_url

    async def send_file(
        self, filepath: Path, caption: str = "", reply_to: Optional[str] = None,
        reply_to_jid: Optional[str] = None,
    ) -> Optional[str]:

        async def _fallback_link_or_error(error_note: str) -> Optional[str]:
            return await self.send_text(f"{caption} {error_note}".strip())

        try:
            url = await self["xep_0363"].upload_file(str(filepath))
        except UploadServiceNotFound:
            logger.warning("No HTTP Upload service found on the XMPP server")
            return await _fallback_link_or_error("(the server does not support file uploads)")
        except FileTooBig as e:
            logger.warning("File is too large to upload: %s", e)
            return await _fallback_link_or_error("(file is too large for the XMPP server)")
        except HTTPError as e:
            logger.warning("The server rejected the upload (%s), retrying manually with browser headers", e)
            try:
                url = await self._manual_upload_with_browser_headers(filepath)
                logger.info("Workaround succeeded — file uploaded manually with browser headers")
            except Exception as manual_err:
                logger.warning("Manual upload failed too (%s) — the server rejects the file", manual_err)
                return await _fallback_link_or_error(f"(the server rejected the file upload: {e})")
        except IqError as e:
            logger.exception("File upload to the XMPP server failed")
            return await _fallback_link_or_error(f"(file upload error: {e})")
        except IqTimeout as e:
            logger.exception("Timeout while uploading the file to the XMPP server")
            return await _fallback_link_or_error(f"(file upload timeout: {e})")
        except OSError as e:
            logger.exception("Network error while uploading the file to the XMPP server")
            return await _fallback_link_or_error(f"(network error during file upload: {e})")

        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        body = f"{caption}\n{url}".strip() if caption else url
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=body, mtype=mtype)
        stanza["oob"]["url"] = url
        if reply_to:
            stanza["reply"]["id"] = reply_to
            stanza["reply"]["to"] = reply_to_jid or XMPP_PEER_JID
        return await self._send_and_confirm(stanza)


_DiscordBase = discord.Client if discord is not None else object


class DiscordBridge(_DiscordBase):

    def __init__(self):
        if discord is None:
            raise RuntimeError(
                "discord.py is not installed (pip install discord.py) but DISCORD_BOT_TOKEN is set"
            )
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.bridge_channel = None
        self._recent_message_ids: set = set()
        self._recent_message_ids_order: list = []

        self.tree = discord.app_commands.CommandTree(self)

        @self.tree.command(name="start", description="Make this channel the bridge channel")
        async def start_command(interaction: "discord.Interaction"):
            perms = interaction.user.guild_permissions if interaction.guild else None
            if not perms or not perms.administrator:
                await interaction.response.send_message(
                    "⛔ This command is available to server administrators only.", ephemeral=True
                )
                return
            global DISCORD_CHANNEL_ID
            DISCORD_CHANNEL_ID = interaction.channel_id
            self.bridge_channel = interaction.channel
            _db_save_setting("discord_channel_id", str(interaction.channel_id))
            await interaction.response.send_message(
                f"✅ The bridge now works in this channel (id: `{interaction.channel_id}`)."
            )
            logger.info(
                "Discord: bridge channel set with /start: #%s (%s)",
                getattr(interaction.channel, "name", interaction.channel_id), interaction.channel_id,
            )

    def _already_processed(self, message_id: int) -> bool:
        if message_id in self._recent_message_ids:
            return True
        self._recent_message_ids.add(message_id)
        self._recent_message_ids_order.append(message_id)
        if len(self._recent_message_ids_order) > 500:
            old = self._recent_message_ids_order.pop(0)
            self._recent_message_ids.discard(old)
        return False

    async def on_ready(self):
        try:
            await self.tree.sync()
        except Exception:
            logger.exception("Could not sync Discord slash commands")
        if DISCORD_CHANNEL_ID is None:
            logger.info(
                "Discord: connected as %s, no channel selected yet — "
                "run /start in the desired channel", self.user,
            )
            return
        channel = self.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(DISCORD_CHANNEL_ID)
            except Exception:
                logger.exception("Could not find the Discord channel with ID %s", DISCORD_CHANNEL_ID)
                return
        self.bridge_channel = channel
        logger.info(
            "Discord: connected as %s, channel: #%s",
            self.user, getattr(channel, "name", DISCORD_CHANNEL_ID),
        )

    async def on_message(self, message: "discord.Message"):
        if self.user and message.author.id == self.user.id:
            return

        if DISCORD_CHANNEL_ID is None or message.channel.id != DISCORD_CHANNEL_ID:
            return
        if self._already_processed(message.id):
            logger.info("Discord redelivered an already processed message %s — ignoring", message.id)
            return

        author = _colored_author(f"discord:{message.author.id}", message.author.display_name)

        replied_id = message.reference.message_id if message.reference else None

        if replied_id and message.content and not message.attachments and _is_emoji_only(message.content):
            reaction_key = _get_reply_key("discord", replied_id)
            if reaction_key:
                await _mirror_reactions(reaction_key, _extract_emojis(message.content), skip="discord")
                return

        reply_targets = _get_reply_targets("discord", replied_id) if replied_id else {}

        poll = getattr(message, "poll", None)
        if poll is not None:
            try:
                question = poll.question.text if hasattr(poll.question, "text") else str(poll.question)
                lines = [f"📊 Poll from {author}: {question}"]
                for i, answer in enumerate(poll.answers, start=1):
                    answer_text = answer.text if hasattr(answer, "text") else str(answer)
                    lines.append(f"{i}) {answer_text}")
                text = "\n".join(lines)
            except Exception:
                logger.exception("Could not parse the Discord poll, forwarding as is")
                text = f"{author}: [poll — could not get its content]"
            key = _new_bridge_record(discord=message.id)
            if xmpp_bridge:
                try:
                    xmpp_id = await xmpp_bridge.send_text(
                        text, reply_to=reply_targets.get("xmpp"), reply_to_jid=reply_targets.get("xmpp_to")
                    )
                    _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
                except Exception:
                    logger.exception("Could not forward the poll from Discord to XMPP")
            if telegram_app:
                try:
                    sent = await telegram_app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID, text=text,
                        reply_to_message_id=reply_targets.get("tg"),
                    )
                    _link_record(key, tg=sent.message_id)
                except Exception:
                    logger.exception("Could not forward the poll from Discord to Telegram")
            return

        has_attachments = bool(message.attachments)
        _quote = _get_xmpp_quote_preview("discord", replied_id) if replied_id else None
        for attachment in message.attachments:
            if message.content:
                caption_xmpp = f"{author}: {_convert_markup(message.content, 'discord', 'xmpp')}"
                caption_tg = (
                    f"{_html_escape(author, quote=False)}: "
                    f"{_convert_markup(_strip_manual_quote_for_telegram(message.content), 'discord', 'telegram')}"
                )
            else:
                caption_xmpp = author
                caption_tg = _html_escape(author, quote=False)
            if _quote:
                caption_xmpp = f"> {_quote}\n{caption_xmpp}"
            try:
                local_path = await _download_discord_attachment(attachment)
            except Exception:
                logger.exception("Could not download the Discord attachment")
                continue
            key = _new_bridge_record(discord=message.id)
            try:
                if xmpp_bridge:
                    try:
                        xmpp_id = await xmpp_bridge.send_file(
                            local_path, caption=caption_xmpp,
                            reply_to=reply_targets.get("xmpp"), reply_to_jid=reply_targets.get("xmpp_to"),
                        )
                        if xmpp_id:
                            _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
                    except Exception:
                        logger.exception("Could not forward the attachment from Discord to XMPP")
                if telegram_app:
                    try:
                        sent = await _tg_send_file(
                            local_path, caption_tg, reply_to=reply_targets.get("tg"), parse_mode="HTML"
                        )
                        if sent is not None:
                            _link_record(key, tg=sent.message_id)
                    except Exception:
                        logger.exception("Could not forward the attachment from Discord to Telegram")
            finally:
                local_path.unlink(missing_ok=True)

        if has_attachments or not message.content:
            return
        xmpp_text = f"{author}: {_convert_markup(message.content, 'discord', 'xmpp')}"
        if replied_id:
            _quote = _get_xmpp_quote_preview("discord", replied_id)
            if _quote:
                xmpp_text = f"> {_quote}\n{xmpp_text}"
        tg_text = (
            f"{_html_escape(author, quote=False)}: "
            f"{_convert_markup(_strip_manual_quote_for_telegram(message.content), 'discord', 'telegram')}"
        )
        key = _new_bridge_record(discord=message.id)

        if xmpp_bridge:
            try:
                xmpp_id = await xmpp_bridge.send_text(
                    xmpp_text, reply_to=reply_targets.get("xmpp"), reply_to_jid=reply_targets.get("xmpp_to")
                )
                _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
                _remember_xmpp_text(key, xmpp_text)
            except Exception:
                logger.exception("Could not forward the message from Discord to XMPP")

        if telegram_app:
            try:
                sent = await telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=tg_text, parse_mode="HTML",
                    reply_to_message_id=reply_targets.get("tg"),
                )
                _link_record(key, tg=sent.message_id)
            except Exception:
                logger.exception("Could not forward the message from Discord to Telegram")

    async def on_message_delete(self, message: "discord.Message"):
        if message.channel.id != DISCORD_CHANNEL_ID:
            return
        _prune_bridge_records()
        key = _discord_lookup.get(message.id)
        if not key:
            return
        record = _bridge_records.get(key)
        if not record:
            return
        await _propagate_deletion(record, skip="discord")
        _forget_record_by_key(key)

    async def on_message_edit(self, before, after):
        if self.user and after.author.id == self.user.id:
            return
        if after.channel.id != DISCORD_CHANNEL_ID:
            return

        _prune_bridge_records()
        key = _discord_lookup.get(after.id)
        record = _bridge_records.get(key) if key else None
        if not record or time.time() - record["ts"] > EDIT_WINDOW_SECONDS:
            return

        author = _colored_author(f"discord:{after.author.id}", after.author.display_name)
        xmpp_text = f"{author}: {_convert_markup(after.content, 'discord', 'xmpp')} (edited)"
        tg_text = (
            f"{_html_escape(author, quote=False)}: "
            f"{_convert_markup(_strip_manual_quote_for_telegram(after.content), 'discord', 'telegram')} (edited)"
        )

        if xmpp_bridge and record.get("xmpp"):
            try:
                new_xmpp_id = await xmpp_bridge.send_correction(xmpp_text, record["xmpp"])
                if new_xmpp_id:
                    _link_record(key, xmpp=new_xmpp_id)
            except Exception:
                logger.exception("Could not forward the correction to XMPP")
        if telegram_app and record.get("tg"):
            try:
                await telegram_app.bot.edit_message_text(
                    chat_id=TELEGRAM_CHAT_ID, message_id=record["tg"], text=tg_text, parse_mode="HTML",
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    logger.exception("Could not apply the correction in Telegram")
            except Exception:
                logger.exception("Could not apply the correction in Telegram")

    async def on_raw_reaction_add(self, payload):
        await self._handle_reaction_change(payload)

    async def on_raw_reaction_remove(self, payload):
        await self._handle_reaction_change(payload)

    async def _handle_reaction_change(self, payload):
        if self.user and payload.user_id == self.user.id:
            return
        if payload.channel_id != DISCORD_CHANNEL_ID:
            return

        _prune_bridge_records()
        key = _discord_lookup.get(payload.message_id)
        if not key:
            return

        try:
            channel = self.bridge_channel or self.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            logger.exception("Could not fetch the Discord message to recount reactions")
            return

        active = set()
        for r in message.reactions:
            human_count = r.count - (1 if r.me else 0)
            if human_count > 0:
                active.add(str(r.emoji))

        await _mirror_reactions(key, active, skip="discord")

    async def send_text(self, text: str, reply_to: Optional[int] = None) -> int:
        if self.bridge_channel is None:
            raise RuntimeError("The Discord channel is not ready yet")
        reference = (
            discord.MessageReference(
                message_id=reply_to, channel_id=DISCORD_CHANNEL_ID, fail_if_not_exists=False
            )
            if reply_to else None
        )
        sent = await self.bridge_channel.send(text, reference=reference)
        return sent.id

    async def send_correction(self, discord_message_id: int, new_text: str) -> None:
        if self.bridge_channel is None:
            return
        try:
            message = await self.bridge_channel.fetch_message(discord_message_id)
            await message.edit(content=new_text)
        except Exception:
            logger.exception("Could not apply the correction in Discord")

    async def delete_message(self, discord_message_id: int) -> None:
        if self.bridge_channel is None:
            return
        try:
            message = await self.bridge_channel.fetch_message(discord_message_id)
            await message.delete()
        except discord.errors.NotFound:
            pass
        except Exception:
            logger.exception("Could not delete the message in Discord")

    async def send_file(self, filepath: Path, caption: str = "", reply_to: Optional[int] = None) -> int:
        if self.bridge_channel is None:
            raise RuntimeError("The Discord channel is not ready yet")
        reference = (
            discord.MessageReference(
                message_id=reply_to, channel_id=DISCORD_CHANNEL_ID, fail_if_not_exists=False
            )
            if reply_to else None
        )
        sent = await self.bridge_channel.send(
            content=caption or None, file=discord.File(str(filepath)), reference=reference
        )
        return sent.id

    async def set_reactions(self, discord_message_id: int, emojis: set) -> None:
        if self.bridge_channel is None:
            return
        try:
            message = await self.bridge_channel.fetch_message(discord_message_id)
        except Exception:
            logger.exception("Could not fetch the Discord message to set reactions")
            return

        current = {str(r.emoji) for r in message.reactions if r.me}
        for e in current - emojis:
            try:
                await message.remove_reaction(e, self.user)
            except Exception:
                logger.exception("Could not remove reaction %s in Discord", e)
        for e in emojis - current:
            try:
                await message.add_reaction(e)
            except Exception:
                logger.exception("Could not add reaction %s in Discord", e)


_MEDIA_ATTRS = ("photo", "video", "voice", "audio", "document", "sticker", "video_note", "animation")


async def _tg_send_file(
    local_path: Path, caption: str, reply_to: Optional[int] = None, parse_mode: Optional[str] = None
):
    suffix = local_path.suffix.lower()
    with open(local_path, "rb") as f:
        if suffix in (".jpg", ".jpeg", ".png", ".webp"):
            return await telegram_app.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID, photo=f, caption=caption,
                reply_to_message_id=reply_to, parse_mode=parse_mode,
            )
        elif suffix in (".mp4", ".mov", ".webm"):
            return await telegram_app.bot.send_video(
                chat_id=TELEGRAM_CHAT_ID, video=f, caption=caption,
                reply_to_message_id=reply_to, parse_mode=parse_mode,
            )
        elif suffix in (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".opus"):
            return await telegram_app.bot.send_audio(
                chat_id=TELEGRAM_CHAT_ID, audio=f, caption=caption,
                reply_to_message_id=reply_to, parse_mode=parse_mode,
            )
        else:
            return await telegram_app.bot.send_document(
                chat_id=TELEGRAM_CHAT_ID, document=f, caption=caption,
                reply_to_message_id=reply_to, parse_mode=parse_mode,
            )


async def tg_handle_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    title = chat.title or chat.first_name or "this chat"
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"ID of “{title}”: `{chat.id}`\n\n"
            f"Put this value into the TELEGRAM_CHAT_ID variable in .env "
            f"and restart the bot to enable message forwarding."
        ),
        parse_mode="Markdown",
    )
    logger.info("/chatid command: chat_id=%s (%s)", chat.id, title)


async def tg_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    if msg.message_id in _verification_forward_ids:
        return
    if msg.from_user and msg.from_user.id == context.bot.id:
        return

    if msg.from_user:
        raw_author_name = msg.from_user.first_name
        author_key = f"tg:{msg.from_user.id}"
    elif msg.sender_chat:
        raw_author_name = msg.sender_chat.title or "Telegram"
        author_key = f"tg:channel:{msg.sender_chat.id}"
    else:
        raw_author_name = "Telegram"
        author_key = "tg:unknown"
    author = _colored_author(author_key, raw_author_name)
    has_media = any(getattr(msg, attr, None) for attr in _MEDIA_ATTRS)

    replied_id = msg.reply_to_message.message_id if msg.reply_to_message else None

    if replied_id and msg.text and not has_media and _is_emoji_only(msg.text):
        reaction_key = _get_reply_key("tg", replied_id)
        if reaction_key:
            await _mirror_reactions(reaction_key, _extract_emojis(msg.text), skip="tg")
            return

    reply_targets = _get_reply_targets("tg", replied_id) if replied_id else {}

    if msg.poll:
        lines = [f"📊 Poll from {author}: {msg.poll.question}"]
        for i, option in enumerate(msg.poll.options, start=1):
            lines.append(f"{i}) {option.text}")
        if msg.poll.is_anonymous is False:
            lines.append("(the poll is not anonymous)")
        text = "\n".join(lines)
        key = _new_bridge_record(tg=msg.message_id)
        if xmpp_bridge:
            try:
                xmpp_id = await xmpp_bridge.send_text(
                    text, reply_to=reply_targets.get("xmpp"), reply_to_jid=reply_targets.get("xmpp_to")
                )
                _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
            except Exception:
                logger.exception("Could not forward the poll from Telegram to XMPP")
        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_text(text, reply_to=reply_targets.get("discord"))
                _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Could not forward the poll from Telegram to Discord")
        return

    if msg.text and not has_media:
        xmpp_body = _format_tg_text(msg.text, msg.text_html, "xmpp")
        discord_body = _format_tg_text(msg.text, msg.text_html, "discord")
        key = _new_bridge_record(tg=msg.message_id)
        if xmpp_bridge:
            try:
                xmpp_full_text = f"{author}: {xmpp_body}"
                _quote = _get_xmpp_quote_preview("tg", replied_id) if replied_id else None
                if _quote:
                    xmpp_full_text = f"> {_quote}\n{xmpp_full_text}"
                xmpp_id = await xmpp_bridge.send_text(
                    xmpp_full_text, reply_to=reply_targets.get("xmpp"), reply_to_jid=reply_targets.get("xmpp_to")
                )
                _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
                _remember_xmpp_text(key, xmpp_full_text)
            except Exception:
                logger.exception("Could not forward the message from Telegram to XMPP")
        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_text(
                    f"{author}: {discord_body}", reply_to=reply_targets.get("discord")
                )
                _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Could not forward the message from Telegram to Discord")
        return

    if not has_media:
        return

    caption_xmpp = f"{author}: {_format_tg_text(msg.caption, msg.caption_html, 'xmpp')}" if msg.caption else author
    caption_discord = (
        f"{author}: {_format_tg_text(msg.caption, msg.caption_html, 'discord')}" if msg.caption else author
    )
    if replied_id:
        _quote = _get_xmpp_quote_preview("tg", replied_id)
        if _quote:
            caption_xmpp = f"> {_quote}\n{caption_xmpp}"

    file_obj = None
    suffix = ""
    is_static_sticker = False

    try:
        if msg.photo:
            file_obj = await msg.photo[-1].get_file()
            suffix = ".jpg"
        elif msg.video:
            file_obj = await msg.video.get_file()
            suffix = Path(msg.video.file_name or "video.mp4").suffix or ".mp4"
        elif msg.video_note:
            file_obj = await msg.video_note.get_file()
            suffix = ".mp4"
        elif msg.animation:
            file_obj = await msg.animation.get_file()
            suffix = Path(msg.animation.file_name or "animation.mp4").suffix or ".mp4"
        elif msg.voice:
            file_obj = await msg.voice.get_file()
            suffix = ".ogg"
        elif msg.audio:
            file_obj = await msg.audio.get_file()
            suffix = Path(msg.audio.file_name or "audio.mp3").suffix or ".mp3"
        elif msg.document:
            file_obj = await msg.document.get_file()
            suffix = Path(msg.document.file_name or "file").suffix
        elif msg.sticker:
            file_obj = await msg.sticker.get_file()
            if msg.sticker.is_animated:
                suffix = ".tgs"
            elif msg.sticker.is_video:
                suffix = ".webm"
            else:
                suffix = ".webp"
                is_static_sticker = True
    except BadRequest as e:
        if "too big" in str(e).lower():
            logger.warning("The file exceeds the Telegram Bot API limit: %s", e)
            notice = (
                f"{author}: [file not forwarded — exceeds the Telegram Bot API limit "
                f"of {TELEGRAM_BOT_API_FILE_LIMIT_MB} MB for bot downloads]"
            )
            key = _new_bridge_record(tg=msg.message_id)
            if xmpp_bridge:
                try:
                    xmpp_id = await xmpp_bridge.send_text(notice)
                    if xmpp_id:
                        _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
                except Exception:
                    logger.exception("Could not forward the size limit notice to XMPP")
            if discord_bridge:
                try:
                    discord_id = await discord_bridge.send_text(notice)
                    if discord_id:
                        _link_record(key, discord=discord_id)
                except Exception:
                    logger.exception("Could not forward the size limit notice to Discord")
        else:
            logger.exception("Telegram error while fetching the file")
        return

    if file_obj is None:
        return

    local_path = TMP_DIR / f"{file_obj.file_unique_id}{suffix}"
    key = _new_bridge_record(tg=msg.message_id)
    try:
        await file_obj.download_to_drive(custom_path=str(local_path))
        if is_static_sticker:
            local_path = _convert_static_sticker_to_png(local_path)
        if xmpp_bridge:
            try:
                xmpp_id = await xmpp_bridge.send_file(
                    local_path, caption=caption_xmpp,
                    reply_to=reply_targets.get("xmpp"), reply_to_jid=reply_targets.get("xmpp_to"),
                )
                if xmpp_id:
                    _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
            except Exception:
                logger.exception("Could not forward media from Telegram to XMPP")
        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_file(
                    local_path, caption=caption_discord, reply_to=reply_targets.get("discord")
                )
                if discord_id:
                    _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Could not forward media from Telegram to Discord")
    except Exception:
        logger.exception("Could not download media from Telegram")
    finally:
        try:
            local_path.unlink(missing_ok=True)
        except PermissionError:
            logger.warning(
                "Could not delete temporary file %s right away — it is still in use, leaving it",
                local_path,
            )


async def tg_handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    if msg.from_user and msg.from_user.id == context.bot.id:
        return

    _prune_bridge_records()
    key = _tg_lookup.get(msg.message_id)
    record = _bridge_records.get(key) if key else None
    if not record or time.time() - record["ts"] > EDIT_WINDOW_SECONDS:
        logger.info(
            "Message %s was edited, but it is older than %s s or was not forwarded — ignoring",
            msg.message_id, EDIT_WINDOW_SECONDS,
        )
        return

    if msg.from_user:
        author = _colored_author(f"tg:{msg.from_user.id}", msg.from_user.first_name)
    elif msg.sender_chat:
        author = _colored_author(f"tg:channel:{msg.sender_chat.id}", msg.sender_chat.title or "Telegram")
    else:
        author = _colored_author("tg:unknown", "Telegram")

    if xmpp_bridge and record.get("xmpp"):
        try:
            xmpp_text = f"{author}: {_format_tg_text(msg.text, msg.text_html, 'xmpp')} (edited)"
            new_xmpp_id = await xmpp_bridge.send_correction(xmpp_text, record["xmpp"])
            if new_xmpp_id:
                _link_record(key, xmpp=new_xmpp_id)
        except Exception:
            logger.exception("Could not forward the correction to XMPP")
    if discord_bridge and record.get("discord"):
        try:
            discord_text = f"{author}: {_format_tg_text(msg.text, msg.text_html, 'discord')}"
            await discord_bridge.send_correction(record["discord"], discord_text)
        except Exception:
            logger.exception("Could not forward the correction to Discord")


async def tg_handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction = update.message_reaction
    if reaction is None:
        return

    _prune_bridge_records()
    key = _tg_lookup.get(reaction.message_id)
    if not key:
        return

    emojis = {r.emoji for r in reaction.new_reaction if isinstance(r, ReactionTypeEmoji)}
    await _mirror_reactions(key, emojis, skip="tg")


async def _start_healthcheck_server():
    port_raw = os.environ.get("PORT")
    if not port_raw:
        return None

    from aiohttp import web

    async def _health(request):
        status = "bridge active" if BRIDGE_ENABLED else "bot running in /chatid mode"
        return web.Response(text=f"OK — {status}")

    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port_raw))
    await site.start()
    logger.info("HTTP health-check server listening on port %s (for Render or similar)", port_raw)
    return runner


TELEGRAM_DELETE_CHECK_INTERVAL = int(os.environ.get("TELEGRAM_DELETE_CHECK_INTERVAL", "300"))
TELEGRAM_DELETE_CHECK_BATCH = int(os.environ.get("TELEGRAM_DELETE_CHECK_BATCH", "30"))
TELEGRAM_DELETE_CHECK_DELAY = float(os.environ.get("TELEGRAM_DELETE_CHECK_DELAY", "1.0"))

_verification_forward_ids: set = set()


async def _forget_verification_id_later(message_id: int, delay: float = 30.0) -> None:
    await asyncio.sleep(delay)
    _verification_forward_ids.discard(message_id)


_non_editable_tg_messages: set = set()
_last_forward_check: dict = {}
TELEGRAM_FORWARD_CHECK_MIN_INTERVAL = float(
    os.environ.get("TELEGRAM_FORWARD_CHECK_MIN_INTERVAL", str(12 * 3600))
)
TELEGRAM_FORWARD_CHECK_ENABLED = (
    os.environ.get("TELEGRAM_FORWARD_CHECK_ENABLED", "true").strip().lower() == "true"
)


async def _tg_check_deletions_loop() -> None:
    logger.info(
        "Telegram deletion check started (every %s s, up to %s messages per pass)",
        TELEGRAM_DELETE_CHECK_INTERVAL, TELEGRAM_DELETE_CHECK_BATCH,
    )
    deleted_markers = (
        "message to edit not found",
        "message_id_invalid",
        "message to delete not found",
        "message to forward not found",
        "message can't be found",
    )

    while True:
        await asyncio.sleep(TELEGRAM_DELETE_CHECK_INTERVAL)
        if not telegram_app or not TELEGRAM_CHAT_ID:
            continue
        _prune_bridge_records()
        items = [(k, v) for k, v in _bridge_records.items() if v.get("tg")]
        items = items[-TELEGRAM_DELETE_CHECK_BATCH:]

        for key, record in items:
            tg_id = record.get("tg")
            if not tg_id:
                continue

            if tg_id in _non_editable_tg_messages:
                if not TELEGRAM_FORWARD_CHECK_ENABLED:
                    continue
                last = _last_forward_check.get(tg_id, 0.0)
                if time.monotonic() - last < TELEGRAM_FORWARD_CHECK_MIN_INTERVAL:
                    continue
                _last_forward_check[tg_id] = time.monotonic()

                try:
                    forwarded = await telegram_app.bot.forward_message(
                        chat_id=TELEGRAM_CHAT_ID, from_chat_id=TELEGRAM_CHAT_ID, message_id=tg_id
                    )
                    _verification_forward_ids.add(forwarded.message_id)
                    try:
                        await telegram_app.bot.delete_message(
                            chat_id=TELEGRAM_CHAT_ID, message_id=forwarded.message_id
                        )
                    except Exception:
                        logger.exception(
                            "Could not delete the temporary copy (id %s) after the forward check",
                            forwarded.message_id,
                        )
                    asyncio.create_task(_forget_verification_id_later(forwarded.message_id))
                except BadRequest as fe:
                    if any(marker in str(fe).lower() for marker in deleted_markers):
                        logger.info(
                            "Deletion of Telegram message %s detected (via forward) — "
                            "deleting on the other platforms", tg_id,
                        )
                        await _propagate_deletion(record, skip="tg")
                        _forget_record_by_key(key)
                        _non_editable_tg_messages.discard(tg_id)
                        _last_forward_check.pop(tg_id, None)
                except Exception:
                    pass
                await asyncio.sleep(TELEGRAM_DELETE_CHECK_DELAY)
                continue

            try:
                await telegram_app.bot.edit_message_reply_markup(
                    chat_id=TELEGRAM_CHAT_ID, message_id=tg_id, reply_markup=None
                )
            except BadRequest as e:
                error_text = str(e).lower()
                if any(marker in error_text for marker in deleted_markers):
                    logger.info(
                        "Deletion of Telegram message %s detected — deleting on the other platforms", tg_id
                    )
                    await _propagate_deletion(record, skip="tg")
                    _forget_record_by_key(key)
                elif "message can't be edited" in error_text:
                    _non_editable_tg_messages.add(tg_id)
                    _last_forward_check[tg_id] = time.monotonic()
                elif "message is not modified" not in error_text:
                    logger.info(
                        "Telegram deletion check %s: unrecognized BadRequest: %s", tg_id, e
                    )
            except Exception as e:
                logger.info("Telegram deletion check %s: other error %s: %s", tg_id, type(e).__name__, e)
            await asyncio.sleep(TELEGRAM_DELETE_CHECK_DELAY)


_telegram_userbot = None


def _tg_chat_ids_match(a: int, b: int) -> bool:

    def _core(x: int) -> int:
        s = str(abs(x))
        if s.startswith("100") and len(s) > 3:
            s = s[3:]
        return int(s)

    return _core(a) == _core(b)


async def _start_telegram_userbot() -> None:
    global _telegram_userbot
    if not TELEGRAM_USERBOT_ENABLED:
        return

    try:
        from telethon import TelegramClient as _TelethonClient
        from telethon import events as _telethon_events
        from telethon.sessions import StringSession as _TelethonStringSession
    except ImportError:
        logger.error(
            "TELEGRAM_USERBOT_SESSION is set but telethon is not installed "
            "(pip install telethon) — instant deletion detection is disabled"
        )
        return

    try:
        client = _TelethonClient(
            _TelethonStringSession(TELEGRAM_USERBOT_SESSION),
            int(TELEGRAM_API_ID), TELEGRAM_API_HASH,
        )
        await client.start()
        _telegram_userbot = client

        @client.on(_telethon_events.MessageDeleted())
        async def _on_tg_message_deleted(event):
            if event.chat_id is None:
                return
            if not _tg_chat_ids_match(event.chat_id, TELEGRAM_CHAT_ID):
                return
            for mid in event.deleted_ids:
                _prune_bridge_records()
                key = _tg_lookup.get(mid)
                if not key:
                    continue
                record = _bridge_records.get(key)
                if not record:
                    continue
                logger.info(
                    "MTProto: instant deletion of Telegram message %s detected — "
                    "deleting on the other platforms", mid,
                )
                await _propagate_deletion(record, skip="tg")
                _forget_record_by_key(key)

        me = await client.get_me()
        logger.info(
            "MTProto account connected (%s) — Telegram deletions are now detected instantly",
            getattr(me, "username", None) or getattr(me, "phone", "?"),
        )
    except Exception:
        logger.exception("Could not connect the MTProto account for deletion tracking")


async def _stop_telegram_userbot() -> None:
    if _telegram_userbot is not None:
        try:
            await _telegram_userbot.disconnect()
        except Exception:
            pass


async def main() -> None:
    global telegram_app, xmpp_bridge, discord_bridge, DISCORD_CHANNEL_ID

    await _init_db()
    await _load_from_db()

    saved_channel = await _db_load_setting("discord_channel_id")
    if saved_channel:
        try:
            DISCORD_CHANNEL_ID = int(saved_channel)
            logger.info("Discord: loaded the previously saved channel (id: %s)", DISCORD_CHANNEL_ID)
        except ValueError:
            pass

    healthcheck_runner = await _start_healthcheck_server()
    await _start_telegram_userbot()

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN).rate_limiter(AIORateLimiter(max_retries=3))

    if TELEGRAM_FORCE_IPV4:
        import httpx

        ipv4_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        forced_request = HTTPXRequest(httpx_kwargs={"transport": ipv4_transport})
        builder = builder.request(forced_request).get_updates_request(forced_request)
        logger.info("Forcing IPv4 for Telegram requests (TELEGRAM_FORCE_IPV4=true)")

    telegram_app = builder.build()

    telegram_app.add_handler(CommandHandler("chatid", tg_handle_chatid))

    allowed_updates = ["message", "edited_message"]

    if BRIDGE_ENABLED:
        telegram_app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.Chat(chat_id=TELEGRAM_CHAT_ID), tg_handle_message
            )
        )
        telegram_app.add_handler(
            MessageHandler(
                filters.UpdateType.EDITED_MESSAGE & filters.Chat(chat_id=TELEGRAM_CHAT_ID),
                tg_handle_edited_message,
            )
        )
        telegram_app.add_handler(
            MessageReactionHandler(tg_handle_reaction, chat_id=TELEGRAM_CHAT_ID)
        )
        allowed_updates.append("message_reaction")

        if XMPP_ENABLED:
            xmpp_bridge = XMPPBridge(XMPP_JID, XMPP_PASSWORD)
            xmpp_bridge.connect()

        if DISCORD_ENABLED:
            if discord is None:
                logger.warning(
                    "DISCORD_BOT_TOKEN is set but discord.py is not installed "
                    "(pip install discord.py) — the Discord side of the bridge is disabled"
                )
            else:
                discord_bridge = DiscordBridge()
                asyncio.create_task(discord_bridge.start(DISCORD_BOT_TOKEN))

        asyncio.create_task(_tg_check_deletions_loop())
    else:
        missing = []
        if not TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        if not XMPP_ENABLED and not DISCORD_ENABLED:
            missing.append("(XMPP_JID+XMPP_PASSWORD+XMPP_PEER_JID) or (DISCORD_BOT_TOKEN+DISCORD_CHANNEL_ID)")
        logger.warning(
            "Bridge NOT active — missing: %s. The bot runs in /chatid mode only.",
            ", ".join(missing),
        )

    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True, allowed_updates=allowed_updates)
        if BRIDGE_ENABLED:
            targets = []
            if XMPP_ENABLED:
                targets.append("XMPP")
            if DISCORD_ENABLED:
                targets.append("Discord")
            logger.info(
                "Telegram bot started, waiting for messages in chat %s. Bridge targets: %s",
                TELEGRAM_CHAT_ID, ", ".join(targets),
            )
        else:
            logger.info("Telegram bot started in /chatid mode (send /chatid in the desired chat)")

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            logger.info("Stopping...")
            await telegram_app.updater.stop()
            await telegram_app.stop()
            if xmpp_bridge is not None:
                xmpp_bridge.disconnect()
            if discord_bridge is not None:
                await discord_bridge.close()
            if healthcheck_runner is not None:
                await healthcheck_runner.cleanup()
            await _stop_telegram_userbot()
            if _db_pool is not None:
                await _db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
