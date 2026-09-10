"""Отправка карточки заявки кандидату в Telegram.

Telegram-бот не может написать человеку первым: пока кандидат не нажал
Start, у нас нет его chat_id. Поэтому бот показывает ссылку с кодом
заявки, а этот модуль обрабатывает нажатие и отправляет карточку.

Разбор апдейта и сборка карточки — чистые функции: ни сети, ни базы.
"""

import json
import os
import urllib.request

API = "https://api.telegram.org/bot{token}/sendMessage"

MANAGER_LINE = "По вопросам пишите менеджеру агентства на почту, указанную в вакансии."


def is_configured() -> bool:
    """Настроен ли Telegram. Без этого фича просто выключена."""
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_BOT_USERNAME"))


def deep_link(token: str):
    """Ссылка вида https://t.me/бот?start=код. None, если Telegram не настроен."""
    if not token or not is_configured():
        return None
    return f"https://t.me/{os.environ['TELEGRAM_BOT_USERNAME']}?start={token}"


def parse_start(update: dict):
    """Достать (chat_id, код) из апдейта Telegram.

    None означает «нас это не касается»: не сообщение, не текст, не
    /start или /start без кода. Такие апдейты просто игнорируются.
    """
    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not isinstance(text, str) or chat_id is None:
        return None
    parts = text.split()
    if len(parts) != 2 or parts[0] != "/start":
        return None
    return chat_id, parts[1]


def build_card(application: dict) -> str:
    """Текст карточки для кандидата.

    Вердикта модели здесь нет и быть не должно: это внутренняя заметка
    рекрутера, а не то, что показывают человеку.
    """
    starts_at = application["starts_at"]
    when = starts_at.strftime("%d.%m в %H:%M UTC")
    return (
        "✅ Вы записаны на интервью\n\n"
        f"Вакансия: {application['rank']} — {application['vessel_type']}\n"
        f"Когда: {when}\n\n"
        f"Кандидат: {application['full_name']}\n"
        f"Контакт: {application['contact']}\n"
        f"Готов с: {application['readiness_date']}\n\n"
        f"{MANAGER_LINE}"
    )


def send_message(chat_id: int, text: str, *, opener=None) -> bool:
    """Отправить сообщение в Telegram. False при любой неудаче.

    Исключение наружу не летит: карточка — приятное дополнение, а бронь
    к этому моменту уже сохранена, и ронять из-за неё ничего нельзя.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        API.format(token=token),
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception:
        return False
