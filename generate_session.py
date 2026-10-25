"""
Одноразовый скрипт для входа в отдельный Telegram-аккаунт (тот, что вы
завели специально под это) и получения "session string" — строки,
которая позволяет боту потом входить в аккаунт БЕЗ повторного ввода
номера телефона и кода на сервере (Render это не поддерживает —
там нет интерактивного терминала).

ЗАПУСКАТЬ ТОЛЬКО У СЕБЯ НА КОМПЬЮТЕРЕ, один раз. Полученную строку
сохраните в переменную окружения TELEGRAM_USERBOT_SESSION на Render —
и удалите/не публикуйте нигде больше (она равносильна паролю от
аккаунта).

Установка (один раз):
    pip install telethon

Запуск:
    python generate_session.py

Спросит:
  1. API_ID и API_HASH — получите на https://my.telegram.org
     (Log in → API development tools → создать приложение, вписав
     любое название).
  2. Номер телефона ТОГО САМОГО отдельного аккаунта (с кодом страны,
     например +79991234567).
  3. Код подтверждения, который придёт в само приложение Telegram
     (или SMS) на этот номер.
  4. Если на аккаунте включена двухфакторная аутентификация — ещё
     и пароль от неё.

В конце в терминал будет напечатана длинная строка — это и есть
TELEGRAM_USERBOT_SESSION.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID (число, с my.telegram.org): ").strip())
api_hash = input("API_HASH (строка, с my.telegram.org): ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()
    print("\n" + "=" * 70)
    print("Готово! Вот ваша session string — сохраните её как переменную")
    print("окружения TELEGRAM_USERBOT_SESSION на Render:\n")
    print(session_string)
    print("=" * 70)
    print(
        "\nВАЖНО: никому не показывайте эту строку — она даёт полный доступ "
        "к аккаунту, как будто кто-то вошёл в него напрямую."
    )
