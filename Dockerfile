# Образ для Telegram/XMPP/Discord-моста.
# ffmpeg нужен для конвертации "гифок" (Telegram хранит их как .mp4) —
# запасной способ загрузки в XMPP, если сервер отклоняет исходный формат.

FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bridge.py"]
