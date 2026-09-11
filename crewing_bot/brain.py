"""Четыре вызова модели: Router, Structured Output, RAG-ответ, Judge.

У каждой функции есть параметр ask — так тесты подставляют свою
функцию и проверяют разбор ответа, не ходя в сеть.
"""

import json
import logging
import os

from openai import OpenAI

logger = logging.getLogger(__name__)

# Квота бесплатного тарифа Google считается по каждой модели отдельно и
# составляет 20 запросов в сутки. Лёгкая модель справляется с нашими задачами
# (распознать вопрос, вытащить поля из текста) и отвечает быстрее.
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class ModelUnavailable(str):
    """Ответ-заглушка: модель недоступна.

    Ведёт себя как обычная строка (её можно показать человеку), но
    вызывающий код отличает её от настоящего ответа через is_unavailable
    и не двигает воронку на сбое модели.
    """

    __slots__ = ()


class UnavailableFields(dict):
    """Пустой результат extract, помеченный как недоступность модели.

    Пустой словарь от модели («в сообщении ничего нет») и пустой словарь
    из-за сбоя — разные вещи, и app.py должен их различать.
    """

    __slots__ = ()


def is_unavailable(value) -> bool:
    """Ответ получен из-за сбоя модели, а не от самой модели?"""
    return isinstance(value, (ModelUnavailable, UnavailableFields))


def ask_model(prompt: str, system: str = "Ты — помощник крюингового агентства.") -> str:
    key = os.getenv("GOOGLE_API_KEY")
    if not key or "..." in key:
        logger.error("Модель недоступна: GOOGLE_API_KEY не задан или содержит плейсхолдер")
        return ModelUnavailable("⚠️ Нет GOOGLE_API_KEY в .env")
    try:
        client = OpenAI(api_key=key, base_url=BASE_URL)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content
        # Если модель вернула пустой ответ (None или пустую строку),
        # возвращаем помеченное предупреждение, а не None, чтобы не ронять
        # бота и чтобы вызывающий код увидел сбой, а не «ответ модели».
        if not content:
            return ModelUnavailable("⚠️ Модель вернула пустой ответ")
        return content
    except Exception as error:
        # Текст запроса в лог не пишем: там анкета кандидата.
        logger.exception("Модель не ответила (%s)", type(error).__name__)
        return ModelUnavailable(f"⚠️ Модель не отвечает: {error}")


# Слова, с которых начинается встречный вопрос кандидата. «есть» сюда не
# входит намеренно: «да, есть» — это ответ, а не вопрос.
QUESTION_WORDS = frozenset([
    'как', 'какая', 'какой', 'какие', 'каков', 'сколько', 'когда', 'где',
    'куда', 'откуда', 'почему', 'зачем', 'что', 'чем', 'кто', 'кому', 'кого',
    'можно', 'могу', 'нужно', 'нужен', 'нужна', 'надо', 'обязательно',
])

# Разговорные зачины: они ничего не значат, отбрасываем перед разбором.
FILLERS = frozenset(['а', 'и', 'но', 'скажите', 'подскажите', 'извините',
                     'простите', 'слушайте'])

def looks_like_question(text) -> bool:
    """Похоже ли сообщение на встречный вопрос — без обращения к модели.

    Раньше это решала модель, и стоило это отдельного запроса на каждое
    сообщение: половина времени ответа и половина суточной квоты. Знак вопроса и
    вопросительное слово в начале видны обычной проверкой текста.

    Цена ошибки известна: вопрос без знака и без вопросительного слова
    будет принят за ответ и попадёт в поле анкеты. Там его подхватит
    существующая страховка — сырой текст уходит в примечания рекрутеру.
    """
    if not isinstance(text, str):
        return False
    cleaned = text.strip().lower()
    if not cleaned:
        return False
    if "?" in cleaned:
        return True

    words = [w.strip(".,!:;()\"'") for w in cleaned.split()]
    while words and words[0] in FILLERS:
        words.pop(0)
    if not words:
        return False
    if words[0] in QUESTION_WORDS:
        return True
    # «есть ли», «нужно ли», «можно ли» — частица сразу после первого слова
    return "ли" in words[1:3]


def extract(message: str, fields: list, *, ask=ask_model) -> dict:
    """Structured Output: свободный текст → словарь только запрошенных полей."""
    reply = ask(
        "Извлеки из сообщения перечисленные поля. Верни СТРОГО JSON-объект, "
        "только его, без пояснений. Чего нет в сообщении — не включай.\n\n"
        f"Поля: {', '.join(fields)}\nСообщение: {message}"
    )
    if is_unavailable(reply):
        return UnavailableFields()
    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        parsed = json.loads(reply[start:end + 1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {name: value for name, value in parsed.items() if name in fields}


def answer(message: str, knowledge: str, vacancy_text: str, *, ask=ask_model) -> str:
    """RAG: ответ на встречный вопрос строго по фактам агентства и вакансии."""
    return ask(
        "Ответь кратко на вопрос кандидата ТОЛЬКО по фактам ниже. "
        "Факта нет — честно скажи, что уточнит менеджер. "
        "Отвечай на языке вопроса.\n\n"
        f"ФАКТЫ АГЕНТСТВА:\n{knowledge}\n\nВАКАНСИЯ:\n{vacancy_text}\n\n"
        f"Вопрос: {message}"
    )


# Персональные данные, которые для оценки кандидата под вакансию не нужны,
# а значит и уходить в промпт модели не должны.
PERSONAL_FIELDS = ("full_name", "contact")


def verdict(vacancy: dict, profile: dict, screening: dict, *, ask=ask_model) -> str:
    """Judge: короткая оценка кандидата для рекрутера. Решение принимает человек.

    ФИО и контакт в промпт не попадают: для сравнения опыта с требованиями
    вакансии они бесполезны, а отдавать их модели незачем.
    """
    impersonal = {
        name: value for name, value in profile.items() if name not in PERSONAL_FIELDS
    }
    return ask(
        "Оцени кандидата под вакансию для крюинг-менеджера. Два-три предложения: "
        "подходит или нет и почему. Не отказывай кандидату — это заметка "
        "для менеджера, решение принимает он.\n\n"
        f"ВАКАНСИЯ: {vacancy.get('rank')} на {vacancy.get('vessel_type')}. "
        f"Требования: {vacancy.get('requirements')}\n\n"
        f"АНКЕТА: {json.dumps(impersonal, ensure_ascii=False)}\n\n"
        f"СКРИНИНГ: {json.dumps(screening, ensure_ascii=False)}"
    )
