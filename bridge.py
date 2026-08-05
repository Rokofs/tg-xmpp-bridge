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
- Редактирование и реакции поддерживаются только для обычного текста
  (не для подписей к медиа и не для самих файлов).
- Реакция "от нескольких разных людей одновременно" не агрегируется
  идеально: если на исходной площадке реагируют несколько человек
  разными эмодзи, на других площадках отражается только последнее
  изменение (не сумма всех реакций).
- Ответы (reply) работают, только если исходное сообщение (на которое
  отвечают) было переслано мостом, то есть связка для него ещё жива
  в памяти (см. MESSAGE_MAP_TTL_SECONDS ниже). Ответ на что-то более
  старое или отправленное до запуска бота придёт как обычное
  сообщение, без привязки к реплаю.
- Вся связка сообщений между площадками хранится в памяти процесса и
  теряется при перезапуске бота (старые сообщения после перезапуска
  уже нельзя отредактировать/реагировать через мост).
- Пересылка XMPP-файлов (не Telegram/Discord) идёт только ссылкой —
  сам файл с XMPP-стороны на другие площадки не перезаливается.
"""

import asyncio
import itertools
import logging
import os
import re
import shutil
import tempfile
import time
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


def _new_bridge_record(**ids) -> str:
    key = str(next(_bridge_key_counter))
    record = {"tg": None, "xmpp": None, "discord": None, "ts": time.monotonic()}
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
    return {k: v for k, v in record.items() if k in ("tg", "xmpp", "discord") and v is not None}


def _get_reply_key(origin: str, replied_native_id) -> Optional[str]:
    """То же, что _get_reply_targets, но возвращает сам bridge key (для реакций)."""
    if replied_native_id is None:
        return None
    _prune_bridge_records()
    lookup = {"tg": _tg_lookup, "xmpp": _xmpp_lookup, "discord": _discord_lookup}[origin]
    return lookup.get(replied_native_id)


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

    async def on_xmpp_message(self, msg):
        """Обрабатывает входящие XMPP-сообщения: новые и правки."""
        if msg["type"] not in ("chat", "normal", "groupchat"):
            return
        if not self._is_from_peer(msg):
            return

        body = (msg["body"] or "").strip()
        oob_url = msg["oob"]["url"] or None
        correction_id = msg["replace"]["id"] or None

        if not body and not oob_url:
            return

        sender = str(msg["mucnick"]) if XMPP_IS_MUC else str(msg["from"].bare)

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

            text = f"{sender}: {body} (ред.)"
            if telegram_app and record.get("tg"):
                try:
                    await telegram_app.bot.edit_message_text(
                        chat_id=TELEGRAM_CHAT_ID, message_id=record["tg"], text=text
                    )
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.exception("Не удалось применить исправление в Telegram")
                except Exception:
                    logger.exception("Не удалось применить исправление в Telegram")
            if discord_bridge and record.get("discord"):
                try:
                    await discord_bridge.send_correction(record["discord"], text)
                except Exception:
                    logger.exception("Не удалось применить исправление в Discord")
            return

        # --- обычное новое сообщение ---
        reply_id = msg["reply"]["id"] or None

        # Ответ одним (или несколькими) эмодзи на пересланное сообщение —
        # считаем это реакцией, а не обычным ответом (полезно, если в
        # клиенте нет отдельной кнопки для реакций).
        if reply_id and body and not oob_url and _is_emoji_only(body):
            reaction_key = _get_reply_key("xmpp", reply_id)
            if reaction_key:
                await _mirror_reactions(reaction_key, _extract_emojis(body), skip="xmpp")
                return

        text = f"{sender}: {oob_url}" if oob_url and not body else f"{sender}: {body}"
        if oob_url and body and body != oob_url:
            text = f"{sender}: {body}\n{oob_url}"

        reply_targets = _get_reply_targets("xmpp", reply_id) if reply_id else {}

        key = _new_bridge_record()
        stanza_id = msg["id"] or None
        if stanza_id:
            _link_record(key, xmpp=stanza_id)

        if telegram_app:
            try:
                sent = await telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=text,
                    reply_to_message_id=reply_targets.get("tg"),
                )
                _link_record(key, tg=sent.message_id)
            except Exception:
                logger.exception("Не удалось переслать сообщение из XMPP в Telegram")

        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_text(text, reply_to=reply_targets.get("discord"))
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

    async def send_text(self, text: str, reply_to: Optional[str] = None) -> str:
        """Отправляет текст, возвращает id отправленной станзы (для правок/реакций).

        reply_to — id station сообщения, на которое отвечаем (XEP-0461).
        """
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=text, mtype=mtype)
        if reply_to:
            stanza["reply"]["id"] = reply_to
            stanza["reply"]["to"] = XMPP_PEER_JID
        stanza.send()
        return stanza["id"]

    async def send_correction(self, new_text: str, original_stanza_id: str) -> None:
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=new_text, mtype=mtype)
        stanza["replace"]["id"] = original_stanza_id
        stanza.send()

    async def send_reaction(self, target_stanza_id: str, emojis: set) -> None:
        await self["xep_0444"].send_reactions(JID(XMPP_PEER_JID), target_stanza_id, emojis)

    async def send_file(self, filepath: Path, caption: str = "", reply_to: Optional[str] = None) -> None:
        """Загружает файл на XMPP-сервер (XEP-0363) и отправляет ссылку с OOB."""
        try:
            url = await self["xep_0363"].upload_file(str(filepath))
        except UploadServiceNotFound:
            logger.warning("На XMPP-сервере не найден сервис HTTP Upload")
            await self.send_text(f"{caption} (сервер не поддерживает загрузку файлов)".strip())
            return
        except FileTooBig as e:
            logger.warning("Файл слишком большой для загрузки: %s", e)
            await self.send_text(f"{caption} (файл слишком большой для XMPP-сервера)".strip())
            return
        except HTTPError as e:
            # Некоторые серверы (замечено на xmpp.jp) отклоняют PUT для
            # определённых расширений (например, видео/gif) с 405.
            # Пробуем обойти: перезаливаем тот же файл под нейтральным
            # расширением .dat.
            logger.warning("Сервер отклонил загрузку (%s), пробую с нейтральным расширением", e)
            renamed = filepath.with_name(filepath.stem + "_upload.dat")
            try:
                shutil.copyfile(filepath, renamed)
                url = await self["xep_0363"].upload_file(str(renamed))
                logger.info("Обходной путь сработал — файл загружен под нейтральным расширением")
            except Exception:
                logger.exception("Обходной путь тоже не сработал — сервер отклоняет файл")
                await self.send_text(f"{caption} (сервер отклонил загрузку файла: {e})".strip())
                return
            finally:
                renamed.unlink(missing_ok=True)
        except IqError as e:
            logger.exception("Ошибка загрузки файла на XMPP-сервер")
            await self.send_text(f"{caption} (ошибка загрузки файла: {e})".strip())
            return
        except IqTimeout as e:
            logger.exception("Таймаут при загрузке файла на XMPP-сервер")
            await self.send_text(f"{caption} (таймаут при загрузке файла: {e})".strip())
            return
        except OSError as e:
            logger.exception("Сетевая ошибка при загрузке файла на XMPP-сервер")
            await self.send_text(f"{caption} (сетевая ошибка при загрузке файла: {e})".strip())
            return

        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        body = f"{caption}\n{url}".strip() if caption else url
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=body, mtype=mtype)
        stanza["oob"]["url"] = url
        if reply_to:
            stanza["reply"]["id"] = reply_to
            stanza["reply"]["to"] = XMPP_PEER_JID
        stanza.send()


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
        if message.author.bot:
            return
        if message.channel.id != DISCORD_CHANNEL_ID:
            return

        author = message.author.display_name

        replied_id = message.reference.message_id if message.reference else None

        # Ответ одним эмодзи на пересланное сообщение — считаем реакцией.
        if replied_id and message.content and not message.attachments and _is_emoji_only(message.content):
            reaction_key = _get_reply_key("discord", replied_id)
            if reaction_key:
                await _mirror_reactions(reaction_key, _extract_emojis(message.content), skip="discord")
                return

        reply_targets = _get_reply_targets("discord", replied_id) if replied_id else {}

        # --- вложения ---
        for attachment in message.attachments:
            caption = f"{author}: {message.content}" if message.content else author
            try:
                local_path = await _download_discord_attachment(attachment)
            except Exception:
                logger.exception("Не удалось скачать вложение из Discord")
                continue
            try:
                if xmpp_bridge:
                    try:
                        await xmpp_bridge.send_file(
                            local_path, caption=caption, reply_to=reply_targets.get("xmpp")
                        )
                    except Exception:
                        logger.exception("Не удалось переслать вложение из Discord в XMPP")
                if telegram_app:
                    try:
                        await _tg_send_file(local_path, caption, reply_to=reply_targets.get("tg"))
                    except Exception:
                        logger.exception("Не удалось переслать вложение из Discord в Telegram")
            finally:
                local_path.unlink(missing_ok=True)

        # --- текст ---
        if not message.content:
            return
        text = f"{author}: {message.content}"
        key = _new_bridge_record(discord=message.id)

        if xmpp_bridge:
            try:
                xmpp_id = await xmpp_bridge.send_text(text, reply_to=reply_targets.get("xmpp"))
                _link_record(key, xmpp=xmpp_id)
            except Exception:
                logger.exception("Не удалось переслать сообщение из Discord в XMPP")

        if telegram_app:
            try:
                sent = await telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=text,
                    reply_to_message_id=reply_targets.get("tg"),
                )
                _link_record(key, tg=sent.message_id)
            except Exception:
                logger.exception("Не удалось переслать сообщение из Discord в Telegram")

    async def on_message_edit(self, before, after):
        if self.user and after.author.id == self.user.id:
            return
        if after.author.bot:
            return
        if after.channel.id != DISCORD_CHANNEL_ID:
            return

        _prune_bridge_records()
        key = _discord_lookup.get(after.id)
        record = _bridge_records.get(key) if key else None
        if not record or time.monotonic() - record["ts"] > EDIT_WINDOW_SECONDS:
            return

        text = f"{after.author.display_name}: {after.content}"
        if xmpp_bridge and record.get("xmpp"):
            try:
                await xmpp_bridge.send_correction(text, record["xmpp"])
            except Exception:
                logger.exception("Не удалось переслать исправление в XMPP")
        if telegram_app and record.get("tg"):
            try:
                await telegram_app.bot.edit_message_text(
                    chat_id=TELEGRAM_CHAT_ID, message_id=record["tg"], text=text
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
    local_path: Path, caption: str, reply_to: Optional[int] = None
) -> None:
    """Отправляет файл в Telegram, подбирая подходящий метод по расширению."""
    suffix = local_path.suffix.lower()
    with open(local_path, "rb") as f:
        if suffix in (".jpg", ".jpeg", ".png", ".webp"):
            await telegram_app.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID, photo=f, caption=caption, reply_to_message_id=reply_to
            )
        elif suffix in (".mp4", ".mov", ".webm"):
            await telegram_app.bot.send_video(
                chat_id=TELEGRAM_CHAT_ID, video=f, caption=caption, reply_to_message_id=reply_to
            )
        elif suffix in (".mp3", ".ogg", ".wav", ".m4a"):
            await telegram_app.bot.send_audio(
                chat_id=TELEGRAM_CHAT_ID, audio=f, caption=caption, reply_to_message_id=reply_to
            )
        else:
            await telegram_app.bot.send_document(
                chat_id=TELEGRAM_CHAT_ID, document=f, caption=caption, reply_to_message_id=reply_to
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
    if msg.from_user and msg.from_user.is_bot:
        return

    author = msg.from_user.first_name if msg.from_user else "Telegram"
    has_media = any(getattr(msg, attr, None) for attr in _MEDIA_ATTRS)

    replied_id = msg.reply_to_message.message_id if msg.reply_to_message else None

    # Ответ одним эмодзи на пересланное сообщение — считаем реакцией.
    if replied_id and msg.text and not has_media and _is_emoji_only(msg.text):
        reaction_key = _get_reply_key("tg", replied_id)
        if reaction_key:
            await _mirror_reactions(reaction_key, _extract_emojis(msg.text), skip="tg")
            return

    reply_targets = _get_reply_targets("tg", replied_id) if replied_id else {}

    # --- чистый текст, без вложений ---
    if msg.text and not has_media:
        text = f"{author}: {msg.text}"
        key = _new_bridge_record(tg=msg.message_id)
        if xmpp_bridge:
            try:
                xmpp_id = await xmpp_bridge.send_text(text, reply_to=reply_targets.get("xmpp"))
                _link_record(key, xmpp=xmpp_id)
            except Exception:
                logger.exception("Не удалось переслать сообщение из Telegram в XMPP")
        if discord_bridge:
            try:
                discord_id = await discord_bridge.send_text(text, reply_to=reply_targets.get("discord"))
                _link_record(key, discord=discord_id)
            except Exception:
                logger.exception("Не удалось переслать сообщение из Telegram в Discord")
        return

    if not has_media:
        return  # сервисное сообщение и т.п.

    caption = f"{author}: {msg.caption}" if msg.caption else author

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
    try:
        await file_obj.download_to_drive(custom_path=str(local_path))
        if is_static_sticker:
            local_path = _convert_static_sticker_to_png(local_path)
        if xmpp_bridge:
            try:
                await xmpp_bridge.send_file(local_path, caption=caption, reply_to=reply_targets.get("xmpp"))
            except Exception:
                logger.exception("Не удалось переслать медиа из Telegram в XMPP")
        if discord_bridge:
            try:
                await discord_bridge.send_file(
                    local_path, caption=caption, reply_to=reply_targets.get("discord")
                )
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
    if msg.from_user and msg.from_user.is_bot:
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

    author = msg.from_user.first_name if msg.from_user else "Telegram"
    text = f"{author}: {msg.text}"

    if xmpp_bridge and record.get("xmpp"):
        try:
            await xmpp_bridge.send_correction(text, record["xmpp"])
        except Exception:
            logger.exception("Не удалось переслать исправление в XMPP")
    if discord_bridge and record.get("discord"):
        try:
            await discord_bridge.send_correction(record["discord"], text)
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


# ========================================================================
# ЗАПУСК
# ========================================================================

async def main() -> None:
    global telegram_app, xmpp_bridge, discord_bridge

    healthcheck_runner = await _start_healthcheck_server()

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)

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


if __name__ == "__main__":
    asyncio.run(main())
