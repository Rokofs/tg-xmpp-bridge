"""
Telegram <-> XMPP мост (bridge)
================================

Пересылает текстовые сообщения и медиафайлы (фото, видео, гифки,
голосовые, аудио, документы, стикеры — статические и анимированные)
между одним чатом/группой в Telegram и одним контактом или комнатой
(MUC) в XMPP (xmpp.jp, monocles.eu и любой другой сервер,
поддерживающий XEP-0363 HTTP Upload).

АРХИТЕКТУРА
-----------
- Telegram-часть: python-telegram-bot (v20+, асинхронный, long polling).
- XMPP-часть: slixmpp (нативно асинхронный, на asyncio).
- Обе части работают в одном asyncio event loop — отдельный поток
  или второй loop не нужен.

НАПРАВЛЕНИЕ TG -> XMPP
- Текст пересылается как обычное сообщение с именем отправителя.
- Медиа скачивается с серверов Telegram, загружается на XMPP-сервер
  через HTTP Upload (XEP-0363) и отправляется как OOB-ссылка
  (XEP-0066), которую большинство XMPP-клиентов (в т.ч. monocles
  chat) показывают как обычное вложение.

НАПРАВЛЕНИЕ XMPP -> TG
- Текст пересылается как обычное сообщение.
- Если в сообщении есть OOB-ссылка на файл — она пересылается в
  Telegram как ссылка (Telegram сам сделает превью для картинок/видео,
  если ссылка публично доступна).

УСТАНОВКА
---------
    pip install python-telegram-bot slixmpp

НАСТРОЙКА
---------
Все параметры передаются через переменные окружения (см. .env.example
рядом с этим файлом) — так секреты не попадают в код и в git.

ОГРАНИЧЕНИЯ ЭТОЙ ВЕРСИИ
------------------------
- Мост работает только с ОДНИМ Telegram-чатом и ОДНИМ XMPP-собеседником
  (или одной MUC-комнатой) одновременно. Для нескольких пар чатов
  запускайте несколько экземпляров с разными переменными окружения.
- Файлы > 20 МБ Telegram Bot API не отдаёт ботам (ограничение самого
  Telegram, не скрипта) — при превышении лимита бот пишет об этом
  текстовым сообщением в XMPP-чат вместо файла.
- Статические стикеры (.webp) конвертируются в PNG для совместимости
  с XMPP-клиентами (нужен пакет Pillow — без него отправятся как
  .webp). Анимированные (.tgs) и видео-стикеры (.webm) пересылаются
  как есть — конвертация в статичную картинку не производится.
- Редактирование и удаление сообщений не синхронизируются.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

try:
    # Если установлен python-dotenv (pip install python-dotenv) и рядом
    # со скриптом лежит файл .env — переменные из него подхватятся
    # автоматически. Если пакета нет, просто продолжаем: значит,
    # переменные окружения нужно задать вручную перед запуском.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import slixmpp
from slixmpp.exceptions import IqError, IqTimeout
from slixmpp.plugins.xep_0363.http_upload import UploadServiceNotFound, FileTooBig, HTTPError

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

# ========================================================================
# КОНФИГУРАЦИЯ (через переменные окружения)
# ========================================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# TELEGRAM_CHAT_ID можно не указывать при первом запуске: бот всё равно
# стартует и по команде /chatid пришлёт ID текущего чата. Впишите
# полученное число в .env и перезапустите бота, чтобы включить мост.
_raw_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_CHAT_ID = int(_raw_chat_id) if _raw_chat_id else None

# XMPP-параметры тоже необязательны на первом запуске (когда вы ещё
# только узнаёте TELEGRAM_CHAT_ID) — если чего-то не хватает, мост
# просто не подключается к XMPP, но /chatid всё равно работает.
XMPP_JID = os.environ.get("XMPP_JID", "").strip() or None
XMPP_PASSWORD = os.environ.get("XMPP_PASSWORD", "").strip() or None
XMPP_PEER_JID = os.environ.get("XMPP_PEER_JID", "").strip() or None
XMPP_IS_MUC = os.environ.get("XMPP_IS_MUC", "false").strip().lower() == "true"
XMPP_MUC_NICK = os.environ.get("XMPP_MUC_NICK", "TelegramBridge")
XMPP_MUC_PASSWORD = os.environ.get("XMPP_MUC_PASSWORD", "").strip() or None

XMPP_ENABLED = bool(XMPP_JID and XMPP_PASSWORD and XMPP_PEER_JID)
BRIDGE_ENABLED = bool(TELEGRAM_CHAT_ID and XMPP_ENABLED)

# Некоторые VPN-приложения туннелируют только IPv4, а httpx/anyio по
# умолчанию сначала пробует IPv6 и подолгу "виснет" на нём, прежде чем
# откатиться на IPv4 — из-за этого telegram.error.TimedOut даже при
# рабочем интернете. TELEGRAM_FORCE_IPV4=true заставляет httpx сразу
# использовать только IPv4 и обходит эту проблему.
TELEGRAM_FORCE_IPV4 = os.environ.get("TELEGRAM_FORCE_IPV4", "false").strip().lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("tg_xmpp_bridge")
logging.getLogger("slixmpp").setLevel(logging.WARNING)  # slixmpp сам по себе многословный

TMP_DIR = Path(tempfile.gettempdir()) / "tg_xmpp_bridge"
TMP_DIR.mkdir(parents=True, exist_ok=True)


# ========================================================================
# XMPP-СТОРОНА
# ========================================================================

class XMPPBridge(slixmpp.ClientXMPP):
    """XMPP-клиент, принимающий/отправляющий текст и файлы."""

    def __init__(self, jid: str, password: str, telegram_app: Application):
        super().__init__(jid, password)
        self.telegram_app = telegram_app

        self.add_event_handler("session_start", self.on_session_start)
        self.add_event_handler("message", self.on_xmpp_message)

        self.register_plugin("xep_0030")  # Service Discovery — нужен для поиска сервиса загрузки
        self.register_plugin("xep_0128")  # Service Discovery Extensions — зависимость xep_0363
        self.register_plugin("xep_0199")  # XMPP Ping — держит соединение живым
        self.register_plugin("xep_0066")  # Out of Band Data — ссылки на файлы
        self.register_plugin("xep_0363")  # HTTP Upload — загрузка файлов на сервер
        self.register_plugin("xep_0045")  # Multi-User Chat — если мостим в комнату

    async def on_session_start(self, event):
        self.send_presence()
        await self.get_roster()

        if XMPP_IS_MUC:
            self.plugin["xep_0045"].join_muc(XMPP_PEER_JID, XMPP_MUC_NICK, password=XMPP_MUC_PASSWORD)
            logger.info("XMPP: зашёл в комнату %s как %s", XMPP_PEER_JID, XMPP_MUC_NICK)

        logger.info("XMPP: сессия начата, вошёл как %s", self.boundjid.bare)

    async def on_xmpp_message(self, msg):
        """Обрабатывает входящие XMPP-сообщения и пересылает их в Telegram."""
        if msg["type"] not in ("chat", "normal", "groupchat"):
            return

        # Не реагируем на собственные сообщения в MUC
        if XMPP_IS_MUC:
            if str(msg["mucnick"]) == XMPP_MUC_NICK:
                return
            if str(msg["from"].bare) != XMPP_PEER_JID:
                return  # сообщение из другой комнаты — игнор
        else:
            if str(msg["from"].bare) != XMPP_PEER_JID:
                return  # сообщение не от нашего собеседника — игнор

        body = (msg["body"] or "").strip()
        oob_url = msg["oob"]["url"]  # None, если ссылки на файл нет

        if not body and not oob_url:
            return

        sender = str(msg["mucnick"]) if XMPP_IS_MUC else str(msg["from"].bare)

        try:
            if oob_url:
                text = f"{sender}: {oob_url}"
                if body and body != oob_url:
                    text = f"{sender}: {body}\n{oob_url}"
                await self.telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
            else:
                await self.telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=f"{sender}: {body}"
                )
        except Exception:
            logger.exception("Не удалось переслать сообщение из XMPP в Telegram")

    async def send_text(self, text: str) -> None:
        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        self.send_message(mto=XMPP_PEER_JID, mbody=text, mtype=mtype)

    async def send_file(self, filepath: Path, caption: str = "") -> None:
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
            # определённых расширений (например, видео/gif) с 405, при
            # этом изображения принимают нормально. Похоже на конфиг
            # веб-сервера, заточенный под отдачу видео, а не приём.
            # Пробуем обойти: перезаливаем тот же файл под нейтральным
            # расширением .dat — если дело было в расширении, сработает.
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
            # Сюда попадают сетевые обрывы соединения (в т.ч.
            # ClientConnectorDNSError от aiohttp) — например, из-за VPN,
            # блокирующего DNS-резолвер aiodns. См. README.
            logger.exception("Сетевая ошибка при загрузке файла на XMPP-сервер")
            await self.send_text(f"{caption} (сетевая ошибка при загрузке файла: {e})".strip())
            return

        mtype = "groupchat" if XMPP_IS_MUC else "chat"
        body = f"{caption}\n{url}".strip() if caption else url
        stanza = self.make_message(mto=XMPP_PEER_JID, mbody=body, mtype=mtype)
        stanza["oob"]["url"] = url
        stanza.send()


# ========================================================================
# TELEGRAM-СТОРОНА
# ========================================================================

xmpp_bridge: Optional[XMPPBridge] = None

# какие типы сообщений считаем "просто текстом" (без вложений)
_MEDIA_ATTRS = ("photo", "video", "voice", "audio", "document", "sticker", "video_note", "animation")

# Лимит Telegram Bot API на скачивание файлов ботом (не зависит от скрипта).
TELEGRAM_BOT_API_FILE_LIMIT_MB = 20


def _convert_static_sticker_to_png(path: Path) -> Path:
    """Конвертирует статический webp-стикер в PNG.

    Многие XMPP-клиенты (в т.ч. некоторые версии Monocles Chat) не
    показывают превью .webp как картинку. PNG отображается везде.
    Если Pillow не установлен — просто отправляем исходный .webp.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning(
            "Pillow не установлен (pip install Pillow) — статический стикер "
            "будет отправлен как .webp без конвертации в PNG"
        )
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


async def tg_handle_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /chatid — присылает ID текущего чата, группы или канала.

    Работает в любом чате, где есть бот, вне зависимости от того,
    настроен мост или нет — удобно, чтобы один раз узнать нужное
    значение для TELEGRAM_CHAT_ID в .env.
    """
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
    """Обрабатывает входящие сообщения Telegram и пересылает их в XMPP."""
    msg = update.effective_message
    if msg is None:
        return
    if msg.from_user and msg.from_user.is_bot:
        return  # не пересылаем сообщения от ботов (в т.ч. свои же эхо)

    author = msg.from_user.first_name if msg.from_user else "Telegram"
    has_media = any(getattr(msg, attr, None) for attr in _MEDIA_ATTRS)

    # --- чистый текст, без вложений ---
    if msg.text and not has_media:
        await xmpp_bridge.send_text(f"{author}: {msg.text}")
        return

    if not has_media:
        return  # сервисное сообщение и т.п. — пропускаем

    caption = f"{author}: {msg.caption}" if msg.caption else author

    file_obj = None
    suffix = ""
    is_static_sticker = False

    try:
        if msg.photo:
            file_obj = await msg.photo[-1].get_file()  # берём самое большое разрешение
            suffix = ".jpg"
        elif msg.video:
            file_obj = await msg.video.get_file()
            suffix = Path(msg.video.file_name or "video.mp4").suffix or ".mp4"
        elif msg.video_note:
            file_obj = await msg.video_note.get_file()
            suffix = ".mp4"
        elif msg.animation:
            # GIF в Telegram технически передаётся как MP4-анимация без звука
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
                suffix = ".tgs"  # анимированный стикер (Lottie/JSON) — как есть, не картинка
            elif msg.sticker.is_video:
                suffix = ".webm"  # видео-стикер — как есть, не картинка
            else:
                suffix = ".webp"  # статический стикер — сконвертируем в PNG ниже
                is_static_sticker = True
    except BadRequest as e:
        if "too big" in str(e).lower():
            logger.warning("Файл превышает лимит Telegram Bot API: %s", e)
            await xmpp_bridge.send_text(
                f"{author}: [файл не переслан — превышает лимит Telegram Bot API "
                f"в {TELEGRAM_BOT_API_FILE_LIMIT_MB} МБ на скачивание ботом]"
            )
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
        await xmpp_bridge.send_file(local_path, caption=caption)
    except Exception:
        logger.exception("Не удалось переслать медиа из Telegram в XMPP")
    finally:
        # На Windows файл иногда остаётся ненадолго заблокирован
        # библиотекой, если загрузка на XMPP-сервер оборвалась с
        # ошибкой соединения — в этом случае просто оставляем файл
        # (система сама подчистит временную папку), не роняя обработчик.
        try:
            local_path.unlink(missing_ok=True)
        except PermissionError:
            logger.warning(
                "Не удалось сразу удалить временный файл %s — он ещё занят "
                "другим процессом, оставляю как есть",
                local_path,
            )


# ========================================================================
# HTTP HEALTH-CHECK (для хостингов вроде Render, которым нужен открытый порт)
# ========================================================================

async def _start_healthcheck_server():
    """Поднимает минимальный HTTP-сервер на порту из переменной PORT.

    Нужен только для хостингов типа Render Web Service, которые ждут,
    что приложение слушает HTTP-порт, и по нему же (или по внешнему
    пингеру вроде UptimeRobot) определяют, что сервис "живой".
    Если переменная PORT не задана (обычный локальный запуск) —
    сервер просто не поднимается, ничего лишнего не открывается.
    """
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
    global xmpp_bridge

    healthcheck_runner = await _start_healthcheck_server()

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)

    if TELEGRAM_FORCE_IPV4:
        # local_address="0.0.0.0" заставляет httpx создавать только
        # IPv4-сокеты, что чинит зависания/таймауты через некоторые VPN.
        import httpx

        ipv4_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        forced_request = HTTPXRequest(httpx_kwargs={"transport": ipv4_transport})
        builder = builder.request(forced_request).get_updates_request(forced_request)
        logger.info("Принудительно использую IPv4 для запросов к Telegram (TELEGRAM_FORCE_IPV4=true)")

    telegram_app = builder.build()

    # /chatid работает всегда, в любом чате — не зависит от настройки моста
    telegram_app.add_handler(CommandHandler("chatid", tg_handle_chatid))

    if BRIDGE_ENABLED:
        telegram_app.add_handler(
            MessageHandler(filters.ALL & filters.Chat(chat_id=TELEGRAM_CHAT_ID), tg_handle_message)
        )
        xmpp_bridge = XMPPBridge(XMPP_JID, XMPP_PASSWORD, telegram_app)
        xmpp_bridge.connect()  # соединение устанавливается асинхронно на текущем event loop
    else:
        missing = []
        if not TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        if not XMPP_ENABLED:
            missing.extend(
                name for name, val in (
                    ("XMPP_JID", XMPP_JID),
                    ("XMPP_PASSWORD", XMPP_PASSWORD),
                    ("XMPP_PEER_JID", XMPP_PEER_JID),
                ) if not val
            )
        logger.warning(
            "Мост НЕ активен — не заданы: %s. "
            "Бот работает только в режиме /chatid.",
            ", ".join(missing),
        )

    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        if BRIDGE_ENABLED:
            logger.info("Telegram-бот запущен, жду сообщений в чате %s", TELEGRAM_CHAT_ID)
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
            if healthcheck_runner is not None:
                await healthcheck_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
