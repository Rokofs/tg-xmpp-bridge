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
    # pip install python-dotenv — тогда .env рядом со скриптом
    # подхватится автоматически. Без пакета переменные нужно задавать
    # вручную перед запуском.
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

# ========================================================================
# КОНФИГУРАЦИЯ (через переменные окружения)
# ========================================================================

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
DISCORD_ENABLED = bool(DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID)

BRIDGE_ENABLED = bool(TELEGRAM_CHAT_ID and (XMPP_ENABLED or DISCORD_ENABLED))

# Некоторые VPN туннелируют только IPv4, а httpx/anyio по умолчанию
# сначала пробует IPv6 и подолгу "виснет" — из-за этого TimedOut даже
# при рабочем интернете. TELEGRAM_FORCE_IPV4=true чинит это.
TELEGRAM_FORCE_IPV4 = os.environ.get("TELEGRAM_FORCE_IPV4", "false").strip().lower() == "true"

TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
TELEGRAM_USERBOT_SESSION = os.environ.get("TELEGRAM_USERBOT_SESSION", "").strip()
TELEGRAM_USERBOT_ENABLED = bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_USERBOT_SESSION)

# ============================================================================
# БАЗА ДАННЫХ (PostgreSQL, например Neon) — НЕОБЯЗАТЕЛЬНЫЙ БЛОК.
# Без неё связка сообщений между площадками (для reply/реакций/правок)
# живёт только в памяти процесса и обнуляется при каждом перезапуске.
# С базой — переживает перезапуски, ограничена только MESSAGE_MAP_TTL_SECONDS.
# Чтобы отключить — не задавайте DATABASE_URL (бот тогда работает как раньше,
# только в памяти).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None
DB_ENABLED = bool(DATABASE_URL)
# ============================================================================

# Сколько секунд после отправки сообщение можно ещё отредактировать и
# получить исправление на других площадках.
EDIT_WINDOW_SECONDS = int(os.environ.get("EDIT_WINDOW_SECONDS", "300"))

# Как долго вообще помнить связку сообщений между площадками (нужно для
# того, чтобы реакции и reply могли долетать спустя долгое время после
# отправки). Раньше это было ограничено 6, потом 24 часами — но это было
# ДО того, как появилась настоящая база данных (DATABASE_URL). Теперь,
# когда связка не пропадает при перезапуске, можно позволить себе
# хранить её действительно долго — по умолчанию 180 дней (полгода).
# Место в базе это почти не расходует (каждая запись — пара id, десятки-
# сотни байт), так что можно смело ставить и больше через переменную
# окружения. Если DATABASE_URL НЕ задан (работа только в памяти) —
# большой TTL смысла не имеет, всё равно обнулится при следующем
# перезапуске/передеплое.
MESSAGE_MAP_TTL_SECONDS = int(os.environ.get("MESSAGE_MAP_TTL_SECONDS", str(180 * 24 * 3600)))

# Сколько последних XMPP-сообщений держать для эвристики "угадывания"
# reply по тексту цитаты (для клиентов вроде Monocles, которые не шлют
# структурированный <reply>, а вставляют "> текст" вручную). Раньше было
# 200 — подняли, раз теперь есть настоящая база и место не проблема.
RECENT_XMPP_TEXTS_LIMIT = int(os.environ.get("RECENT_XMPP_TEXTS_LIMIT", "2000"))

TELEGRAM_BOT_API_FILE_LIMIT_MB = 20  # ограничение самого Telegram, не скрипта

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("tg_xmpp_bridge")
logging.getLogger("slixmpp").setLevel(logging.WARNING)
logging.getLogger("discord").setLevel(logging.WARNING)

TMP_DIR = Path(tempfile.gettempdir()) / "tg_xmpp_bridge"
TMP_DIR.mkdir(parents=True, exist_ok=True)


# ========================================================================
# СВЯЗКА СООБЩЕНИЙ МЕЖДУ ПЛОЩАДКАМИ (для правок и реакций)
# ========================================================================
# Каждое пересланное сообщение получает один "bridge key". Для него
# храним, под каким id оно было отправлено на каждой из площадок —
# это нужно, чтобы потом найти, ЧТО поправить/на чём поставить
# реакцию, когда придёт событие с любой стороны.
#
# ВАЖНО: ключи — настоящие UUID, а не последовательный счётчик. Счётчик,
# начинающий с 1 при каждом перезапуске, при подключённой базе данных
# (DATABASE_URL) сталкивался бы с ключами старых записей, загруженных из
# базы (у них тоже маленькие номера из прошлой сессии) — реакция/ответ
# по такому "переиспользованному" ключу мог попасть на совершенно
# другое, чужое сообщение.

_bridge_records: dict = {}   # key -> {"tg": int|None, "xmpp": str|None, "discord": int|None, "ts": float}
_tg_lookup: dict = {}        # tg_message_id -> key
_xmpp_lookup: dict = {}      # xmpp_stanza_id -> key
_discord_lookup: dict = {}   # discord_message_id -> key


# ------------------------------------------------------------------------
# Цветные метки авторов — у каждого нового автора (по стабильному ключу
# платформа+id) появляется свой цветной значок, который потом всегда
# ставится рядом с его именем при пересылке на другие площадки.
# ------------------------------------------------------------------------
_COLOR_EMOJIS = [
    "🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "⚫️", "⚪️", "🟤",
    "🔺", "🔻", "🔸", "🔹", "🔶", "🔷", "🔳", "🔲",
    "🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "⬛️", "⬜️", "🟫",
]
_author_colors: dict = {}
_color_counter = itertools.count(0)


def _colored_author(key: str, display_name: str) -> str:
    """Возвращает "значок Имя" — значок стабильно закреплён за key
    (например, "tg:12345" или "discord:98765" или "xmpp:nick")."""
    if key not in _author_colors:
        idx = next(_color_counter) % len(_COLOR_EMOJIS)
        _author_colors[key] = _COLOR_EMOJIS[idx]
    return f"{_author_colors[key]} {display_name}"


def _own_xmpp_reply_jid() -> str:
    """JID, который надо указывать в 'to' у <reply>/<reactions> для
    сообщений, которые в комнату/чат отправил сам бот (не важно, чей
    текст внутри — TG или Discord автора)."""
    if XMPP_IS_MUC:
        return f"{XMPP_PEER_JID}/{XMPP_MUC_NICK}"
    return XMPP_PEER_JID


_db_pool = None  # type: Optional["asyncpg.Pool"]


async def _init_db() -> None:
    """Подключается к базе (если задан DATABASE_URL) и создаёт таблицы,
    если их ещё нет. При любой ошибке просто отключает работу с базой —
    бот продолжит работать как раньше, только в памяти."""
    global _db_pool
    if not DB_ENABLED:
        return
    try:
        import asyncpg
    except ImportError:
        logger.error(
            "DATABASE_URL задан, но asyncpg не установлен (pip install asyncpg) — "
            "связка сообщений будет жить только в памяти"
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
        logger.info("База данных подключена — связка сообщений теперь переживёт перезапуск бота")
    except Exception:
        logger.exception("Не удалось подключиться к базе данных — работаю только в памяти (как раньше)")
        _db_pool = None


async def _load_from_db() -> None:
    """Вызывается один раз при старте — подгружает в память всё, что ещё
    не устарело (в рамках MESSAGE_MAP_TTL_SECONDS)."""
    if _db_pool is None:
        return
    try:
        now = time.time()
        async with _db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, tg, xmpp, discord, xmpp_to, ts FROM bridge_records")
            loaded = 0
            for row in rows:
                if now - row["ts"] > MESSAGE_MAP_TTL_SECONDS:
                    continue
                record = {
                    "tg": row["tg"], "xmpp": row["xmpp"], "discord": row["discord"],
                    "xmpp_to": row["xmpp_to"], "ts": row["ts"],
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
            "Загружено из базы: %s связок сообщений, %s недавних текстов для цитат",
            loaded, len(text_rows),
        )
    except Exception:
        logger.exception("Не удалось загрузить связку сообщений из базы данных")


def _db_save_record(key: str, record: dict) -> None:
    """Запись в базу — асинхронно, не блокируя основной поток обработки."""
    if _db_pool is None:
        return
    asyncio.create_task(_db_save_record_impl(key, record))


async def _db_save_record_impl(key: str, record: dict) -> None:
    try:
        async with _db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bridge_records (key, tg, xmpp, discord, xmpp_to, ts)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (key) DO UPDATE SET
                    tg = EXCLUDED.tg, xmpp = EXCLUDED.xmpp, discord = EXCLUDED.discord,
                    xmpp_to = EXCLUDED.xmpp_to, ts = EXCLUDED.ts
                """,
                key, record.get("tg"), record.get("xmpp"), record.get("discord"),
                record.get("xmpp_to"), record["ts"],
            )
    except Exception:
        logger.exception("Не удалось сохранить связку сообщения в базу данных")


def _db_delete_record(key: str) -> None:
    if _db_pool is None:
        return
    asyncio.create_task(_db_delete_record_impl(key))


async def _db_delete_record_impl(key: str) -> None:
    try:
        async with _db_pool.acquire() as conn:
            await conn.execute("DELETE FROM bridge_records WHERE key = $1", key)
    except Exception:
        logger.exception("Не удалось удалить связку сообщения из базы данных")


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
            # Не даём таблице расти бесконечно — оставляем только последние RECENT_XMPP_TEXTS_LIMIT.
            await conn.execute(
                """
                DELETE FROM recent_xmpp_texts WHERE id NOT IN (
                    SELECT id FROM recent_xmpp_texts ORDER BY ts DESC LIMIT $1
                )
                """,
                RECENT_XMPP_TEXTS_LIMIT,
            )
    except Exception:
        logger.exception("Не удалось сохранить текст для цитаты в базу данных")


def _new_bridge_record(**ids) -> str:
    key = uuid.uuid4().hex
    record = {"tg": None, "xmpp": None, "discord": None, "xmpp_to": None, "ts": time.time()}
    record.update({k: v for k, v in ids.items() if v is not None})
    _bridge_records[key] = record
    _link_lookup(key, record)
    _db_save_record(key, record)
    return key


def _link_record(key: str, **ids) -> None:
    record = _bridge_records.get(key)
    if not record:
        return
    for k, v in ids.items():
        if v is not None:
            record[k] = v
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
    """Полностью убирает связку из памяти — вызывается после того, как
    удаление сообщения уже разослано на все площадки, чтобы то же самое
    "мёртвое" сообщение не находилось заново на следующих проверках
    (иначе бот будет бесконечно пересоздавать заглушку "удалено")."""
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
    """Проставляет набор реакций emojis на всех площадках, кроме skip."""
    record = _bridge_records.get(key)
    if not record:
        return

    if skip != "xmpp" and xmpp_bridge and record.get("xmpp"):
        try:
            await xmpp_bridge.send_reaction(record["xmpp"], emojis)
        except Exception:
            logger.exception("Не удалось переслать реакцию в XMPP")

    if skip != "discord" and discord_bridge and record.get("discord"):
        try:
            await discord_bridge.set_reactions(record["discord"], emojis)
        except Exception:
            logger.exception("Не удалось переслать реакцию в Discord")

    if skip != "tg" and telegram_app and record.get("tg"):
        try:
            await telegram_app.bot.set_message_reaction(
                chat_id=TELEGRAM_CHAT_ID,
                message_id=record["tg"],
                reaction=[ReactionTypeEmoji(e) for e in emojis] if emojis else [],
            )
        except Exception:
            logger.exception("Не удалось переслать реакцию в Telegram")


async def _propagate_deletion(record: dict, skip: str) -> None:
    """Удаляет сообщение на всех площадках, кроме skip (той, где его
    удалили изначально)."""
    if skip != "tg" and telegram_app and record.get("tg"):
        try:
            await telegram_app.bot.delete_message(chat_id=TELEGRAM_CHAT_ID, message_id=record["tg"])
        except BadRequest as e:
            if "message to delete not found" in str(e).lower():
                # Уже удалено (например, другой путь обнаружения удаления
                # сработал параллельно чуть раньше) — не ошибка, просто
                # гонка, ничего страшного.
                pass
            else:
                logger.exception("Не удалось удалить сообщение в Telegram")
        except Exception:
            logger.exception("Не удалось удалить сообщение в Telegram")

    if skip != "xmpp" and xmpp_bridge and record.get("xmpp"):
        try:
            await xmpp_bridge.send_retraction_reliable(record["xmpp"])
        except Exception:
            logger.exception("Не удалось отправить удаление в XMPP")

    if skip != "discord" and discord_bridge and record.get("discord"):
        try:
            await discord_bridge.delete_message(record["discord"])
        except Exception:
            logger.exception("Не удалось удалить сообщение в Discord")


def _get_reply_targets(origin: str, replied_native_id) -> dict:
    """По id сообщения, на которое отвечают (на площадке origin),
    возвращает id этого же сообщения на двух других площадках —
    чтобы отправить туда настоящий reply, а не просто текст.
    """
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
    """То же, что _get_reply_targets, но возвращает сам bridge key (для реакций)."""
    if replied_native_id is None:
        return None
    _prune_bridge_records()
    lookup = {"tg": _tg_lookup, "xmpp": _xmpp_lookup, "discord": _discord_lookup}[origin]
    return lookup.get(replied_native_id)


def _get_xmpp_quote_preview(origin: str, replied_native_id) -> Optional[str]:
    """Возвращает короткий текстовый превью сообщения, на которое отвечают,
    чтобы вставить его цитатой ("> ...") в само тело XMPP-сообщения —
    "по старинке", для клиентов вроде Monocles Chat, которые не рисуют
    структурированный XEP-0461 reply как настоящий ответ."""
    key = _get_reply_key(origin, replied_native_id)
    if not key:
        return None
    for k, text, _ts in reversed(_recent_xmpp_texts):
        if k == key:
            # Если цитируемое сообщение само уже содержит вложенную цитату
            # (например, это был чей-то предыдущий ответ) — берём только
            # "новую" часть, а не всё целиком. Иначе с каждым следующим
            # ответом в цепочке цитата разрастается ("> > > > ...").
            if text.strip().startswith(">"):
                _quoted, remaining = _parse_manual_quote(text)
                if remaining:
                    text = remaining
            snippet = text.strip().replace("\n", " ")
            if len(snippet) > 150:
                snippet = snippet[:150].rstrip() + "…"
            return snippet
    return None


# ------------------------------------------------------------------------
# ЭКСПЕРИМЕНТАЛЬНОЕ: распознавание "ручных" текстовых цитат Monocles Chat
# ------------------------------------------------------------------------
# Monocles Chat, судя по всему, не отправляет структурированный XEP-0461
# reply — вместо этого он просто вставляет цитату исходного сообщения
# текстом прямо в тело: "> исходный текст\n\nновый текст". Ниже — попытка
# распознать такую цитату и сопоставить её с недавно отправленными в эту
# же XMPP-комнату сообщениями по содержимому текста, чтобы всё равно
# получить настоящий reply на других площадках. Метод эвристический —
# может ошибаться на коротких/похожих сообщениях.

_recent_xmpp_texts: list = []  # [(key, text, ts), ...]


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
    # Нормализуем пробельные символы перед сравнением: _parse_manual_quote
    # склеивает строки цитаты пробелами, а сохранённый оригинал текста
    # хранит настоящие переносы строк — без нормализации многострочные
    # сообщения никогда не совпадут как подстрока.
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
    # ВРЕМЕННАЯ ДИАГНОСТИКА: совпадение не нашлось — печатаем repr()
    # (видны скрытые/невидимые символы, которые иногда вставляет
    # Monocles) и несколько последних запомненных текстов для сравнения.
    try:
        recent_sample = [(k, t) for k, t, _ts in list(reversed(_recent_xmpp_texts))[:5]]
        logger.info(
            "ДИАГНОСТИКА: не нашлось совпадение цитаты. quoted=%r; последние запомненные тексты: %r",
            quoted, recent_sample,
        )
    except Exception:
        pass
    return None


_MOBILE_QUOTE_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\(GMT[+-]?\d*\)$")


def _parse_manual_quote(body: str):
    """Разбирает тело сообщения на "цитируемую" часть (строки, начинающиеся
    с "> ") и обычный текст — сканирует ВСЕ строки сообщения, а не только
    идущие подряд с самого начала (цитата может быть не первой). Не
    считает цитатой вложенные "> >" (чужая цитата внутри цитаты). Также
    учитывает особенность некоторых мобильных XMPP-клиентов, которые
    перед самой цитатой вставляют отдельную служебную строку вида
    "2024-01-01  12:00 (GMT+0000)" — такую строку (и предшествующую ей
    строку с именем отправителя) не считаем частью реальной цитаты.

    Возвращает (quoted_text, remaining_text). Если цитаты нет —
    (None, body).

    Подход подсмотрен в похожем проекте jabagram (ventureoo/jabagram,
    GPL-3.0) и переписан под наш стиль.
    """
    quoted_lines: list = []
    other_lines: list = []

    for line in body.split("\n"):
        if line[:1] == ">" and line[1:2] == " " and line[2:3] != ">":
            content = line[2:].strip()
            if _MOBILE_QUOTE_TIMESTAMP_RE.match(content):
                # служебная строка с датой/временем от мобильного клиента —
                # значит, и предыдущая добавленная строка (имя автора)
                # тоже была служебной, а не текстом цитаты
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
    """Убирает "сырые" ручные цитаты из текста перед отправкой в Telegram —
    там уже работает настоящий reply_to_message_id, а вложенный текст
    цитаты только захламляет сообщение (особенно после нескольких
    "прыжков" через сторонних ботов вроде matrixt2bot/ChatResender, у
    каждого из которых свой формат цитирования текстом).

    В отличие от _parse_manual_quote (который используется для XMPP и
    намеренно НЕ трогает вложенные ">>"-цитаты, чтобы не потерять
    настоящий текст при сопоставлении reply) — тут чистим агрессивно:
    убираем любые строки, начинающиеся с одного или нескольких ">",
    независимо от вложенности. Для Telegram нам не нужно ничего из
    этого сохранять — там и так есть нормальный reply."""
    if not text:
        return text
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        # Строка, целиком состоящая из голой ссылки — почти всегда
        # дублирующая ссылка на уже прикреплённый/процитированный файл
        # (Monocles вставляет такие ссылки текстом внутри цитат).
        if re.fullmatch(r"https?://\S+", line.strip()):
            continue
        lines.append(line)
    remaining = "\n".join(lines).strip()
    return remaining if remaining else text


# Простое (не идеальное) распознавание "текст состоит только из эмодзи" —
# без внешней зависимости на пакет `emoji`. Покрывает подавляющее
# большинство современных эмодзи (включая ZWJ-последовательности и
# variation selector), но не претендует на 100% полноту юникода.
_EMOJI_ONLY_RE = re.compile(
    r"^[\s\u2600-\u27BF\U0001F000-\U0001FFFF\u2190-\u21FF\u2300-\u23FF\uFE0F\u200D]+$"
)


def _is_emoji_only(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    return bool(_EMOJI_ONLY_RE.match(text))


def _extract_emojis(text: str) -> set:
    """Пытается разбить текст на отдельные эмодзи через пакет `emoji`
    (если установлен), иначе просто возвращает весь текст одним
    элементом множества — этого достаточно для типичного случая
    "ответ одним эмодзи".
    """
    try:
        import emoji as emoji_lib
        found = {m["emoji"] for m in emoji_lib.emoji_list(text)}
        if found:
            return found
    except ImportError:
        pass
    return {text.strip()}


# Модульные ссылки на "живые" мосты — заполняются в main(), используются
# отовсюду для кросс-пересылки без сложных циклических зависимостей.
telegram_app: Optional[Application] = None
xmpp_bridge = None       # type: Optional["XMPPBridge"]
discord_bridge = None    # type: Optional["DiscordBridge"]


# ========================================================================
# ОБЩИЕ ПОМОЩНИКИ
# ========================================================================

_DISCORD_MARKERS = {
    "b": "**", "strong": "**",
    "i": "*", "em": "*",
    "u": "__", "ins": "__",
    "s": "~~", "strike": "~~", "del": "~~",
}
# XEP-0393 (Message Styling) поддерживает только *bold*, _italic_, ~strike~,
# `code`/```pre``` и > quote — нет подчёркивания и спойлеров, поэтому для
# них маркер просто пустой (форматирование теряется, но текст остаётся).
_XMPP_MARKERS = {
    "b": "*", "strong": "*",
    "i": "_", "em": "_",
    "s": "~", "strike": "~", "del": "~",
}


def _format_from_telegram_html(html_text: str, target: str) -> str:
    """Конвертирует HTML-разметку Telegram (msg.text_html/caption_html) в
    разметку, понятную XMPP (XEP-0393 Message Styling) или Discord
    (обычный markdown). target: 'xmpp' или 'discord'.
    """
    markers = _DISCORD_MARKERS if target == "discord" else _XMPP_MARKERS

    class _Conv(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out = []
            self.tag_stack = []  # [(tag, "закрывающий_маркер_или_href")]

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
                # tg://user?id=... — упоминание пользователя без username
                # (и вообще любые внутренние tg:// ссылки). Они нерабочие
                # за пределами Telegram — не дописываем их в скобках.
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
                    pass  # оставляем голый текст метки, без ссылки
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
        logger.exception("Не удалось разобрать форматирование Telegram, отправляю как обычный текст")
        return html_text
    return "".join(conv.out)


def _format_tg_text(plain_text: Optional[str], html_text: Optional[str], target: str) -> str:
    """Возвращает текст сообщения с конвертированным форматированием,
    либо обычный текст, если форматирования нет/распознать не удалось.
    """
    if not html_text:
        return plain_text or ""
    return _format_from_telegram_html(html_text, target)


# ------------------------------------------------------------------------
# ЭКСПЕРИМЕНТАЛЬНОЕ: конвертация разметки Discord (markdown) и XMPP
# (XEP-0393 Message Styling) друг в друга и в Telegram HTML.
# ------------------------------------------------------------------------
# В отличие от Telegram (где есть точные entity с offset/length),
# здесь приходится РАСПОЗНАВАТЬ разметку по текстовым маркерам через
# регулярки — это эвристика: не гарантирует идеальную обработку
# вложенной/пересекающейся или "случайной" разметки (например, если в
# обычном тексте просто встретилась одинокая звёздочка).

_MD_CODEBLOCK_RE = re.compile(r"```.*?```", re.S)
_MD_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _convert_markup(text: str, source: str, target: str) -> str:
    """source/target: 'discord' | 'xmpp' | 'telegram' (telegram — только
    как target, для получения HTML с parse_mode='HTML').
    """
    if not text or source == target:
        return _html_escape(text, quote=False) if target == "telegram" else text

    if target == "telegram":
        text = _html_escape(text, quote=False)

    # 1. Прячем code/pre-блоки от дальнейшей обработки
    code_stash = []

    def _stash_code(m):
        code_stash.append(m.group(0))
        return f"\x00{len(code_stash) - 1}\x00"

    text = _MD_CODEBLOCK_RE.sub(_stash_code, text)
    text = _MD_INLINE_CODE_RE.sub(_stash_code, text)

    # 2. Прячем ссылки [текст](url)
    link_stash = []

    def _stash_link(m):
        link_stash.append((m.group(1), m.group(2)))
        return f"\x01{len(link_stash) - 1}\x01"

    text = _MD_LINK_RE.sub(_stash_link, text)

    # 3. Распознаём маркеры разметки и СРАЗУ прячем их за плейсхолдеры
    # (а не сразу подставляем целевой синтаксис!) — иначе, например,
    # только что созданный "*bold*" для XMPP тут же попадётся под руку
    # регулярке italic и испортится. Реальный целевой синтаксис
    # подставляется только в конце, за один проход.
    style_stash = []  # [(kind, inner_text), ...]

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
    else:  # xmpp (XEP-0393) — одиночные маркеры, без подчёркивания/спойлера
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
            # подчёркивания/спойлера в XEP-0393 нет — просто нет записи,
            # ниже это означает "убрать разметку, оставить только текст"
        },
    }
    mapping = _TARGET_SYNTAX[target]
    for i, (kind, inner) in enumerate(style_stash):
        placeholder = f"\x02{i}\x02"
        if kind in mapping:
            open_m, close_m = mapping[kind]
            replacement = f"{open_m}{inner}{close_m}"
        else:
            replacement = inner  # аналога нет — оставляем только текст
        text = text.replace(placeholder, replacement)

    # 4. Восстанавливаем ссылки
    for i, (label, url) in enumerate(link_stash):
        placeholder = f"\x01{i}\x01"
        if target == "telegram":
            replacement = f'<a href="{_html_escape(url, quote=True)}">{label}</a>'
        elif target == "discord":
            replacement = f"[{label}]({url})"
        else:
            replacement = url if label == url else f"{label} ({url})"
        text = text.replace(placeholder, replacement)

    # 5. Восстанавливаем code/pre
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
            replacement = original  # discord/xmpp код-разметка совпадает по синтаксису
        text = text.replace(placeholder, replacement)

    return text


def _convert_static_sticker_to_png(path: Path) -> Path:
    """Конвертирует статический webp-стикер в PNG для совместимости."""
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow не установлен — статический стикер отправится как .webp")
        return path
    try:
        png_path = path.with_suffix(".png")
        with Image.open(path) as im:
            im.convert("RGBA").save(png_path, "PNG")
        path.unlink(missing_ok=True)
        return png_path
    except Exception:
        logger.exception("Не удалось сконвертировать стикер в PNG, отправляю как есть")
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
    """Скачивает файл по прямой ссылке (используется для OOB-ссылок из XMPP)."""
    import hashlib
    import mimetypes
    import aiohttp
    from urllib.parse import urlparse

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()

    # Пытаемся определить расширение: сначала из самого URL, иначе по Content-Type.
    suffix = Path(urlparse(url).path).suffix
    if not suffix and content_type:
        suffix = mimetypes.guess_extension(content_type) or ""

    name_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    local_path = TMP_DIR / f"xmpp_{name_hash}{suffix}"
    local_path.write_bytes(data)
    return local_path


# ========================================================================
# XMPP-СТОРОНА
# ========================================================================

class XMPPBridge(slixmpp.ClientXMPP):
    """XMPP-клиент, принимающий/отправляющий текст, файлы, правки, реакции."""

    def __init__(self, jid: str, password: str):
        super().__init__(jid, password)

        self.add_event_handler("session_start", self.on_session_start)
        self.add_event_handler("message", self.on_xmpp_message)
        self.add_event_handler("reactions", self.on_xmpp_reactions)
        self.add_event_handler("message_retract", self.on_xmpp_retraction)

        # client_id -> asyncio.Future, которое разрешается, когда сервер
        # комнаты отражает наше же сообщение обратно с настоящим
        # stanza-id (см. _learn_own_stanza_id и send_text/send_file).
        self._pending_echo_futures: dict = {}

        self.register_plugin("xep_0030")  # Service Discovery
        self.register_plugin("xep_0115")  # Entity Capabilities — ОБЯЗАТЕЛЕН по спеке XEP-0444
        # для того, чтобы другие клиенты доверяли нашей поддержке реакций
        self.register_plugin("xep_0128")  # Service Discovery Extensions — зависимость xep_0363
        self.register_plugin("xep_0199")  # XMPP Ping — держит соединение живым
        self.register_plugin("xep_0066")  # Out of Band Data — ссылки на файлы
        self.register_plugin("xep_0363")  # HTTP Upload
        self.register_plugin("xep_0045")  # Multi-User Chat
        self.register_plugin("xep_0308")  # Last Message Correction — редактирование
        self.register_plugin("xep_0334")  # Message Processing Hints — зависимость xep_0444
        self.register_plugin("xep_0444")  # Message Reactions
        self.register_plugin("xep_0461")  # Message Replies — настоящие ответы на сообщения
        self.register_plugin("xep_0359")  # Unique and Stable Stanza IDs — нужен для reply/реакций в MUC
        self.register_plugin("xep_0424")  # Message Retraction — удаление сообщений

    async def on_session_start(self, event):
        self.send_presence()
        await self.get_roster()

        if XMPP_IS_MUC:
            self.plugin["xep_0045"].join_muc(XMPP_PEER_JID, XMPP_MUC_NICK, password=XMPP_MUC_PASSWORD)
            logger.info("XMPP: зашёл в комнату %s как %s", XMPP_PEER_JID, XMPP_MUC_NICK)

        logger.info("XMPP: сессия начата, вошёл как %s", self.boundjid.bare)

    def _is_from_peer(self, msg) -> bool:
        if XMPP_IS_MUC:
            if str(msg["mucnick"]) == XMPP_MUC_NICK:
                return False  # это наше же эхо из MUC
            return str(msg["from"].bare) == XMPP_PEER_JID
        return str(msg["from"].bare) == XMPP_PEER_JID

    def _learn_own_stanza_id(self, msg) -> None:
        """MUC-сервер отражает нам наши же сообщения обратно и добавляет
        к ним <stanza-id> (XEP-0359) — уникальный id, назначенный именно
        комнатой. Именно на НЕГО (а не на наш собственный client-side id)
        ссылаются reply/reactions в MUC согласно спецификации — без этого
        входящие reply/реакции на наши сообщения не находят совпадения.

        Также разрешает "future", если send_text/send_file ждут
        подтверждения id именно этого сообщения (см. _send_and_confirm).
        """
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

        # ВРЕМЕННАЯ ДИАГНОСТИКА: печатаем occupant-id этого эха — сравниваем
        # occupant-id обычных сообщений с occupant-id retraction-сообщений,
        # проверяем гипотезу про спецификацию (XEP-0424 требует совпадения
        # occupant-id между исходным сообщением и retraction в полу-
        # анонимных MUC). Ищем прямо в сыром XML — не зависит от того,
        # зарегистрирован ли у нас плагин xep_0421.
        try:
            occupant_el = msg.xml.find("{urn:xmpp:occupant-id:0}occupant-id")
            occupant_id = occupant_el.get("id") if occupant_el is not None else None
            is_retraction = msg.xml.find("{urn:xmpp:message-retract:1}retract") is not None
            logger.info(
                "ДИАГНОСТИКА: occupant-id нашего эха (retraction=%s, resolved_id=%s): %s",
                is_retraction, resolved_id, occupant_id,
            )
        except Exception:
            pass

        if real_id and real_id != client_id:
            key = _xmpp_lookup.get(client_id)
            if key:
                _link_record(key, xmpp=real_id)  # добавляет доп. запись в _xmpp_lookup

        if future is not None and not future.done():
            future.set_result(resolved_id)

    async def _send_and_confirm(self, stanza) -> str:
        """Отправляет стензу и, если это MUC, дожидается эха от сервера
        комнаты с настоящим stanza-id (см. _learn_own_stanza_id) —
        нужно, чтобы reply/реакции на это сообщение сразу ссылались на
        правильный id, а не ловили гонку при быстрых последовательных
        сообщениях. Если эхо не пришло за разумное время — используем
        клиентский id как есть (не блокируем работу бота навсегда).
        """
        client_id = stanza["id"]
        if stanza["reply"]["id"]:
            # ВРЕМЕННАЯ ДИАГНОСТИКА: печатаем ровно то, что реально уходит
            # на сервер, когда это reply — чтобы исключить любые сюрпризы.
            logger.info("ДИАГНОСТИКА: отправляю reply-стензу:\n%s", str(stanza))
        future = None
        if XMPP_IS_MUC:
            future = asyncio.get_event_loop().create_future()
            self._pending_echo_futures[client_id] = future
        stanza.send()
        if future is None:
            return client_id
        try:
            resolved = await asyncio.wait_for(future, timeout=8)
            if stanza["reply"]["id"]:
                logger.info("ДИАГНОСТИКА: подтверждённый канонический id = %s", resolved)
            return resolved
        except asyncio.TimeoutError:
            self._pending_echo_futures.pop(client_id, None)
            logger.warning(
                "Не дождался эха от MUC для сообщения %s за 8 сек — "
                "использую клиентский id как есть (reply на него может не сработать)",
                client_id,
            )
            return client_id

    async def on_xmpp_message(self, msg):
        """Обрабатывает входящие XMPP-сообщения: новые и правки."""
        if msg["type"] not in ("chat", "normal", "groupchat"):
            return
        if XMPP_IS_MUC and str(msg["mucnick"]) == XMPP_MUC_NICK:
            # ВРЕМЕННАЯ ДИАГНОСТИКА: подтверждаем, что сервер реально
            # отразил обратно нашу reaction-стензу (как раньше делали
            # для retraction).
            if msg["reactions"]["id"]:
                logger.info("ДИАГНОСТИКА: сервер отразил обратно нашу reaction-стензу:\n%s", str(msg))
            self._learn_own_stanza_id(msg)
            return
        if not self._is_from_peer(msg):
            return

        # Стензы retraction (XEP-0424) часто несут текстовый <body> как
        # fallback для клиентов без поддержки удаления (например, Monocles
        # сама подставляет "This message has been retracted by the
        # sender."). Отдельный обработчик on_xmpp_retraction уже разбирает
        # такие стензы для пересылки удаления — здесь их нужно
        # проигнорировать, иначе fallback-текст улетит как обычное новое
        # сообщение на все площадки.
        if msg["retract"]["id"]:
            return

        # То же самое с реакциями (XEP-0444) — Monocles шлёт structured
        # <reactions> ВМЕСТЕ с текстовым fallback-телом (цитата + сам
        # эмодзи) для клиентов без поддержки реакций. on_xmpp_reactions
        # уже обрабатывает такие стензы отдельно — здесь их надо
        # проигнорировать, иначе fallback-текст улетит как отдельное
        # новое сообщение вдобавок к самой реакции.
        if msg["reactions"]["id"]:
            return

        body = (msg["body"] or "").strip()
        oob_url = msg["oob"]["url"] or None
        correction_id = msg["replace"]["id"] or None

        if not body and not oob_url:
            return

        sender = str(msg["mucnick"]) if XMPP_IS_MUC else str(msg["from"].bare)
        display_sender = _colored_author(f"xmpp:{sender}", sender)

        # ВРЕМЕННАЯ ДИАГНОСТИКА: если структурированная правка (XEP-0308)
        # не распознана — печатаем сырой XML, чтобы понять, что реально
        # шлёт клиент при редактировании (аналогично истории с reply).
        if not correction_id:
            logger.info("ДИАГНОСТИКА: сообщение без распознанной правки. Сырой XML:\n%s", str(msg))

        # --- это исправление ранее отправленного сообщения ---
        if correction_id:
            _prune_bridge_records()
            key = _xmpp_lookup.get(correction_id)
            record = _bridge_records.get(key) if key else None
            if not record or time.time() - record["ts"] > EDIT_WINDOW_SECONDS:
                logger.info(
                    "Исправление XMPP-сообщения %s: оригинал не найден или окно "
                    "редактирования истекло — игнорирую",
                    correction_id,
                )
                return

            # Discord сам показывает пометку "(edited)" при вызове .edit() —
            # свою дописывать не нужно. Telegram для правок от ботов такую
            # пометку не показывает вообще — дописываем текстом сами.
            text_tg = (
                f"{_html_escape(display_sender, quote=False)}: "
                f"{_convert_markup(_strip_manual_quote_for_telegram(body), 'xmpp', 'telegram')} (ред.)"
            )
            text_discord = f"{display_sender}: {_convert_markup(body, 'xmpp', 'discord')}"

            if telegram_app and record.get("tg"):
                try:
                    await telegram_app.bot.edit_message_text(
                        chat_id=TELEGRAM_CHAT_ID, message_id=record["tg"], text=text_tg, parse_mode="HTML",
                    )
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.exception("Не удалось применить исправление в Telegram")
                except Exception:
                    logger.exception("Не удалось применить исправление в Telegram")
            if discord_bridge and record.get("discord"):
                try:
                    await discord_bridge.send_correction(record["discord"], text_discord)
                except Exception:
                    logger.exception("Не удалось применить исправление в Discord")
            return

        # --- обычное новое сообщение ---
        reply_id = msg["reply"]["id"] or None
        effective_body = body
        quote_matched_key = None

        # Пробуем распознать "ручную" текстовую цитату (так делает Monocles
        # Chat, а также мосты вроде matrixt2bot из Matrix) и сопоставить её
        # с недавно отправленными сообщениями по тексту. ВАЖНО: делаем это
        # независимо от наличия структурированного reply (Monocles иногда
        # добавляет и то, и другое разом) и НЕЗАВИСИМО от того, где именно
        # в сообщении находится цитата — она может идти и первой строкой,
        # и последней, и даже посередине (пользователь может ответить
        # "сверху и снизу одновременно"). _parse_manual_quote сам сканирует
        # все строки, поэтому здесь не нужно (и не надо) проверять
        # body.startswith(">") — раньше это было причиной того, что
        # привязка reply работала только для цитаты в самом начале.
        if ">" in body:
            quoted_text, remaining_text = _parse_manual_quote(body)
            if quoted_text:
                if not reply_id:
                    quote_matched_key = _find_key_by_quoted_text(quoted_text)
                # Даже если совпадение НЕ нашлось (например, цитата пришла
                # из Matrix через несколько "прыжков" и не совпадает ни с
                # чем в нашей памяти) — всё равно убираем "сырую" цитату
                # из видимого текста. Иначе на другие площадки улетает
                # разрастающийся ком вложенных "> > > >" при каждой
                # следующей связке ответов.
                effective_body = remaining_text

        # Ответ одним (или несколькими) эмодзи на пересланное сообщение —
        # считаем это реакцией, а не обычным ответом (полезно, если в
        # клиенте нет отдельной кнопки для реакций).
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

        # Пробуем реально скачать файл по OOB-ссылке и переслать как
        # настоящее вложение (фото/видео/голосовое/документ), а не
        # просто ссылкой — так пересланное с XMPP-стороны видно так же,
        # как обычное медиа.
        local_path = None
        if oob_url:
            try:
                local_path = await _download_url_to_file(oob_url)
            except Exception:
                logger.warning(
                    "Не удалось скачать файл по OOB-ссылке (%s) — пришлю просто ссылкой", oob_url
                )

        key = _new_bridge_record()
        client_id = msg["id"] or None
        canonical_id = client_id
        if XMPP_IS_MUC:
            # В MUC на сообщение принято ссылаться по stanza-id (XEP-0359),
            # назначенному самой комнатой, а не по клиентскому id отправителя.
            sid_el = msg["stanza_id"]
            if sid_el and str(sid_el["by"]) == XMPP_PEER_JID and sid_el["id"]:
                canonical_id = sid_el["id"]
        if canonical_id:
            peer_reply_jid = f"{XMPP_PEER_JID}/{sender}" if XMPP_IS_MUC else XMPP_PEER_JID
            _link_record(key, xmpp=canonical_id, xmpp_to=peer_reply_jid)
        if client_id and client_id != canonical_id:
            _xmpp_lookup[client_id] = key  # доп. ключ — на случай, если кто-то сошлётся по нему
        _remember_xmpp_text(key, body)  # для будущего сопоставления чужих текстовых цитат

        if local_path is not None:
            base = effective_body or ""
            if oob_url and base:
                # Некоторые клиенты (замечено на Monocles) дублируют ссылку
                # на файл ещё и текстом внутри тела сообщения, хотя она уже
                # есть в самом OOB-вложении — убираем дубликат, раз файл и
                # так пересылается как настоящее вложение.
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
                        logger.exception("Не удалось переслать медиа из XMPP в Telegram")
                if discord_bridge:
                    try:
                        discord_id = await discord_bridge.send_file(
                            local_path, caption=caption_discord, reply_to=reply_targets.get("discord")
                        )
                        _link_record(key, discord=discord_id)
                    except Exception:
                        logger.exception("Не удалось переслать медиа из XMPP в Discord")
            finally:
                local_path.unlink(missing_ok=True)
            return

        # --- нет вложения (или не удалось скачать) — обычный текст/ссылка ---
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
                logger.exception("Не удалось переслать сообщение из XMPP в Telegram")

        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_text(text_discord, reply_to=reply_targets.get("discord"))
                _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Не удалось переслать сообщение из XMPP в Discord")

    async def on_xmpp_reactions(self, msg):
        """Обрабатывает входящие реакции (XEP-0444) и отражает их на других площадках."""
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
        """Отправляет текст, возвращает КАНОНИЧЕСКИЙ id (для правок/реакций/reply).

        reply_to — id сообщения, на которое отвечаем (XEP-0461).
        reply_to_jid — полный JID автора этого сообщения (с ником-ресурсом
        в MUC) для атрибута 'to' у <reply> — без него некоторые клиенты
        (замечено на Monocles Chat) не признают reply валидным. Если не
        передан, используем JID комнаты/собеседника как раньше (может не
        сработать в MUC).
        В MUC дожидается эха от сервера с настоящим stanza-id, чтобы
        reply на это сообщение сразу работал корректно (см. _send_and_confirm).
        """
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=text, mtype=mtype)
        if reply_to:
            stanza["reply"]["id"] = reply_to
            stanza["reply"]["to"] = reply_to_jid or XMPP_PEER_JID
        return await self._send_and_confirm(stanza)

    async def send_correction(self, new_text: str, original_stanza_id: str) -> Optional[str]:
        """Отправляет исправление.

        ВАЖНОЕ НАБЛЮДЕНИЕ (проверено на практике): если retraction
        (XEP-0424) отработала, а следом шлём новое сообщение с
        <replace id="уже_удалённый_id">, Monocles Chat молча ОТБРАСЫВАЕТ
        его целиком — видимо, интерпретирует как "правка к несуществующему
        сообщению" и не показывает вообще. Поэтому правку теперь делаем
        так: удаляем старое сообщение, затем шлём новое ОБЫЧНЫМ
        сообщением, БЕЗ <replace> — тогда Monocles не пытается его с чем-то
        связывать и просто показывает как есть.

        Возвращает id новой стензы — вызывающий код должен обновить им
        связку, т.к. старый id теперь "мёртв" (сообщение удалено)."""
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        try:
            # Для правок не нужно ждать полный набор попыток (это заметно
            # задержит появление исправленного текста) — двух с короткой
            # паузой обычно достаточно.
            await self.send_retraction(original_stanza_id, attempts=2, delay=1.0)
        except Exception:
            logger.exception("Не удалось отправить retraction перед правкой в XMPP")

        # Текстовая цитата — по той же схеме, что и для обычных reply.
        # Структурированный <reply> ниже не гарантированно отрисовывается
        # (см. известные ограничения в README), а текст виден всегда.
        mbody = new_text
        key_for_quote = _xmpp_lookup.get(original_stanza_id)
        if key_for_quote:
            for k, text, _ts in reversed(_recent_xmpp_texts):
                if k == key_for_quote:
                    snippet = text.strip().replace("\n", " ")
                    if len(snippet) > 150:
                        snippet = snippet[:150].rstrip() + "…"
                    mbody = f"> {snippet}\n{new_text}"
                    break

        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=mbody, mtype=mtype)
        stanza.send()
        return stanza["id"]

    async def send_reaction(self, target_stanza_id: str, emojis: set) -> None:
        """Собираем стензу вручную (не через self["xep_0444"].send_reactions),
        чтобы точно проставить <store/> (XEP-0334) — это официальная
        ЗАВИСИМОСТЬ самого XEP-0444, и у сообщения с одними реакциями нет
        <body>, а мы уже знаем (по опыту с retraction), что сервер xmpp.jp
        не ретранслирует в комнату сообщения без body и без этой
        подсказки."""
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

        # ВРЕМЕННАЯ ДИАГНОСТИКА: проверяем реальный исходящий XML.
        try:
            logger.info("ДИАГНОСТИКА: отправляю reaction-стензу:\n%s", str(stanza))
        except Exception:
            pass

        stanza.send()

    async def send_retraction_reliable(self, target_stanza_id: str) -> None:
        """Настоящее удаление сообщений (не правок) — реализовано через тот
        же механизм, что и правки (send_correction), только БЕЗ отдельного
        retraction и без повторной отправки текста.

        Обнаружено на практике: сообщение с <replace id="X"> на уже
        отправленный id Monocles воспринимает как правку — но поскольку
        мы не посылаем никакого "настоящего" нового текста отдельно,
        а только саму правку, старое сообщение просто ПОЛНОСТЬЮ ПРОПАДАЕТ
        (не отображается вообще ни в каком виде). Для правок (см.
        send_correction) мы, наоборот, ПОСЛЕ этого шлём ещё и обычное
        сообщение с текстом — тут этого делать не нужно, раз цель именно
        удаление, а не показ нового текста."""
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        stanza = self.make_message(
            mto=XMPP_PEER_JID,
            mbody="/me retracted a previous message, but it's unsupported by your client.",
            mtype=mtype,
        )
        stanza["replace"]["id"] = target_stanza_id
        stanza.send()

    async def send_retraction(self, target_stanza_id: str, attempts: int = 3, delay: float = 2.0) -> None:
        """Отправляет удаление (retraction, XEP-0424) ранее отправленного
        сообщения. Собрано вручную (не через self["xep_0424"].send_retraction)
        и сверено построчно с официальным примером из самой спецификации
        (https://xmpp.org/extensions/xep-0424.html, раздел "Use Case") —
        порядок элементов, текст fallback-body (конвенция /me, XEP-0245) и
        атрибут for="urn:xmpp:message-retract:1" у <fallback> (обязателен
        по XEP-0428) сделаны так же, как в эталонном примере.
        Совпадает с тем, как это делает converse.js (сверено с их тестами).

        Отправляет НЕСКОЛЬКО раз с паузой — на практике одиночная попытка
        не всегда доходит с первого раза. Ограниченное число попыток —
        не бесконечный повтор.
        """
        from xml.etree import ElementTree as ET

        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        # Текст-заглушка нарочно общий, без содержимого удаляемого
        # сообщения (спецификация прямо требует этого в разделе Security
        # Considerations) — ровно текст из официального примера.
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
        """Входящее удаление сообщения от собеседника — пробрасываем на
        другие площадки."""
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
        """Запасной путь загрузки: сами запрашиваем слот (id) через
        XEP-0363, но PUT делаем своим aiohttp-запросом с "браузерными"
        заголовками — на случай, если сервер блокирует автоматические
        запросы без них (замечено на xmpp.jp для видео/gif)."""
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
            raise RuntimeError("не удалось определить сервис HTTP Upload")

        slot_iq = await upload_plugin.request_slot(domain, basename, size, content_type)
        slot = slot_iq["http_upload_slot"]
        put_url = slot["put"]["url"]
        get_url = slot["get"]["url"]

        headers = {}
        try:
            for h in slot["put"]["headers"]:
                headers[h["name"]] = h["value"]
        except Exception:
            pass  # не у всех серверов есть доп. заголовки в слоте — не критично
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
        """Загружает файл на XMPP-сервер (XEP-0363) и отправляет ссылку с OOB.

        Возвращает id отправленной станзы (для правок/реакций/reply),
        либо None, если файл вообще не удалось отправить.
        """

        async def _fallback_link_or_error(error_note: str) -> Optional[str]:
            # Раньше здесь можно было прислать прямую ссылку на исходный
            # файл (например, из Telegram) вместо текста ошибки — но
            # такая ссылка содержит токен бота в открытом виде, а канал
            # групповой — решили не рисковать безопасностью токена ради
            # красивого фолбэка.
            return await self.send_text(f"{caption} {error_note}".strip())

        try:
            url = await self["xep_0363"].upload_file(str(filepath))
        except UploadServiceNotFound:
            logger.warning("На XMPP-сервере не найден сервис HTTP Upload")
            return await _fallback_link_or_error("(сервер не поддерживает загрузку файлов)")
        except FileTooBig as e:
            logger.warning("Файл слишком большой для загрузки: %s", e)
            return await _fallback_link_or_error("(файл слишком большой для XMPP-сервера)")
        except HTTPError as e:
            # Некоторые серверы (замечено на xmpp.jp) отклоняют
            # автоматическую загрузку некоторых типов файлов (видео/gif)
            # с 405 — но при этом те же файлы нормально загружаются через
            # обычный XMPP-клиент вручную. Похоже, дело не в самом файле,
            # а в том, что PUT-запрос от slixmpp не похож на "настоящий"
            # браузерный запрос (нет привычных заголовков вроде
            # User-Agent). Пробуем залить вручную с такими заголовками.
            logger.warning("Сервер отклонил загрузку (%s), пробую вручную с браузерными заголовками", e)
            try:
                url = await self._manual_upload_with_browser_headers(filepath)
                logger.info("Обходной путь сработал — файл загружен вручную с браузерными заголовками")
            except Exception as manual_err:
                # Перепробовали и смену расширения, и браузерные заголовки, и
                # перекодирование в WebP (через Pillow/ffmpeg) — сервер
                # (xmpp.jp) одинаково отклоняет загрузку независимо от
                # формата и размера файла. Похоже на WAF с проверкой через
                # JavaScript-челлендж (та же история, что и с Rustix/
                # Pterodactyl для /mcstart) — такое принципиально не обойти
                # обычным HTTP-клиентом без настоящего браузера. Дальше не
                # пытаемся, просто сообщаем об ошибке.
                logger.warning("Ручная загрузка тоже не сработала (%s) — сервер отклоняет файл", manual_err)
                return await _fallback_link_or_error(f"(сервер отклонил загрузку файла: {e})")
        except IqError as e:
            logger.exception("Ошибка загрузки файла на XMPP-сервер")
            return await _fallback_link_or_error(f"(ошибка загрузки файла: {e})")
        except IqTimeout as e:
            logger.exception("Таймаут при загрузке файла на XMPP-сервер")
            return await _fallback_link_or_error(f"(таймаут при загрузке файла: {e})")
        except OSError as e:
            logger.exception("Сетевая ошибка при загрузке файла на XMPP-сервер")
            return await _fallback_link_or_error(f"(сетевая ошибка при загрузке файла: {e})")

        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        body = f"{caption}\n{url}".strip() if caption else url
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=body, mtype=mtype)
        stanza["oob"]["url"] = url
        if reply_to:
            stanza["reply"]["id"] = reply_to
            stanza["reply"]["to"] = reply_to_jid or XMPP_PEER_JID
        return await self._send_and_confirm(stanza)


# ========================================================================
# DISCORD-СТОРОНА
# ========================================================================

_DiscordBase = discord.Client if discord is not None else object


class DiscordBridge(_DiscordBase):
    """Discord-клиент, принимающий/отправляющий текст, файлы, правки, реакции."""

    def __init__(self):
        if discord is None:
            raise RuntimeError(
                "discord.py не установлен (pip install discord.py), а DISCORD_BOT_TOKEN задан"
            )
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.bridge_channel = None  # type: Optional["discord.abc.Messageable"]
        # Discord-шлюз иногда повторно доставляет событие о недавнем
        # сообщении при переподключении — без этого получались бы
        # дубликаты (особенно заметно на медиа).
        self._recent_message_ids: set = set()
        self._recent_message_ids_order: list = []

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
        channel = self.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(DISCORD_CHANNEL_ID)
            except Exception:
                logger.exception("Не удалось найти канал Discord с ID %s", DISCORD_CHANNEL_ID)
                return
        self.bridge_channel = channel
        logger.info(
            "Discord: подключился как %s, канал: #%s",
            self.user, getattr(channel, "name", DISCORD_CHANNEL_ID),
        )

    async def on_message(self, message: "discord.Message"):
        if self.user and message.author.id == self.user.id:
            return
        if message.channel.id != DISCORD_CHANNEL_ID:
            return
        if self._already_processed(message.id):
            logger.info("Discord прислал повторно уже обработанное сообщение %s — игнорирую", message.id)
            return

        author = _colored_author(f"discord:{message.author.id}", message.author.display_name)

        replied_id = message.reference.message_id if message.reference else None

        # Ответ одним эмодзи на пересланное сообщение — считаем реакцией.
        if replied_id and message.content and not message.attachments and _is_emoji_only(message.content):
            reaction_key = _get_reply_key("discord", replied_id)
            if reaction_key:
                await _mirror_reactions(reaction_key, _extract_emojis(message.content), skip="discord")
                return

        reply_targets = _get_reply_targets("discord", replied_id) if replied_id else {}

        # --- опрос (poll) ---
        poll = getattr(message, "poll", None)
        if poll is not None:
            try:
                question = poll.question.text if hasattr(poll.question, "text") else str(poll.question)
                lines = [f"📊 Опрос от {author}: {question}"]
                for i, answer in enumerate(poll.answers, start=1):
                    answer_text = answer.text if hasattr(answer, "text") else str(answer)
                    lines.append(f"{i}) {answer_text}")
                text = "\n".join(lines)
            except Exception:
                logger.exception("Не удалось разобрать опрос Discord, пересылаю как есть")
                text = f"{author}: [опрос — не удалось получить содержимое]"
            key = _new_bridge_record(discord=message.id)
            if xmpp_bridge:
                try:
                    xmpp_id = await xmpp_bridge.send_text(
                        text, reply_to=reply_targets.get("xmpp"), reply_to_jid=reply_targets.get("xmpp_to")
                    )
                    _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
                except Exception:
                    logger.exception("Не удалось переслать опрос из Discord в XMPP")
            if telegram_app:
                try:
                    sent = await telegram_app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID, text=text,
                        reply_to_message_id=reply_targets.get("tg"),
                    )
                    _link_record(key, tg=sent.message_id)
                except Exception:
                    logger.exception("Не удалось переслать опрос из Discord в Telegram")
            return

        # --- вложения ---
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
                logger.exception("Не удалось скачать вложение из Discord")
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
                        logger.exception("Не удалось переслать вложение из Discord в XMPP")
                if telegram_app:
                    try:
                        sent = await _tg_send_file(
                            local_path, caption_tg, reply_to=reply_targets.get("tg"), parse_mode="HTML"
                        )
                        if sent is not None:
                            _link_record(key, tg=sent.message_id)
                    except Exception:
                        logger.exception("Не удалось переслать вложение из Discord в Telegram")
            finally:
                local_path.unlink(missing_ok=True)

        # --- текст (только если не было вложений — иначе текст уже ушёл как подпись) ---
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
                logger.exception("Не удалось переслать сообщение из Discord в XMPP")

        if telegram_app:
            try:
                sent = await telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=tg_text, parse_mode="HTML",
                    reply_to_message_id=reply_targets.get("tg"),
                )
                _link_record(key, tg=sent.message_id)
            except Exception:
                logger.exception("Не удалось переслать сообщение из Discord в Telegram")

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
        xmpp_text = f"{author}: {_convert_markup(after.content, 'discord', 'xmpp')} (ред.)"
        tg_text = (
            f"{_html_escape(author, quote=False)}: "
            f"{_convert_markup(_strip_manual_quote_for_telegram(after.content), 'discord', 'telegram')} (ред.)"
        )

        if xmpp_bridge and record.get("xmpp"):
            try:
                new_xmpp_id = await xmpp_bridge.send_correction(xmpp_text, record["xmpp"])
                if new_xmpp_id:
                    _link_record(key, xmpp=new_xmpp_id)
            except Exception:
                logger.exception("Не удалось переслать исправление в XMPP")
        if telegram_app and record.get("tg"):
            try:
                await telegram_app.bot.edit_message_text(
                    chat_id=TELEGRAM_CHAT_ID, message_id=record["tg"], text=tg_text, parse_mode="HTML",
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    logger.exception("Не удалось применить исправление в Telegram")
            except Exception:
                logger.exception("Не удалось применить исправление в Telegram")

    async def on_raw_reaction_add(self, payload):
        await self._handle_reaction_change(payload)

    async def on_raw_reaction_remove(self, payload):
        await self._handle_reaction_change(payload)

    async def _handle_reaction_change(self, payload):
        if self.user and payload.user_id == self.user.id:
            return  # это наша же mirror-реакция
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
            logger.exception("Не удалось получить сообщение Discord для пересчёта реакций")
            return

        active = set()
        for r in message.reactions:
            human_count = r.count - (1 if r.me else 0)
            if human_count > 0:
                active.add(str(r.emoji))

        await _mirror_reactions(key, active, skip="discord")

    async def send_text(self, text: str, reply_to: Optional[int] = None) -> int:
        if self.bridge_channel is None:
            raise RuntimeError("Discord-канал ещё не готов")
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
            logger.exception("Не удалось применить исправление в Discord")

    async def delete_message(self, discord_message_id: int) -> None:
        if self.bridge_channel is None:
            return
        try:
            message = await self.bridge_channel.fetch_message(discord_message_id)
            await message.delete()
        except discord.errors.NotFound:
            # Уже удалено (например, другой путь обнаружения удаления
            # сработал параллельно чуть раньше) — не ошибка, просто гонка.
            pass
        except Exception:
            logger.exception("Не удалось удалить сообщение в Discord")

    async def send_file(self, filepath: Path, caption: str = "", reply_to: Optional[int] = None) -> int:
        if self.bridge_channel is None:
            raise RuntimeError("Discord-канал ещё не готов")
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
            logger.exception("Не удалось получить сообщение Discord для установки реакций")
            return

        current = {str(r.emoji) for r in message.reactions if r.me}
        for e in current - emojis:
            try:
                await message.remove_reaction(e, self.user)
            except Exception:
                logger.exception("Не удалось убрать реакцию %s в Discord", e)
        for e in emojis - current:
            try:
                await message.add_reaction(e)
            except Exception:
                logger.exception("Не удалось поставить реакцию %s в Discord", e)


# ========================================================================
# TELEGRAM-СТОРОНА
# ========================================================================

_MEDIA_ATTRS = ("photo", "video", "voice", "audio", "document", "sticker", "video_note", "animation")


async def _tg_send_file(
    local_path: Path, caption: str, reply_to: Optional[int] = None, parse_mode: Optional[str] = None
):
    """Отправляет файл в Telegram, подбирая подходящий метод по расширению.

    Возвращает отправленное telegram.Message (нужно для связки reply/правок).
    """
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
    """Команда /chatid — присылает ID текущего чата, группы или канала."""
    chat = update.effective_chat
    if chat is None:
        return
    title = chat.title or chat.first_name or "этот чат"
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"ID «{title}»: `{chat.id}`\n\n"
            f"Впишите это значение в переменную TELEGRAM_CHAT_ID в .env "
            f"и перезапустите бота, чтобы включить пересылку сообщений."
        ),
        parse_mode="Markdown",
    )
    logger.info("Команда /chatid: chat_id=%s (%s)", chat.id, title)



async def tg_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает входящие сообщения Telegram и пересылает их в XMPP/Discord."""
    msg = update.effective_message
    if msg is None:
        return
    # Технические пересылки-проверки (см. _tg_check_deletions_loop) — не
    # настоящие сообщения, пересылать их дальше не нужно.
    if msg.message_id in _verification_forward_ids:
        return
    # Игнорируем только СВОЁ ЖЕ эхо (не любых ботов вообще — иначе
    # ломается пересылка от сторонних ботов-мостов вроде matrixt2bot).
    if msg.from_user and msg.from_user.id == context.bot.id:
        return

    if msg.from_user:
        raw_author_name = msg.from_user.first_name
        author_key = f"tg:{msg.from_user.id}"
    elif msg.sender_chat:
        # Пост из привязанного канала (или анонимный админ, постящий "от
        # имени группы") приходит с sender_chat вместо from_user —
        # раньше в таких случаях просто подставлялась заглушка "Telegram".
        raw_author_name = msg.sender_chat.title or "Telegram"
        author_key = f"tg:channel:{msg.sender_chat.id}"
    else:
        raw_author_name = "Telegram"
        author_key = "tg:unknown"
    author = _colored_author(author_key, raw_author_name)
    has_media = any(getattr(msg, attr, None) for attr in _MEDIA_ATTRS)

    replied_id = msg.reply_to_message.message_id if msg.reply_to_message else None

    # Ответ одним эмодзи на пересланное сообщение — считаем реакцией.
    if replied_id and msg.text and not has_media and _is_emoji_only(msg.text):
        reaction_key = _get_reply_key("tg", replied_id)
        if reaction_key:
            await _mirror_reactions(reaction_key, _extract_emojis(msg.text), skip="tg")
            return

    reply_targets = _get_reply_targets("tg", replied_id) if replied_id else {}

    # --- опрос (poll) ---
    if msg.poll:
        lines = [f"📊 Опрос от {author}: {msg.poll.question}"]
        for i, option in enumerate(msg.poll.options, start=1):
            lines.append(f"{i}) {option.text}")
        if msg.poll.is_anonymous is False:
            lines.append("(опрос не анонимный)")
        text = "\n".join(lines)
        key = _new_bridge_record(tg=msg.message_id)
        if xmpp_bridge:
            try:
                xmpp_id = await xmpp_bridge.send_text(
                    text, reply_to=reply_targets.get("xmpp"), reply_to_jid=reply_targets.get("xmpp_to")
                )
                _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
            except Exception:
                logger.exception("Не удалось переслать опрос из Telegram в XMPP")
        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_text(text, reply_to=reply_targets.get("discord"))
                _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Не удалось переслать опрос из Telegram в Discord")
        return

    # --- чистый текст, без вложений ---
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
                logger.exception("Не удалось переслать сообщение из Telegram в XMPP")
        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_text(
                    f"{author}: {discord_body}", reply_to=reply_targets.get("discord")
                )
                _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Не удалось переслать сообщение из Telegram в Discord")
        return

    if not has_media:
        return  # сервисное сообщение и т.п.

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
            logger.warning("Файл превышает лимит Telegram Bot API: %s", e)
            notice = (
                f"{author}: [файл не переслан — превышает лимит Telegram Bot API "
                f"в {TELEGRAM_BOT_API_FILE_LIMIT_MB} МБ на скачивание ботом]"
            )
            # Раньше для этого уведомления не создавалась связка сообщений
            # вовсе — из-за этого нельзя было ни отреагировать, ни ответить
            # ни на исходное сообщение в Telegram, ни на само уведомление
            # на других площадках. Теперь создаём как обычно.
            key = _new_bridge_record(tg=msg.message_id)
            if xmpp_bridge:
                try:
                    xmpp_id = await xmpp_bridge.send_text(notice)
                    if xmpp_id:
                        _link_record(key, xmpp=xmpp_id, xmpp_to=_own_xmpp_reply_jid())
                except Exception:
                    logger.exception("Не удалось переслать уведомление о превышении лимита в XMPP")
            if discord_bridge:
                try:
                    discord_id = await discord_bridge.send_text(notice)
                    if discord_id:
                        _link_record(key, discord=discord_id)
                except Exception:
                    logger.exception("Не удалось переслать уведомление о превышении лимита в Discord")
        else:
            logger.exception("Ошибка Telegram при получении файла")
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
                logger.exception("Не удалось переслать медиа из Telegram в XMPP")
        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_file(
                    local_path, caption=caption_discord, reply_to=reply_targets.get("discord")
                )
                if discord_id:
                    _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Не удалось переслать медиа из Telegram в Discord")
    except Exception:
        logger.exception("Не удалось скачать медиа из Telegram")
    finally:
        try:
            local_path.unlink(missing_ok=True)
        except PermissionError:
            logger.warning(
                "Не удалось сразу удалить временный файл %s — он ещё занят, оставляю как есть",
                local_path,
            )


async def tg_handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пересылает редактирование текстового сообщения (в течение EDIT_WINDOW_SECONDS)."""
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
            "Отредактировано сообщение %s, но оно старше %s сек или не пересылалось — игнорирую",
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
            xmpp_text = f"{author}: {_format_tg_text(msg.text, msg.text_html, 'xmpp')} (ред.)"
            new_xmpp_id = await xmpp_bridge.send_correction(xmpp_text, record["xmpp"])
            if new_xmpp_id:
                _link_record(key, xmpp=new_xmpp_id)
        except Exception:
            logger.exception("Не удалось переслать исправление в XMPP")
    if discord_bridge and record.get("discord"):
        try:
            # Discord сам показывает "(edited)" при вызове .edit() — свою
            # пометку дописывать не нужно.
            discord_text = f"{author}: {_format_tg_text(msg.text, msg.text_html, 'discord')}"
            await discord_bridge.send_correction(record["discord"], discord_text)
        except Exception:
            logger.exception("Не удалось переслать исправление в Discord")


async def tg_handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пересылает реакцию пользователя на других площадках (бот ставит свою же)."""
    reaction = update.message_reaction
    if reaction is None:
        return

    _prune_bridge_records()
    key = _tg_lookup.get(reaction.message_id)
    if not key:
        return

    emojis = {r.emoji for r in reaction.new_reaction if isinstance(r, ReactionTypeEmoji)}
    await _mirror_reactions(key, emojis, skip="tg")


# ========================================================================
# HTTP HEALTH-CHECK (для хостингов вроде Render, которым нужен открытый порт)
# ========================================================================

async def _start_healthcheck_server():
    port_raw = os.environ.get("PORT")
    if not port_raw:
        return None

    from aiohttp import web

    async def _health(request):
        status = "мост активен" if BRIDGE_ENABLED else "бот работает в режиме /chatid"
        return web.Response(text=f"OK — {status}")

    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port_raw))
    await site.start()
    logger.info("HTTP health-check сервер слушает порт %s (для Render/аналогов)", port_raw)
    return runner


# ============================================================================
# ПРОВЕРКА УДАЛЁННЫХ СООБЩЕНИЙ TELEGRAM — НЕОБЯЗАТЕЛЬНЫЙ БЛОК.
# Telegram Bot API НЕ присылает ботам событие "сообщение удалено" — это
# жёсткое ограничение платформы, обойти нельзя. Единственный известный
# обходной путь — периодически пробовать безобидную "правку" сообщения
# (снятие несуществующей инлайн-клавиатуры): если сообщение удалено,
# Telegram ответит ошибкой "message to edit not found"; если нет —
# либо тихо подтвердит, либо скажет "message is not modified" (тоже
# значит "жив"). Проверяем только сообщения, которые уже отслеживает
# мост (не более TELEGRAM_DELETE_CHECK_BATCH штук за раз, реже одного
# раза в TELEGRAM_DELETE_CHECK_INTERVAL секунд — чтобы не долбить API).
#
# ВАЖНО: теперь, когда есть MTProto-userbot (см. TELEGRAM_USERBOT_ENABLED)
# для мгновенного обнаружения удалений, эта проверка через Bot API стала
# просто медленной подстраховкой на случай, если MTProto-сессия
# отключится — поэтому специально сделана НЕАГРЕССИВНОЙ, чтобы не
# отъедать лимит запросов у обычной пересылки сообщений (см. flood
# control — раньше слишком частая/большая проверка реально роняла
# доставку обычных сообщений).
#
# Чтобы отключить — удалите/закомментируйте вызов
# asyncio.create_task(_tg_check_deletions_loop()) в main().

TELEGRAM_DELETE_CHECK_INTERVAL = int(os.environ.get("TELEGRAM_DELETE_CHECK_INTERVAL", "300"))
TELEGRAM_DELETE_CHECK_BATCH = int(os.environ.get("TELEGRAM_DELETE_CHECK_BATCH", "30"))
# Пауза между отдельными проверками внутри одного прохода — весь проход
# должен укладываться в разумное время, не съедая весь интервал между
# проходами и не отбирая лимит запросов у обычной пересылки сообщений.
TELEGRAM_DELETE_CHECK_DELAY = float(os.environ.get("TELEGRAM_DELETE_CHECK_DELAY", "1.0"))

# id технических пересылок-проверок (см. "message can't be edited" ниже) —
# чтобы обычный обработчик сообщений не переслал их дальше как настоящие.
_verification_forward_ids: set = set()


async def _forget_verification_id_later(message_id: int, delay: float = 30.0) -> None:
    await asyncio.sleep(delay)
    _verification_forward_ids.discard(message_id)


# Сообщения, которые бот не может редактировать (написаны не самим ботом —
# то есть настоящими людьми, а не переслано ботом с других площадок).
# Для них editMessageReplyMarkup ВСЕГДА вернёт "message can't be edited" —
# незачем каждый раз пытаться и проваливаться в запасной способ (пересылка
# + удаление), это заметно мигает в чате. Проверяем такие сообщения
# намного реже, отдельным более редким циклом.
_non_editable_tg_messages: set = set()
_last_forward_check: dict = {}  # message_id -> time.monotonic() последней проверки пересылкой
TELEGRAM_FORWARD_CHECK_MIN_INTERVAL = float(
    os.environ.get("TELEGRAM_FORWARD_CHECK_MIN_INTERVAL", str(12 * 3600))
)
# Полностью выключает проверку "чужих" сообщений через пересылку+удаление
# (та самая, что заметно мигает в чате) — например, пока настраиваете
# MTProto-userbot и хотите временно избежать лишнего шума/мигания.
# Обычная (дешёвая) проверка своих же сообщений бота продолжает работать.
TELEGRAM_FORWARD_CHECK_ENABLED = (
    os.environ.get("TELEGRAM_FORWARD_CHECK_ENABLED", "true").strip().lower() == "true"
)


async def _tg_check_deletions_loop() -> None:
    logger.info(
        "Проверка удалений Telegram запущена (каждые %s сек, до %s сообщений за проход)",
        TELEGRAM_DELETE_CHECK_INTERVAL, TELEGRAM_DELETE_CHECK_BATCH,
    )
    # Telegram может вернуть разные формулировки для удалённого сообщения
    # в зависимости от ситуации — проверяем известные варианты сразу.
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

            # --- "чужие" сообщения (написаны не самим ботом) — не можем
            # редактировать в принципе, проверяем только через пересылку,
            # и не чаще, чем раз в TELEGRAM_FORWARD_CHECK_MIN_INTERVAL сек
            # (иначе получается заметное мигание в чате на каждом проходе).
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
                            "Не удалось удалить временную копию (id %s) после проверки пересылкой",
                            forwarded.message_id,
                        )
                    asyncio.create_task(_forget_verification_id_later(forwarded.message_id))
                except BadRequest as fe:
                    if any(marker in str(fe).lower() for marker in deleted_markers):
                        logger.info(
                            "Обнаружено удаление сообщения Telegram %s (через пересылку) — "
                            "удаляю на других площадках", tg_id,
                        )
                        await _propagate_deletion(record, skip="tg")
                        _forget_record_by_key(key)
                        _non_editable_tg_messages.discard(tg_id)
                        _last_forward_check.pop(tg_id, None)
                except Exception:
                    pass
                await asyncio.sleep(TELEGRAM_DELETE_CHECK_DELAY)
                continue

            # --- обычная (дешёвая) проверка через editMessageReplyMarkup ---
            try:
                # Безобидно: снимаем клавиатуру, которой и так нет.
                await telegram_app.bot.edit_message_reply_markup(
                    chat_id=TELEGRAM_CHAT_ID, message_id=tg_id, reply_markup=None
                )
            except BadRequest as e:
                error_text = str(e).lower()
                if any(marker in error_text for marker in deleted_markers):
                    logger.info(
                        "Обнаружено удаление сообщения Telegram %s — удаляю на других площадках", tg_id
                    )
                    await _propagate_deletion(record, skip="tg")
                    _forget_record_by_key(key)
                elif "message can't be edited" in error_text:
                    # Сообщение написано не ботом — запоминаем и дальше
                    # проверяем через пересылку, но редко (см. выше).
                    # Важно: сразу проставляем "последнюю проверку" сейчас,
                    # а не ждём следующего цикла — иначе первая пересылка-
                    # проверка срабатывала бы уже через ~10 сек после
                    # обнаружения, а не через полный интервал (заметное
                    # мигание сразу после каждого нового сообщения).
                    _non_editable_tg_messages.add(tg_id)
                    _last_forward_check[tg_id] = time.monotonic()
                elif "message is not modified" not in error_text:
                    # Неизвестный вариант ошибки — не тот, что мы уже знаем
                    # как "сообщение живо", и не как "сообщение удалено".
                    # Логируем, чтобы в следующий раз не гадать вслепую.
                    logger.info(
                        "Проверка удаления Telegram %s: неопознанный BadRequest: %s", tg_id, e
                    )
            except Exception as e:
                logger.info("Проверка удаления Telegram %s: прочая ошибка %s: %s", tg_id, type(e).__name__, e)
            await asyncio.sleep(TELEGRAM_DELETE_CHECK_DELAY)


# ============================================================================
# TELEGRAM USERBOT (MTProto/Telethon) — НЕОБЯЗАТЕЛЬНЫЙ БЛОК.
# Мгновенное обнаружение удаления ЛЮБЫХ сообщений в чате (не только своих)
# через полноценный вход в отдельный Telegram-аккаунт — см. комментарий у
# TELEGRAM_USERBOT_ENABLED в начале файла и generate_session.py рядом.
# Работает только для супергрупп (обычные "маленькие" группы Telegram не
# используют единую нумерацию сообщений, как супергруппы и каналы — и в
# любом случае для нашего моста нужна именно супергруппа, чтобы у
# сообщений был стабильный числовой id).

_telegram_userbot = None


def _tg_chat_ids_match(a: int, b: int) -> bool:
    """Сравнивает id чата с поправкой на то, что Telethon и Bot API иногда
    представляют id супергрупп по-разному (с "-100" в начале или без)."""

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
            "TELEGRAM_USERBOT_SESSION задан, но telethon не установлен "
            "(pip install telethon) — мгновенное обнаружение удалений отключено"
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
                return  # Telegram не всегда указывает чат при удалении — ничего не поделать
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
                    "MTProto: мгновенно обнаружено удаление сообщения Telegram %s — "
                    "удаляю на других площадках", mid,
                )
                await _propagate_deletion(record, skip="tg")
                _forget_record_by_key(key)

        me = await client.get_me()
        logger.info(
            "MTProto-аккаунт подключён (%s) — удаления в Telegram теперь обнаруживаются мгновенно",
            getattr(me, "username", None) or getattr(me, "phone", "?"),
        )
    except Exception:
        logger.exception("Не удалось подключить MTProto-аккаунт для отслеживания удалений")


async def _stop_telegram_userbot() -> None:
    if _telegram_userbot is not None:
        try:
            await _telegram_userbot.disconnect()
        except Exception:
            pass


# ========================================================================
# ЗАПУСК
# ========================================================================

async def main() -> None:
    global telegram_app, xmpp_bridge, discord_bridge

    await _init_db()
    await _load_from_db()

    healthcheck_runner = await _start_healthcheck_server()
    await _start_telegram_userbot()

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN).rate_limiter(AIORateLimiter(max_retries=3))

    if TELEGRAM_FORCE_IPV4:
        import httpx

        ipv4_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        forced_request = HTTPXRequest(httpx_kwargs={"transport": ipv4_transport})
        builder = builder.request(forced_request).get_updates_request(forced_request)
        logger.info("Принудительно использую IPv4 для запросов к Telegram (TELEGRAM_FORCE_IPV4=true)")

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
            xmpp_bridge.connect()  # соединение устанавливается асинхронно на текущем event loop

        if DISCORD_ENABLED:
            if discord is None:
                logger.warning(
                    "DISCORD_BOT_TOKEN задан, но discord.py не установлен "
                    "(pip install discord.py) — Discord-сторона моста отключена"
                )
            else:
                discord_bridge = DiscordBridge()
                asyncio.create_task(discord_bridge.start(DISCORD_BOT_TOKEN))

        # Проверка удалений Telegram — необязательный блок, см. комментарий
        # у _tg_check_deletions_loop(). Уберите строку ниже, чтобы отключить.
        asyncio.create_task(_tg_check_deletions_loop())
    else:
        missing = []
        if not TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        if not XMPP_ENABLED and not DISCORD_ENABLED:
            missing.append("(XMPP_JID+XMPP_PASSWORD+XMPP_PEER_JID) или (DISCORD_BOT_TOKEN+DISCORD_CHANNEL_ID)")
        logger.warning(
            "Мост НЕ активен — не заданы: %s. Бот работает только в режиме /chatid.",
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
                "Telegram-бот запущен, жду сообщений в чате %s. Цели моста: %s",
                TELEGRAM_CHAT_ID, ", ".join(targets),
            )
        else:
            logger.info("Telegram-бот запущен в режиме /chatid (напишите /chatid в нужном чате)")

        try:
            await asyncio.Event().wait()  # держим процесс живым до Ctrl+C
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            logger.info("Останавливаюсь...")
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
