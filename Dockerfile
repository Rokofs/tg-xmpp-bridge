# Единый образ для Telegram/XMPP/Discord-моста (Python) и AFK-бота
# Minecraft (Node.js/mineflayer) — запускаются в одном Render-сервисе,
# чтобы не делить лимит бесплатных часов между двумя сервисами.

FROM python:3.13-slim

# Устанавливаем Node.js (нужен только для AFK-бота Minecraft — если
# он не используется, эти пакеты просто не понадобятся во время
# исполнения, но всё равно должны быть в образе на случай, если он
# понадобится позже).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python-зависимости ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Node-зависимости AFK-бота ---
COPY minecraft_afk/package.json minecraft_afk/package-lock.json* ./minecraft_afk/
RUN cd minecraft_afk && npm install --omit=dev

# --- Остальной код ---
COPY . .

CMD ["python", "bridge.py"]
