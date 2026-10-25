# Telegram ↔ XMPP ↔ Discord bridge

🇷🇺 [Русский](#русский) · 🇬🇧 [English](#english)

> The bot was written entirely with Claude for personal use and is shared in case someone needs a similar bridge.
> Бот полностью написан через Claude для личного использования; выложен на случай, если кому-то нужен такой мост.

## Files / Файлы

| File | Needed? |
|---|---|
| `bridge.py` | yes / да |
| `Dockerfile` | yes / да |
| `requirements.txt` | yes / да |
| `README.md` | no, docs only / нет, только описание |
| `.env.example` | no, a cheat sheet for variables / нет, шпаргалка по переменным |
| `generate_session.py` | only once, locally, for MTProto / один раз локально, для MTProto |

Never commit real tokens. Put them in Render's Environment. / Не коммить настоящие токены — только в Environment на Render.

---

## English

Messages, media, edits, deletions, reactions and replies are forwarded between a Telegram chat, an XMPP contact/room (MUC) and a Discord channel. XMPP and Discord are optional.

### Setup (all in the browser, no local install)

1. **Telegram**: create a bot with [@BotFather](https://t.me/BotFather), add it to your group and make it **admin** (needed to receive reactions). Start the bot without `TELEGRAM_CHAT_ID`, send `/chatid` in the group and copy the number.
2. **XMPP** (optional): register a separate account for the bot (e.g. on xmpp.jp). The server must support HTTP Upload. For a MUC, use the room JID (`room@conference.server`).
3. **Discord** (optional): [Developer Portal](https://discord.com/developers/applications) → New Application → Bot → copy the token and enable **Message Content Intent**. Invite with scopes `bot` + `applications.commands` and permissions: Send Messages, Read Message History, Add Reactions, Attach Files, Manage Messages. The channel is chosen in Discord by an admin with `/start` (or set `DISCORD_CHANNEL_ID`).
4. **Neon** (recommended): create a free project at [neon.com](https://neon.com), copy the connection string into `DATABASE_URL`. Tables are created automatically. It keeps message links across restarts, so replies, reactions and edits work on old messages. Without it everything lives in memory and is lost on restart.
5. **Render**: push `bridge.py`, `Dockerfile`, `requirements.txt` to a (private) GitHub repo → New **Web Service** → Docker, Free instance → add the variables below in **Environment**. Build/Start commands stay empty.
6. **UptimeRobot**: add an HTTP monitor for your Render URL, interval 5 min, so the free tier doesn't sleep.
7. **Instant deletion detection** (optional): Bot API cannot see deleted messages. Use a separate Telegram account: get `API_ID`/`API_HASH` at [my.telegram.org](https://my.telegram.org), run `pip install telethon && python generate_session.py` once locally, put the three values in Environment, and join that account to the bridged chat. The session string is as sensitive as a password.

### Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | required |
| `XMPP_JID`, `XMPP_PASSWORD`, `XMPP_PEER_JID` | XMPP account and target |
| `XMPP_IS_MUC`, `XMPP_MUC_NICK`, `XMPP_MUC_PASSWORD` | `true` if the target is a room; bot nick; room password |
| `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` | Discord |
| `DATABASE_URL` | Neon connection string |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_USERBOT_SESSION` | MTProto userbot |
| `EDIT_WINDOW_SECONDS` | how long edits are forwarded (default 300) |
| `MESSAGE_MAP_TTL_SECONDS` | how long message links are kept (default 180 days) |
| `RECENT_XMPP_TEXTS_LIMIT` | recent XMPP texts kept for quote matching (default 2000) |
| `TELEGRAM_DELETE_CHECK_INTERVAL` / `_BATCH` / `_DELAY` | polling check for deleted bot messages (default 300 s / 30 / 1.0 s) |
| `TELEGRAM_FORWARD_CHECK_ENABLED` / `_MIN_INTERVAL` | probing for deleted foreign messages (default `true` / 12 h) |
| `TELEGRAM_FORCE_IPV4` | `true` if the host has IPv6 problems |

### Limitations

- Files over 20 MB can't be downloaded by the Bot API. GIFs/videos may be rejected by some XMPP upload servers (e.g. xmpp.jp).
- Only plain-text edits are forwarded, not captions or files.
- XMPP replies/reactions/retractions depend on the client's XEP support (Converse works; Monocles renders them poorly).
- Polls are forwarded as a static text snapshot.
- Deleting a bridged copy in XMPP/Discord never deletes a human's original Telegram message.
- One instance = one chat set. Run several instances for several sets.

---

## Русский

Пересылает сообщения, медиа, правки, удаления, реакции и ответы между чатом Telegram, собеседником/комнатой XMPP (MUC) и каналом Discord. XMPP и Discord необязательны.

### Настройка (всё через браузер, локально ничего ставить не нужно)

1. **Telegram**: создай бота в [@BotFather](https://t.me/BotFather), добавь в группу и сделай **админом** (нужно для реакций). Запусти бота без `TELEGRAM_CHAT_ID`, напиши `/chatid` в группе и скопируй число.
2. **XMPP** (необязательно): заведи отдельный аккаунт для бота (например, на xmpp.jp). Сервер должен поддерживать HTTP Upload. Для MUC нужен JID комнаты (`room@conference.server`).
3. **Discord** (необязательно): [Developer Portal](https://discord.com/developers/applications) → New Application → Bot → скопируй токен и включи **Message Content Intent**. Приглашай со scope `bot` + `applications.commands` и правами: Send Messages, Read Message History, Add Reactions, Attach Files, Manage Messages. Канал выбирает админ в Discord командой `/start` (или задай `DISCORD_CHANNEL_ID`).
4. **Neon** (рекомендуется): создай бесплатный проект на [neon.com](https://neon.com), скопируй connection string в `DATABASE_URL`. Таблицы создаются сами. База хранит связки сообщений между перезапусками, поэтому ответы, реакции и правки работают и на старых сообщениях. Без неё всё хранится в памяти и теряется при перезапуске.
5. **Render**: залей `bridge.py`, `Dockerfile`, `requirements.txt` в (приватный) репозиторий GitHub → New **Web Service** → Docker, Free → впиши переменные ниже в **Environment**. Build/Start команды оставь пустыми.
6. **UptimeRobot**: добавь HTTP-монитор на адрес сервиса Render с интервалом 5 минут, чтобы бесплатный тариф не засыпал.
7. **Мгновенное обнаружение удалений** (необязательно): Bot API не видит удалённые сообщения. Нужен отдельный аккаунт Telegram: получи `API_ID`/`API_HASH` на [my.telegram.org](https://my.telegram.org), один раз локально выполни `pip install telethon && python generate_session.py`, впиши три значения в Environment и добавь этот аккаунт в мостимый чат. Строка сессии равносильна паролю.

### Переменные

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | обязательные |
| `XMPP_JID`, `XMPP_PASSWORD`, `XMPP_PEER_JID` | аккаунт XMPP и адресат |
| `XMPP_IS_MUC`, `XMPP_MUC_NICK`, `XMPP_MUC_PASSWORD` | `true`, если адресат — комната; ник бота; пароль комнаты |
| `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` | Discord |
| `DATABASE_URL` | connection string от Neon |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_USERBOT_SESSION` | MTProto-юзербот |
| `EDIT_WINDOW_SECONDS` | сколько пересылаются правки (по умолчанию 300) |
| `MESSAGE_MAP_TTL_SECONDS` | как долго помнить связки сообщений (по умолчанию 180 дней) |
| `RECENT_XMPP_TEXTS_LIMIT` | сколько последних XMPP-текстов держать для сопоставления цитат (по умолчанию 2000) |
| `TELEGRAM_DELETE_CHECK_INTERVAL` / `_BATCH` / `_DELAY` | проверка удаления сообщений бота опросом (по умолчанию 300 с / 30 / 1.0 с) |
| `TELEGRAM_FORWARD_CHECK_ENABLED` / `_MIN_INTERVAL` | проверка удаления чужих сообщений (по умолчанию `true` / 12 ч) |
| `TELEGRAM_FORCE_IPV4` | `true`, если у хостинга проблемы с IPv6 |

### Ограничения

- Файлы больше 20 МБ Bot API скачать не даёт. GIF/видео могут отклоняться некоторыми XMPP-серверами загрузки (например, xmpp.jp).
- Пересылаются правки только обычного текста, не подписей и не файлов.
- Ответы/реакции/удаления в XMPP зависят от поддержки XEP в клиенте (Converse работает, Monocles отображает плохо).
- Опросы пересылаются статичным текстом.
- Удаление пересланной копии в XMPP/Discord никогда не удаляет исходное сообщение человека в Telegram.
- Один экземпляр = один набор чатов. Для нескольких наборов запускай несколько экземпляров.
