"""
Telegram <-> XMPP <-> Discord мост (bridge)
=============================================

Трёхсторонний мост: сообщение из любой из трёх площадок (Telegram-чат,
XMPP-собеседник/комната, Discord-канал) пересылается в две остальные.
Работает и в "двустороннем" режиме — просто не настраивайте XMPP или
Discord, и мост будет работать для оставшейся пары.

АРХИТЕКТУРА
-----------
- Telegram: python-telegram-bot (v20+, асинхронный, long polling).
- XMPP: slixmpp (нативно асинхронный, на asyncio).
- Discord: discord.py (нативно асинхронный, на asyncio).
Все три работают в одном asyncio event loop.

ЧТО ПЕРЕСЫЛАЕТСЯ
-----------------
- Текст, фото, видео, гифки, голосовые, аудио, документы, стикеры
  (статические стикеры конвертируются в PNG).
- Редактирование текстовых сообщений в течение EDIT_WINDOW_SECONDS
  после отправки (по умолчанию 5 минут) — через XEP-0308 для XMPP,
  editMessageText для Telegram, message.edit() для Discord.
- Реакции на сообщения (👍❤️ и т.д.) между всеми тремя площадками —
  через XEP-0444 для XMPP, setMessageReaction для Telegram (бот
  должен быть админом чата, чтобы ПОЛУЧАТЬ реакции от пользователей),
  message.add_reaction()/remove_reaction() для Discord.
  ВАЖНО: реакция технически не может "принадлежать" другому
  пользователю на чужой платформе — бот сам ставит соответствующую
  реакцию от своего имени на сообщении на других площадках, как
  визуальное отражение того, что кто-то отреагировал.
- Настоящие ответы (reply) на сообщения — если ответить на пересланное
  сообщение на любой площадке, ответ придёт на двух других площадках
  тоже как настоящий reply (XEP-0461 для XMPP, reply_to_message_id
  для Telegram, MessageReference для Discord), а не просто текстом с
  цитатой.
- Ответ ОДНИМ эмодзи (или несколькими) на пересланное сообщение
  считается реакцией, а не обычным ответом — удобно, если в клиенте
  нет отдельной кнопки для реакций (например, в некоторых версиях
  Monocles Chat поддержка XEP-0444 ограничена).
- Для групповых XMPP-чатов (MUC) reply/реакции корректно используют
  stanza-id (XEP-0359), назначенный самой комнатой, а не клиентский id
  — так, как того требует спецификация. Без этого reply/реакции,
  присланные из клиентов вроде Monocles Chat, не находили нужное
  сообщение и пересылались как обычный текст с цитатой.

УСТАНОВКА
---------
    pip install -r requirements.txt

НАСТРОЙКА
---------
Все параметры — через переменные окружения (см. .env.example).
Каждая из площадок (XMPP, Discord) настраивается независимо — мост
работает и с одной из них, и с обеими сразу.

ОГРАНИЧЕНИЯ ЭТОЙ ВЕРСИИ
------------------------
- Мост работает только с ОДНИМ Telegram-чатом, ОДНИМ XMPP-собеседником
  (или комнатой) и ОДНИМ Discord-каналом одновременно. Для нескольких
  наборов — запускайте несколько экземпляров с разными переменными.
- Файлы > 20 МБ Telegram Bot API не отдаёт ботам (ограничение самого
  Telegram) — при превышении лимита бот пишет об этом текстом.
- Редактирование (правка текста) поддерживается только для обычного
  текста — не для подписей к медиа и не для самих файлов. Реакции же
  теперь работают и на медиа-сообщениях тоже (фото, видео, кружки,
  голосовые, документы, стикеры).
- Реакция "от нескольких разных людей одновременно" не агрегируется
  идеально: если на исходной площадке реагируют несколько человек
  разными эмодзи, на других площадках отражается только последнее
  изменение (не сумма всех реакций).
- Reply на XMPP-сторону детектируется только если клиент отправителя
  реализует структурированный XEP-0461 (`<reply id="..."/>`). Судя по
  всему, Monocles Chat пока просто визуально имитирует цитату текстом
  в теле сообщения, без структурированной пометки. Для этого случая
  добавлена ЭКСПЕРИМЕНТАЛЬНАЯ эвристика: бот распознаёт вставленную
  текстом цитату ("> текст\n\nответ") и пытается сопоставить её с
  недавно отправленными в комнату сообщениями по содержимому текста.
  Работает не идеально — может ошибиться на очень похожих или совсем
  коротких сообщениях, и не сработает, если исходный текст сильно
  обрезан клиентом при цитировании.
- Ответы (reply) работают, только если исходное сообщение (на которое
  отвечают) было переслано мостом, то есть связка для него ещё жива
  в памяти (см. MESSAGE_MAP_TTL_SECONDS ниже). Ответ на что-то более
  старое или отправленное до запуска бота придёт как обычное
  сообщение, без привязки к реплаю.
- Вся связка сообщений между площадками хранится в памяти процесса и
  теряется при перезапуске бота (старые сообщения после перезапуска
  уже нельзя отредактировать/реагировать через мост).
- Опросы (Telegram/Discord polls) пересылаются как текстовый снимок
  вопроса и вариантов на момент создания — не как настоящий
  интерактивный опрос, и результаты голосования не обновляются.
- Форматирование текста Telegram (жирный, курсив, зачёркнутый, код,
  ссылки, спойлер) конвертируется в разметку Discord и в XEP-0393
  Message Styling для XMPP. Подчёркивание и спойлеры не имеют аналога
  в XEP-0393 — при пересылке в XMPP эти два вида просто теряют
  оформление (текст остаётся, разметка — нет). Форматирование теперь
  конвертируется в обе стороны между всеми тремя площадками (включая
  Discord-markdown и XEP-0393 из Monocles Chat), хотя эта часть менее
  строго проверена, чем разбор Telegram (там точные данные от Bot API,
  здесь — эвристика по регуляркам).
- Файлы с XMPP-стороны (в т.ч. голосовые) скачиваются и пересылаются
  как настоящие вложения; если скачать по ссылке не удалось (например,
  сервер требует авторизацию) — пересылается просто ссылкой.
- У каждого автора (по стабильному id — Telegram user id, Discord user
  id или XMPP-нику) появляется свой цветной значок из набора
  🔴🟠🟡🟢🔵🟣⚫️⚪️🟤🔺🔻🔸🔹🔶🔷🔳🔲🟥🟧🟨🟩🟦🟪⬛️⬜️🟫, который
  ставится перед именем при пересылке на другие площадки — так легче
  различать, кто пишет, на площадках без аватарок/цветных ников.
- Reply из Telegram/Discord корректно формируется по спецификации
  XEP-0461 (проверено сверкой сырого XML), но не отображается как
  визуальный reply ВНУТРИ самого Monocles Chat — похоже на ограничение
  конкретно этого клиента в части отрисовки входящих reply от других
  участников комнаты (составление и приём reply от Monocles к другим
  площадкам работает нормально). Как запасной вариант, рядом со
  структурированным reply в XMPP теперь дублируется текстовая цитата
  ("> текст оригинала\nАвтор: ответ") — её увидит любой клиент,
  независимо от поддержки XEP-0461.
"""

import asyncio
import itertools
import logging
import os
import re
import shutil
import tempfile
import time
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

# TELEGRAM_CHAT_ID можно не указывать при первом запуске: бот всё равно
# стартует и по команде /chatid пришлёт ID текущего чата.
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

# ============================================================================
# MINECRAFT (Rustix.me / Pterodactyl) — НЕОБЯЗАТЕЛЬНЫЙ БЛОК.
# Чтобы полностью отключить фичу — закомментируйте эти 4 строки (или
# просто не задавайте MINECRAFT_API_KEY в .env, тогда команда /mcstart
# сама не зарегистрируется).
MINECRAFT_PANEL_URL = os.environ.get("MINECRAFT_PANEL_URL", "https://my.rustix.me").strip().rstrip("/")
MINECRAFT_API_KEY = os.environ.get("MINECRAFT_API_KEY", "").strip() or None
MINECRAFT_SERVER_ID = os.environ.get("MINECRAFT_SERVER_ID", "").strip() or None
MINECRAFT_ENABLED = bool(MINECRAFT_API_KEY and MINECRAFT_SERVER_ID)
# ============================================================================

# Некоторые VPN туннелируют только IPv4, а httpx/anyio по умолчанию
# сначала пробует IPv6 и подолгу "виснет" — из-за этого TimedOut даже
# при рабочем интернете. TELEGRAM_FORCE_IPV4=true чинит это.
TELEGRAM_FORCE_IPV4 = os.environ.get("TELEGRAM_FORCE_IPV4", "false").strip().lower() == "true"

# Сколько секунд после отправки сообщение можно ещё отредактировать и
# получить исправление на других площадках.
EDIT_WINDOW_SECONDS = int(os.environ.get("EDIT_WINDOW_SECONDS", "300"))

# Как долго вообще помнить связку сообщений между площадками (нужно
# для того, чтобы реакции могли долетать и позже, чем EDIT_WINDOW_SECONDS
# после отправки). По умолчанию 6 часов.
MESSAGE_MAP_TTL_SECONDS = int(os.environ.get("MESSAGE_MAP_TTL_SECONDS", str(6 * 3600)))

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

_bridge_key_counter = itertools.count(1)
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


def _mc_media_label(suffix: str) -> str:
    """Короткая текстовая метка типа файла для уведомления в Minecraft-чате
    (без эмодзи — игровой шрифт их не рисует)."""
    suffix = (suffix or "").lower()
    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return "[фото]"
    if suffix in (".mp4", ".mov", ".webm"):
        return "[видео]"
    if suffix in (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".opus"):
        return "[аудио]"
    if suffix in (".tgs",):
        return "[анимированный стикер]"
    return "[файл]"


def _own_xmpp_reply_jid() -> str:
    """JID, который надо указывать в 'to' у <reply>/<reactions> для
    сообщений, которые в комнату/чат отправил сам бот (не важно, чей
    текст внутри — TG или Discord автора)."""
    if XMPP_IS_MUC:
        return f"{XMPP_PEER_JID}/{XMPP_MUC_NICK}"
    return XMPP_PEER_JID


def _new_bridge_record(**ids) -> str:
    key = str(next(_bridge_key_counter))
    record = {"tg": None, "xmpp": None, "discord": None, "xmpp_to": None, "ts": time.monotonic()}
    record.update({k: v for k, v in ids.items() if v is not None})
    _bridge_records[key] = record
    _link_lookup(key, record)
    return key


def _link_record(key: str, **ids) -> None:
    record = _bridge_records.get(key)
    if not record:
        return
    for k, v in ids.items():
        if v is not None:
            record[k] = v
    _link_lookup(key, record)


def _link_lookup(key: str, record: dict) -> None:
    if record.get("tg") is not None:
        _tg_lookup[record["tg"]] = key
    if record.get("xmpp") is not None:
        _xmpp_lookup[record["xmpp"]] = key
    if record.get("discord") is not None:
        _discord_lookup[record["discord"]] = key


def _prune_bridge_records() -> None:
    now = time.monotonic()
    stale = [k for k, v in _bridge_records.items() if now - v["ts"] > MESSAGE_MAP_TTL_SECONDS]
    for k in stale:
        record = _bridge_records.pop(k, None)
        if not record:
            continue
        if record.get("tg") is not None:
            _tg_lookup.pop(record["tg"], None)
        if record.get("xmpp") is not None:
            _xmpp_lookup.pop(record["xmpp"], None)
        if record.get("discord") is not None:
            _discord_lookup.pop(record["discord"], None)


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
        except Exception:
            logger.exception("Не удалось удалить сообщение в Telegram")

    if skip != "xmpp" and xmpp_bridge and record.get("xmpp"):
        try:
            # Чистый retraction (XEP-0424) без текстовой заглушки — клиенты
            # с поддержкой удалят сообщение молча. Раньше здесь была правка
            # на текст "[сообщение удалено]", но она оставляла в чате
            # видимый мусор вместо реального исчезновения сообщения.
            await xmpp_bridge.send_retraction(record["xmpp"])
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
    _recent_xmpp_texts.append((key, text, time.monotonic()))
    if len(_recent_xmpp_texts) > 200:
        del _recent_xmpp_texts[: len(_recent_xmpp_texts) - 200]


def _find_key_by_quoted_text(quoted: str) -> Optional[str]:
    quoted = (quoted or "").strip()
    if not quoted:
        return None
    now = time.monotonic()
    for key, text, ts in reversed(_recent_xmpp_texts):
        if now - ts > MESSAGE_MAP_TTL_SECONDS:
            continue
        if key not in _bridge_records:
            continue
        if quoted in text or text in quoted:
            return key
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
            self._learn_own_stanza_id(msg)
            return
        if not self._is_from_peer(msg):
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
            if not record or time.monotonic() - record["ts"] > EDIT_WINDOW_SECONDS:
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
                f"{_convert_markup(body, 'xmpp', 'telegram')} (ред.)"
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

        # Структурированного XEP-0461 reply нет — пробуем распознать
        # "ручную" текстовую цитату (так делает Monocles Chat) и
        # сопоставить её с недавно отправленными сообщениями по тексту.
        if not reply_id and body.startswith(">"):
            quoted_text, remaining_text = _parse_manual_quote(body)
            if quoted_text:
                quote_matched_key = _find_key_by_quoted_text(quoted_text)
                if quote_matched_key:
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
            base = effective_body if effective_body and effective_body != oob_url else ""
            caption_tg = (
                f"{_html_escape(display_sender, quote=False)}: {_convert_markup(base, 'xmpp', 'telegram')}"
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
                if MC_CHAT_BRIDGE_ENABLED:
                    label = _mc_media_label(local_path.suffix)
                    caption_part = f" {base}" if base else ""
                    await _send_to_minecraft_chat(f"{sender}: {label}{caption_part}")
            finally:
                local_path.unlink(missing_ok=True)
            return

        # --- нет вложения (или не удалось скачать) — обычный текст/ссылка ---
        if oob_url and not effective_body:
            text_tg = f"{_html_escape(display_sender, quote=False)}: {_html_escape(oob_url, quote=False)}"
            text_discord = f"{display_sender}: {oob_url}"
        else:
            converted_tg = _convert_markup(effective_body, "xmpp", "telegram")
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

        if MC_CHAT_BRIDGE_ENABLED:
            mc_text = effective_body if effective_body else (oob_url or "")
            if mc_text:
                await _send_to_minecraft_chat(f"{sender}: {mc_text}")

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
        """Отправляет исправление. Многие клиенты (похоже, включая Monocles
        Chat) не понимают XEP-0308 и по спецификации обязаны показать
        стензу с <replace> просто как обычное новое сообщение — отсюда
        видимые "дубликаты". Раз retraction (XEP-0424) у нас уже точно
        работает и что-то показывает — сначала удаляем старое сообщение,
        затем шлём новое с текстом (оно всё равно содержит <replace> —
        клиенты, которые его понимают, всё равно отредактируют на месте,
        не заметив предшествующий retract). Возвращает id новой стензы —
        вызывающий код должен обновить им связку, т.к. старый id теперь
        "мёртв" (сообщение удалено)."""
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        try:
            # include_fallback=False — иначе slixmpp добавляет в стензу
            # текстовый "fallback body" ("This message has been retracted
            # by the sender"), который клиенты показывают отдельной
            # надписью. Нам это не нужно — удаление должно быть тихим.
            self["xep_0424"].send_retraction(
                JID(XMPP_PEER_JID), original_stanza_id, mtype=mtype, include_fallback=False
            )
        except Exception:
            logger.exception("Не удалось отправить retraction перед правкой в XMPP")
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=new_text, mtype=mtype)
        stanza["replace"]["id"] = original_stanza_id
        stanza.send()
        return stanza["id"]

    async def send_reaction(self, target_stanza_id: str, emojis: set) -> None:
        await self["xep_0444"].send_reactions(JID(XMPP_PEER_JID), target_stanza_id, emojis)

    async def send_retraction(self, target_stanza_id: str) -> None:
        """Отправляет удаление (retraction, XEP-0424) ранее отправленного
        сообщения — тихо, без текстовой надписи-заглушки для клиентов,
        не понимающих retraction (include_fallback=False).
        send_retraction в slixmpp синхронный (сам вызывает send())."""
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        self["xep_0424"].send_retraction(
            JID(XMPP_PEER_JID), target_stanza_id, mtype=mtype, include_fallback=False
        )

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
        try:
            url = await self["xep_0363"].upload_file(str(filepath))
        except UploadServiceNotFound:
            logger.warning("На XMPP-сервере не найден сервис HTTP Upload")
            return await self.send_text(f"{caption} (сервер не поддерживает загрузку файлов)".strip())
        except FileTooBig as e:
            logger.warning("Файл слишком большой для загрузки: %s", e)
            return await self.send_text(f"{caption} (файл слишком большой для XMPP-сервера)".strip())
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
            except Exception:
                logger.exception("Обходной путь тоже не сработал — сервер отклоняет файл")
                return await self.send_text(f"{caption} (сервер отклонил загрузку файла: {e})".strip())
        except IqError as e:
            logger.exception("Ошибка загрузки файла на XMPP-сервер")
            return await self.send_text(f"{caption} (ошибка загрузки файла: {e})".strip())
        except IqTimeout as e:
            logger.exception("Таймаут при загрузке файла на XMPP-сервер")
            return await self.send_text(f"{caption} (таймаут при загрузке файла: {e})".strip())
        except OSError as e:
            logger.exception("Сетевая ошибка при загрузке файла на XMPP-сервер")
            return await self.send_text(f"{caption} (сетевая ошибка при загрузке файла: {e})".strip())

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
                    f"{_convert_markup(message.content, 'discord', 'telegram')}"
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
                if MC_CHAT_BRIDGE_ENABLED:
                    label = _mc_media_label(local_path.suffix)
                    caption_part = f" {message.content}" if message.content else ""
                    await _send_to_minecraft_chat(f"{message.author.display_name}: {label}{caption_part}")
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
        tg_text = f"{_html_escape(author, quote=False)}: {_convert_markup(message.content, 'discord', 'telegram')}"
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

        if MC_CHAT_BRIDGE_ENABLED:
            await _send_to_minecraft_chat(f"{message.author.display_name}: {message.content}")

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
        if not record or time.monotonic() - record["ts"] > EDIT_WINDOW_SECONDS:
            return

        author = _colored_author(f"discord:{after.author.id}", after.author.display_name)
        xmpp_text = f"{author}: {_convert_markup(after.content, 'discord', 'xmpp')} (ред.)"
        tg_text = (
            f"{_html_escape(author, quote=False)}: "
            f"{_convert_markup(after.content, 'discord', 'telegram')} (ред.)"
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


# ============================================================================
# MINECRAFT (Rustix.me / Pterodactyl) — НЕОБЯЗАТЕЛЬНЫЙ БЛОК.
# Чтобы отключить — удалите/закомментируйте вызов add_handler в main()
# (см. поиск по "mc_start_command") или просто не задавайте
# MINECRAFT_API_KEY в .env — тогда команда сама не зарегистрируется.

async def _minecraft_power_action(signal: str) -> tuple:
    """Отправляет команду power-действия (start/stop/restart/kill) через
    официальный Client API Pterodactyl. Возвращает (успех: bool, текст).
    """
    import aiohttp

    url = f"{MINECRAFT_PANEL_URL}/api/client/servers/{MINECRAFT_SERVER_ID}/power"
    headers = {
        "Authorization": f"Bearer {MINECRAFT_API_KEY}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
        "Content-Type": "application/json",
        # Некоторые панели стоят за CDN/WAF (замечено на Rustix.me —
        # cdn.mitelis.net), который блокирует запросы без "браузерных"
        # заголовков — aiohttp по умолчанию шлёт User-Agent вида
        # "Python/3.x aiohttp/x.x", что легко детектится как бот.
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin": MINECRAFT_PANEL_URL,
        "Referer": f"{MINECRAFT_PANEL_URL}/server/{MINECRAFT_SERVER_ID}/console",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json={"signal": signal}) as resp:
                body = await resp.text()
                logger.info(
                    "Minecraft power-action ответ: HTTP %s, тело (первые 300 симв.): %s",
                    resp.status, body[:300],
                )
                if resp.status in (200, 204):
                    return True, "команда отправлена"
                clean_body = re.sub(r"<[^>]+>", "", body).strip()
                return False, f"HTTP {resp.status}: {clean_body[:300]}"
    except Exception as e:
        logger.exception("Ошибка запроса к панели Minecraft (Rustix/Pterodactyl)")
        return False, str(e)


async def _minecraft_get_status() -> tuple:
    """Запрашивает текущее состояние сервера (для диагностики /mcstart)."""
    import aiohttp

    url = f"{MINECRAFT_PANEL_URL}/api/client/servers/{MINECRAFT_SERVER_ID}/resources"
    headers = {
        "Authorization": f"Bearer {MINECRAFT_API_KEY}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin": MINECRAFT_PANEL_URL,
        "Referer": f"{MINECRAFT_PANEL_URL}/server/{MINECRAFT_SERVER_ID}/console",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                body = await resp.text()
                logger.info(
                    "Minecraft status-запрос ответ: HTTP %s, тело (первые 300 симв.): %s",
                    resp.status, body[:300],
                )
                if resp.status != 200:
                    return None, f"HTTP {resp.status}"
                import json as _json
                data = _json.loads(body)
                state = data.get("attributes", {}).get("current_state")
                return state, None
    except Exception as e:
        logger.exception("Ошибка запроса статуса Minecraft-сервера")
        return None, str(e)


async def tg_handle_mcstart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /mcstart — запускает Minecraft-сервер через панель Rustix.me."""
    chat = update.effective_chat
    if chat is None:
        return
    ok, detail = await _minecraft_power_action("start")
    if not ok:
        await context.bot.send_message(chat_id=chat.id, text=f"⚠️ Не удалось запустить сервер: {detail}")
        logger.warning("Не удалось запустить Minecraft-сервер: %s", detail)
        return

    await asyncio.sleep(2)  # даём панели секунду обновить статус
    state, err = await _minecraft_get_status()
    if err:
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"🟢 Команда отправлена, но не удалось проверить статус ({err}).",
        )
    else:
        await context.bot.send_message(
            chat_id=chat.id, text=f"🟢 Команда отправлена. Текущий статус: `{state}`", parse_mode="Markdown"
        )

# ========================= /MINECRAFT ======================================


async def tg_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает входящие сообщения Telegram и пересылает их в XMPP/Discord."""
    msg = update.effective_message
    if msg is None:
        return
    # Игнорируем только СВОЁ ЖЕ эхо (не любых ботов вообще — иначе
    # ломается пересылка от сторонних ботов-мостов вроде matrixt2bot).
    if msg.from_user and msg.from_user.id == context.bot.id:
        return

    raw_author_name = msg.from_user.first_name if msg.from_user else "Telegram"
    author = _colored_author(
        f"tg:{msg.from_user.id}" if msg.from_user else "tg:unknown",
        raw_author_name,
    )
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
        if MC_CHAT_BRIDGE_ENABLED:
            await _send_to_minecraft_chat(f"{raw_author_name}: {msg.text}")
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
            if xmpp_bridge:
                await xmpp_bridge.send_text(notice)
            if discord_bridge:
                await discord_bridge.send_text(notice)
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
        if MC_CHAT_BRIDGE_ENABLED:
            label = _mc_media_label(local_path.suffix)
            caption_part = f" {msg.caption}" if msg.caption else ""
            await _send_to_minecraft_chat(f"{raw_author_name}: {label}{caption_part}")
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
    if not record or time.monotonic() - record["ts"] > EDIT_WINDOW_SECONDS:
        logger.info(
            "Отредактировано сообщение %s, но оно старше %s сек или не пересылалось — игнорирую",
            msg.message_id, EDIT_WINDOW_SECONDS,
        )
        return

    author = _colored_author(
        f"tg:{msg.from_user.id}" if msg.from_user else "tg:unknown",
        msg.from_user.first_name if msg.from_user else "Telegram",
    )

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
# MINECRAFT AFK-БОТ (Node.js/mineflayer) — НЕОБЯЗАТЕЛЬНЫЙ БЛОК.
# Держит Minecraft-сервер (например, Aternos) "живым", подключаясь как
# игрок. Запускается как подпроцесс, чтобы жить в том же Render-сервисе
# и не тратить отдельный лимит бесплатных часов.
#
# Чтобы отключить — не задавайте MC_HOST в .env (тогда подпроцесс просто
# не стартует), либо удалите/закомментируйте вызовы
# _start_minecraft_afk_bot() / _stop_minecraft_afk_bot() в main().

_minecraft_afk_process = None
_minecraft_afk_shutting_down = False


MC_CHAT_BRIDGE_ENABLED = bool(os.environ.get("MC_HOST"))


async def _start_minecraft_afk_bot():
    global _minecraft_afk_process
    if not os.environ.get("MC_HOST"):
        return  # не настроено — не запускаем

    afk_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minecraft_afk")
    if not os.path.isdir(afk_dir):
        logger.warning("Папка minecraft_afk не найдена рядом с bridge.py — AFK-бот не запущен")
        return

    # Не передаём подпроцессу PORT — порт держит только этот, Python,
    # процесс (иначе Node попробует занять тот же порт и упадёт).
    env = os.environ.copy()
    env.pop("PORT", None)

    # Ограничиваем кучу V8 у Node — на бесплатных тарифах (обычно 512 МБ
    # на весь контейнер) без этого Node может незаметно вырасти и увести
    # в OOM-килл ВЕСЬ контейнер целиком (вместе с Python-частью моста).
    # С лимитом упадёт только сам Node-процесс — переподключится по
    # своей же логике реконнекта, а не потащит за собой всё остальное.
    # При необходимости поправьте под свой тариф Render через
    # MC_NODE_MAX_OLD_SPACE_MB (в мегабайтах).
    max_heap_mb = os.environ.get("MC_NODE_MAX_OLD_SPACE_MB", "160")

    logger.info("Запускаю AFK-бота Minecraft (Node.js/mineflayer)...")
    try:
        _minecraft_afk_process = await asyncio.create_subprocess_exec(
            "node", f"--max-old-space-size={max_heap_mb}", "index.js",
            cwd=afk_dir,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(_pipe_minecraft_afk_logs())
        asyncio.create_task(_watch_minecraft_afk_process())
    except FileNotFoundError:
        logger.error(
            "Не удалось запустить AFK-бота: команда 'node' не найдена. "
            "Убедитесь, что в окружении установлен Node.js (см. Dockerfile)."
        )
    except Exception:
        logger.exception("Не удалось запустить AFK-бота Minecraft")


async def _pipe_minecraft_afk_logs():
    """Читает stdout Node-подпроцесса: обычные строки — в общий лог,
    строки с префиксом MCCHAT_IN: — как сообщения игрового чата,
    которые нужно переслать на остальные площадки."""
    if _minecraft_afk_process is None or _minecraft_afk_process.stdout is None:
        return
    async for raw_line in _minecraft_afk_process.stdout:
        line = raw_line.decode(errors="replace").rstrip()
        if line.startswith("MCCHAT_IN:"):
            await _handle_minecraft_chat_message(line[len("MCCHAT_IN:"):])
        else:
            logger.info("[minecraft-afk] %s", line)


async def _handle_minecraft_chat_message(payload_json: str) -> None:
    """Пересылает сообщение игрового чата Minecraft на Telegram/XMPP/Discord."""
    import json as _json

    try:
        data = _json.loads(payload_json)
    except Exception:
        logger.warning("Не удалось разобрать сообщение чата Minecraft: %s", payload_json)
        return

    username = str(data.get("username") or "Minecraft")
    message = str(data.get("message") or "").strip()
    if not message:
        return

    author = _colored_author(f"minecraft:{username}", username)
    text_tg = f"{_html_escape(author, quote=False)}: {_html_escape(message, quote=False)}"
    text_other = f"{author}: {message}"

    if telegram_app and TELEGRAM_CHAT_ID:
        try:
            await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text_tg, parse_mode="HTML")
        except Exception:
            logger.exception("Не удалось переслать сообщение Minecraft-чата в Telegram")
    if xmpp_bridge:
        try:
            await xmpp_bridge.send_text(text_other)
        except Exception:
            logger.exception("Не удалось переслать сообщение Minecraft-чата в XMPP")
    if discord_bridge:
        try:
            await discord_bridge.send_text(text_other)
        except Exception:
            logger.exception("Не удалось переслать сообщение Minecraft-чата в Discord")


async def _send_to_minecraft_chat(text: str) -> None:
    """Отправляет строку в игровой чат Minecraft через AFK-бота (если он запущен)."""
    import json as _json

    if not MC_CHAT_BRIDGE_ENABLED or _minecraft_afk_process is None:
        return
    if _minecraft_afk_process.stdin is None:
        return
    try:
        line = f"MCCHAT_OUT:{_json.dumps({'text': text[:250]})}\n"
        _minecraft_afk_process.stdin.write(line.encode())
        await _minecraft_afk_process.stdin.drain()
    except Exception:
        logger.exception("Не удалось отправить сообщение в Minecraft-чат")


async def _watch_minecraft_afk_process() -> None:
    """Если Node-подпроцесс неожиданно завершился (например, упёрся в
    лимит памяти) — перезапускаем его заново через паузу, а не
    оставляем мёртвым до следующего деплоя."""
    process = _minecraft_afk_process
    if process is None:
        return
    returncode = await process.wait()
    if _minecraft_afk_shutting_down:
        return  # это штатная остановка при выключении бота, не ошибка
    logger.warning(
        "AFK-бот Minecraft неожиданно завершился (код %s) — перезапускаю через 10 сек",
        returncode,
    )
    await asyncio.sleep(10)
    await _start_minecraft_afk_bot()


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
# Чтобы отключить — удалите/закомментируйте вызов
# asyncio.create_task(_tg_check_deletions_loop()) в main().

TELEGRAM_DELETE_CHECK_INTERVAL = int(os.environ.get("TELEGRAM_DELETE_CHECK_INTERVAL", "10"))
TELEGRAM_DELETE_CHECK_BATCH = int(os.environ.get("TELEGRAM_DELETE_CHECK_BATCH", "200"))
# Пауза между отдельными проверками внутри одного прохода — весь проход
# (до 200 сообщений) должен укладываться в несколько секунд, чтобы не
# съедать весь интервал между проходами.
TELEGRAM_DELETE_CHECK_DELAY = float(os.environ.get("TELEGRAM_DELETE_CHECK_DELAY", "0.03"))


async def _tg_check_deletions_loop() -> None:
    logger.info(
        "Проверка удалений Telegram запущена (каждые %s сек, до %s сообщений за проход)",
        TELEGRAM_DELETE_CHECK_INTERVAL, TELEGRAM_DELETE_CHECK_BATCH,
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
            try:
                # Безобидно: снимаем клавиатуру, которой и так нет.
                await telegram_app.bot.edit_message_reply_markup(
                    chat_id=TELEGRAM_CHAT_ID, message_id=tg_id, reply_markup=None
                )
            except BadRequest as e:
                error_text = str(e).lower()
                # Telegram может вернуть разные формулировки для удалённого
                # сообщения в зависимости от ситуации — проверяем известные
                # варианты, а не только один текст.
                deleted_markers = (
                    "message to edit not found",
                    "message_id_invalid",
                    "message to delete not found",
                    "message can't be found",
                )
                if any(marker in error_text for marker in deleted_markers):
                    logger.info(
                        "Обнаружено удаление сообщения Telegram %s — удаляю на других площадках", tg_id
                    )
                    await _propagate_deletion(record, skip="tg")
                    _forget_record_by_key(key)
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


async def _stop_minecraft_afk_bot():
    global _minecraft_afk_shutting_down
    _minecraft_afk_shutting_down = True
    if _minecraft_afk_process is None:
        return
    try:
        _minecraft_afk_process.terminate()
        await asyncio.wait_for(_minecraft_afk_process.wait(), timeout=5)
    except Exception:
        try:
            _minecraft_afk_process.kill()
        except Exception:
            pass

# ========================== /MINECRAFT AFK-БОТ =============================


# ========================================================================
# ЗАПУСК
# ========================================================================

async def main() -> None:
    global telegram_app, xmpp_bridge, discord_bridge

    healthcheck_runner = await _start_healthcheck_server()
    await _start_minecraft_afk_bot()

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)

    if TELEGRAM_FORCE_IPV4:
        import httpx

        ipv4_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        forced_request = HTTPXRequest(httpx_kwargs={"transport": ipv4_transport})
        builder = builder.request(forced_request).get_updates_request(forced_request)
        logger.info("Принудительно использую IPv4 для запросов к Telegram (TELEGRAM_FORCE_IPV4=true)")

    telegram_app = builder.build()

    telegram_app.add_handler(CommandHandler("chatid", tg_handle_chatid))

    # MINECRAFT: чтобы отключить — удалите/закомментируйте следующие 2 строки.
    if MINECRAFT_ENABLED:
        telegram_app.add_handler(CommandHandler("mcstart", tg_handle_mcstart))

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
            await _stop_minecraft_afk_bot()


if __name__ == "__main__":
    asyncio.run(main())
