"""Telegram: карточка заявки кандидату и объявления в канале.

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
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
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
    try:
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(
            API.format(token=token),
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        send = opener or urllib.request.urlopen
        with send(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


CHANNEL_API = "https://api.telegram.org/bot{token}/{method}"


def channel_configured() -> bool:
    """Настроен ли канал. Без адреса канала публикация просто выключена."""
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHANNEL"))


def build_vacancy_post(vacancy: dict, site_url: str = "") -> str:
    """Текст объявления для канала.

    Требования и вопросы скрининга сюда не идут: в канале нужен повод
    открыть сайт, а подробности человек читает в карточке.
    """
    lines = [
        f"⚓ {vacancy['rank']} — {vacancy['vessel_type']}",
        "",
        f"💵 ${vacancy['salary_usd']} / мес",
        f"📆 Контракт {vacancy['contract_months']} мес",
    ]
    requirements = (vacancy.get("requirements") or "").strip()
    if requirements:
        lines += ["", f"Требования: {requirements}"]
    if not vacancy.get("is_active", True):
        lines = ["🚫 Вакансия закрыта", ""] + lines
    if site_url:
        lines += ["", f"Записаться на интервью: {site_url}"]
    return "\n".join(lines)


def _call(method: str, payload: dict, *, opener=None):
    """Вызов Telegram. None при любой неудаче.

    Исключение наружу не летит: публикация в канал — дополнение к работе
    сайта, и ронять из-за неё сохранение вакансии нельзя.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        request = urllib.request.Request(
            CHANNEL_API.format(token=token, method=method),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        send = opener or urllib.request.urlopen
        with send(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                return None
            answer = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return answer.get("result") if answer.get("ok") else None


def publish_vacancy(vacancy: dict, photo_url: str = "", site_url: str = "",
                    *, opener=None):
    """Опубликовать вакансию в канале. Возвращает номер сообщения или None.

    Со снимком судна, если он доступен по сети: канал листают глазами, и
    объявление без картинки там теряется.
    """
    if not channel_configured():
        return None

    channel = os.environ["TELEGRAM_CHANNEL"]
    caption = build_vacancy_post(vacancy, site_url)
    if photo_url:
        result = _call("sendPhoto",
                       {"chat_id": channel, "photo": photo_url,
                        "caption": caption},
                       opener=opener)
        if result:
            return result.get("message_id")
    result = _call("sendMessage", {"chat_id": channel, "text": caption},
                   opener=opener)
    return result.get("message_id") if result else None


def update_vacancy_post(message_id: int, vacancy: dict, site_url: str = "",
                        *, opener=None) -> bool:
    """Переписать уже опубликованное объявление.

    Так закрытая вакансия помечается прямо в канале, а не удаляется:
    ссылку на пост могли сохранить или переслать.
    """
    if not channel_configured() or not message_id:
        return False

    channel = os.environ["TELEGRAM_CHANNEL"]
    caption = build_vacancy_post(vacancy, site_url)
    payload = {"chat_id": channel, "message_id": int(message_id),
               "caption": caption}
    if _call("editMessageCaption", payload, opener=opener) is not None:
        return True
    # Объявление без снимка правится другим методом.
    payload = {"chat_id": channel, "message_id": int(message_id),
               "text": caption}
    return _call("editMessageText", payload, opener=opener) is not None
