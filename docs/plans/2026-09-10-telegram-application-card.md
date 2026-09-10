# Карточка заявки в Telegram — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После брони слота бот показывает кандидату ссылку на Telegram; кандидат жмёт Start и получает карточку своей заявки.

**Architecture:** Telegram не может написать первым, поэтому кандидат сам открывает разговор по ссылке с одноразовым кодом. Код рождается вместе с заявкой в базе. Приложение становится веб-приложением с двумя адресами: `/` отдаёт чат Gradio, `/telegram/webhook` принимает сообщения от Telegram. Разбор входящего сообщения и сборка карточки — чистые функции без сети и базы.

**Tech Stack:** Python 3.12, Gradio 6.26, FastAPI + uvicorn (приходят вместе с Gradio), psycopg2, `urllib.request` из стандартной библиотеки для запросов к Telegram, pytest.

**Spec:** `docs/specs/2026-09-10-telegram-application-card-design.md`

## Global Constraints

- Рабочая папка: `C:\Users\Admin\Desktop\crewing-bot`. Python окружения: `C:/Users/Admin/Desktop/Course Materials AI Development/02-AI Agents Workbook/.venv/Scripts/python.exe`
- **Консоль Windows в cp1252 и падает с `UnicodeEncodeError` на кириллице.** Каждую команду запускать с `PYTHONIOENCODING=utf-8`.
- **Тесты базы идут только по `TEST_DATABASE_URL`** и делают `TRUNCATE`. Без переменной пропускаются; совпадать с `DATABASE_URL` ей нельзя. Для прогона использовать ту же базу с другой строкой:
  `PYTHONIOENCODING=utf-8 TEST_DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)?application_name=crewing_tests" <python> -m pytest crewing_bot/tests -q`
- Вся SQL параметризована через `%s`. Никаких f-строк в запросах.
- Никаких новых зависимостей: FastAPI и uvicorn уже стоят вместе с Gradio, HTTP-запрос — через `urllib.request`.
- Файлы в UTF-8, комментарии и docstring на русском.
- Секретов в коде нет. Токен бота, имя бота и секрет вебхука читаются из переменных окружения.
- **Фича необязательная:** без `TELEGRAM_BOT_TOKEN` бот работает ровно как раньше, ссылка не показывается, ничего не падает.
- Никогда не использовать `git add -A` или `git add .` — добавлять только названные файлы.
- Файл `.env` не трогать и не коммитить.
- Сообщения коммитов на русском, каждое заканчивается строкой `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Сейчас в наборе 77 тестов. После каждой задачи их становится больше — точное число указано в шагах.

---

## Структура файлов

| Файл | Ответственность |
|---|---|
| `crewing_bot/telegram.py` | Новый. Разбор входящего апдейта, сборка карточки, отправка в Telegram, сборка ссылки |
| `crewing_bot/db.py` | Миграция двух колонок, код при создании заявки, поиск по коду, привязка `chat_id` |
| `crewing_bot/app.py` | Ссылка после брони, обработчик вебхука, новая точка входа |
| `crewing_bot/tests/test_telegram.py` | Новый. Чистые функции: разбор и карточка |
| `crewing_bot/tests/test_db.py` | Код заявки, поиск, привязка |
| `crewing_bot/tests/test_app.py` | Ссылка в подтверждении, поведение вебхука |
| `README.md`, `render.yaml`, `.env.example` | Переменные и инструкция по настройке бота |

---

### Task 1: Колонки в базе и код вместе с заявкой

**Files:**
- Modify: `crewing_bot/db.py`, `crewing_bot/tests/test_db.py`

**Interfaces:**
- Consumes: существующие `init_schema(conn, force=False)`, `create_application(conn, candidate_id, vacancy_id, slot_id, profile, screening, notes="")`
- Produces:
  - `COLUMN_MIGRATIONS: tuple[str, ...]` — ALTER-ы, выполняются в `init_schema` сразу после `SCHEMA`
  - `create_application(...)` — прежняя сигнатура, но дополнительно кладёт случайный код в `telegram_token`
  - `application_token(conn, application_id: int) -> str | None`

- [ ] **Step 1: Написать падающие тесты**

Дописать в `crewing_bot/tests/test_db.py`:

```python
def test_application_gets_telegram_token(conn):
    vacancy_id = _vacancy(conn)
    slot_id = _future_slots(conn)[0]
    candidate_id = db.upsert_candidate(conn, "Petrov Petr", "petrov@example.com", "Ukraine")
    db.book_slot(conn, slot_id)
    application_id = db.create_application(
        conn, candidate_id, vacancy_id, slot_id, _profile(), {"STCW?": "да"}
    )

    token = db.application_token(conn, application_id)
    assert token
    assert len(token) >= 16


def test_tokens_of_two_applications_differ(conn):
    vacancy_id = _vacancy(conn)
    slots = _future_slots(conn, 2)
    first_candidate = db.upsert_candidate(conn, "A A", "a@example.com", "Ukraine")
    second_candidate = db.upsert_candidate(conn, "B B", "b@example.com", "Ukraine")
    db.book_slot(conn, slots[0])
    db.book_slot(conn, slots[1])
    first = db.create_application(conn, first_candidate, vacancy_id, slots[0], _profile(), {})
    second = db.create_application(conn, second_candidate, vacancy_id, slots[1], _profile(), {})

    assert db.application_token(conn, first) != db.application_token(conn, second)


def test_application_token_of_unknown_id_is_none(conn):
    assert db.application_token(conn, 999999) is None
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Команда прогона — из Global Constraints, с добавлением `-k telegram_token or tokens_of_two`.
Ожидается: FAIL, `AttributeError: module ... has no attribute 'application_token'`

- [ ] **Step 3: Написать реализацию**

В начало `crewing_bot/db.py`, к остальным импортам, добавить:

```python
import secrets
```

После константы `CONTACT_INDEX` добавить:

```python
# Колонки для Telegram добавляются отдельно от SCHEMA: таблица applications
# у работающих проектов уже создана, а CREATE TABLE IF NOT EXISTS её не
# меняет. UNIQUE по nullable-колонке в Postgres допускает сколько угодно
# NULL — заявки, созданные до появления фичи, останутся без кода.
COLUMN_MIGRATIONS = (
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS telegram_token TEXT",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT",
    "CREATE UNIQUE INDEX IF NOT EXISTS applications_telegram_token"
    " ON applications (telegram_token)",
)
```

В `init_schema`, внутри блока `with conn, conn.cursor() as cur:`, сразу после `cur.execute(SCHEMA)` добавить:

```python
        for statement in COLUMN_MIGRATIONS:
            cur.execute(statement)
```

В `create_application` заменить запрос на вариант с кодом. Полностью новое тело функции:

```python
def create_application(conn, candidate_id: int, vacancy_id: int, slot_id: int,
                       profile: dict, screening: dict, notes: str = "") -> int:
    """Сохранить заявку. Вердикт дописывается позже отдельным запросом:
    отказ модели на последнем шаге не должен стоить кандидату брони.

    Код для ссылки в Telegram рождается здесь же, одной операцией с
    заявкой: если заявка есть — код у неё есть всегда.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applications (candidate_id, vacancy_id, slot_id,"
            " rank_experience_months, total_experience_months, vessel_types,"
            " readiness_date, screening, notes, telegram_token)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " RETURNING id",
            (
                candidate_id, vacancy_id, slot_id,
                _as_int(profile.get("rank_experience_months")),
                _as_int(profile.get("total_experience_months")),
                profile.get("vessel_types"),
                profile.get("readiness_date"),
                json.dumps(screening, ensure_ascii=False),
                notes,
                secrets.token_urlsafe(16),
            ),
        )
        return cur.fetchone()[0]


def application_token(conn, application_id: int):
    """Код заявки для ссылки в Telegram. None, если заявки нет."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_token FROM applications WHERE id = %s",
            (application_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None
```

- [ ] **Step 4: Запустить весь набор**

Ожидается: 80 passed, ни одного SKIPPED.

- [ ] **Step 5: Коммит**

```bash
git add crewing_bot/db.py crewing_bot/tests/test_db.py
git commit -m "feat: код для ссылки в Telegram рождается вместе с заявкой

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Поиск заявки по коду и привязка чата

**Files:**
- Modify: `crewing_bot/db.py`, `crewing_bot/tests/test_db.py`

**Interfaces:**
- Consumes: всё из Task 1
- Produces:
  - `find_application_by_token(conn, token: str) -> dict | None` — ключи `id, telegram_chat_id, full_name, contact, readiness_date, rank, vessel_type, starts_at`
  - `bind_telegram_chat(conn, application_id: int, chat_id: int) -> bool` — `True`, если чат привязан сейчас; `False`, если уже был привязан

- [ ] **Step 1: Написать падающие тесты**

Дописать в `crewing_bot/tests/test_db.py`:

```python
def _application_with_token(conn, contact="petrov@example.com"):
    vacancy_id = _vacancy(conn)
    slot_id = _future_slots(conn)[0]
    candidate_id = db.upsert_candidate(conn, "Petrov Petr", contact, "Ukraine")
    db.book_slot(conn, slot_id)
    application_id = db.create_application(
        conn, candidate_id, vacancy_id, slot_id, _profile(contact=contact), {}
    )
    return application_id, db.application_token(conn, application_id)


def test_find_application_by_token_returns_card_data(conn):
    application_id, token = _application_with_token(conn)

    found = db.find_application_by_token(conn, token)
    assert found is not None
    assert found["id"] == application_id
    assert found["full_name"] == "Petrov Petr"
    assert found["rank"] == "AB"
    assert found["starts_at"] is not None
    assert found["telegram_chat_id"] is None


def test_unknown_token_finds_nothing(conn):
    _application_with_token(conn)
    assert db.find_application_by_token(conn, "нет-такого-кода") is None


def test_first_chat_is_bound_and_second_is_refused(conn):
    application_id, _ = _application_with_token(conn)

    assert db.bind_telegram_chat(conn, application_id, 111) is True
    assert db.bind_telegram_chat(conn, application_id, 222) is False

    with conn, conn.cursor() as cur:
        cur.execute("SELECT telegram_chat_id FROM applications WHERE id = %s", (application_id,))
        assert cur.fetchone()[0] == 111
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Ожидается: FAIL, `AttributeError: module ... has no attribute 'find_application_by_token'`

- [ ] **Step 3: Написать реализацию**

Добавить в `crewing_bot/db.py`:

```python
def find_application_by_token(conn, token: str):
    """Заявка по коду из ссылки. None, если код неизвестен.

    Отдаёт ровно то, что нужно для карточки кандидату. Вердикта модели
    здесь намеренно нет: это внутренняя заметка рекрутера.
    """
    if not token:
        return None
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT a.id, a.telegram_chat_id, a.readiness_date,"
            " c.full_name, c.contact, v.rank, v.vessel_type, s.starts_at"
            " FROM applications a"
            " JOIN candidates c ON c.id = a.candidate_id"
            " JOIN vacancies v ON v.id = a.vacancy_id"
            " JOIN slots s ON s.id = a.slot_id"
            " WHERE a.telegram_token = %s",
            (token,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def bind_telegram_chat(conn, application_id: int, chat_id: int) -> bool:
    """Привязать чат к заявке. False означает, что чат уже был привязан.

    Условие telegram_chat_id IS NULL прямо в UPDATE: первый, кто открыл
    ссылку, становится её владельцем, и подменить его нельзя.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE applications SET telegram_chat_id = %s"
            " WHERE id = %s AND telegram_chat_id IS NULL RETURNING id",
            (chat_id, application_id),
        )
        return cur.fetchone() is not None
```

- [ ] **Step 4: Запустить весь набор**

Ожидается: 83 passed.

- [ ] **Step 5: Коммит**

```bash
git add crewing_bot/db.py crewing_bot/tests/test_db.py
git commit -m "feat: поиск заявки по коду и привязка чата Telegram

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Модуль Telegram — разбор, карточка, отправка

**Files:**
- Create: `crewing_bot/telegram.py`, `crewing_bot/tests/test_telegram.py`

**Interfaces:**
- Consumes: ничего из проекта
- Produces:
  - `parse_start(update: dict) -> tuple[int, str] | None` — `(chat_id, код)` или `None`
  - `build_card(application: dict) -> str`
  - `deep_link(token: str) -> str | None` — `None`, если не заданы переменные окружения
  - `is_configured() -> bool`
  - `send_message(chat_id: int, text: str, *, opener=None) -> bool`
  - `MANAGER_LINE: str`

- [ ] **Step 1: Написать падающие тесты**

`crewing_bot/tests/test_telegram.py`:

```python
from datetime import datetime, timezone

from crewing_bot import telegram


def _application():
    return {
        "id": 1,
        "telegram_chat_id": None,
        "readiness_date": "2026-10-01",
        "full_name": "Petrov Petr",
        "contact": "petrov@example.com",
        "rank": "2nd Engineer",
        "vessel_type": "bulk carrier",
        "starts_at": datetime(2026, 9, 11, 10, 30, tzinfo=timezone.utc),
    }


def test_parse_start_takes_chat_and_token():
    update = {"message": {"chat": {"id": 555}, "text": "/start abc123"}}
    assert telegram.parse_start(update) == (555, "abc123")


def test_parse_start_without_token_is_ignored():
    assert telegram.parse_start({"message": {"chat": {"id": 555}, "text": "/start"}}) is None


def test_parse_start_ignores_other_messages():
    assert telegram.parse_start({"message": {"chat": {"id": 5}, "text": "привет"}}) is None
    assert telegram.parse_start({"message": {"chat": {"id": 5}}}) is None
    assert telegram.parse_start({"edited_message": {"text": "/start x"}}) is None
    assert telegram.parse_start({}) is None


def test_card_has_interview_time_in_utc_and_manager_line():
    card = telegram.build_card(_application())
    assert "11.09" in card
    assert "10:30" in card
    assert "UTC" in card
    assert telegram.MANAGER_LINE in card


def test_card_has_candidate_data():
    card = telegram.build_card(_application())
    assert "Petrov Petr" in card
    assert "2nd Engineer" in card
    assert "bulk carrier" in card


def test_card_never_contains_verdict():
    application = _application()
    application["verdict"] = "Не подходит: мало опыта"
    card = telegram.build_card(application)
    assert "подходит" not in card.lower()
    assert "verdict" not in card.lower()


def test_deep_link_is_none_without_settings(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    assert telegram.deep_link("abc") is None
    assert telegram.is_configured() is False


def test_deep_link_uses_bot_username(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "crewing_meridian_bot")
    assert telegram.deep_link("abc123") == "https://t.me/crewing_meridian_bot?start=abc123"
    assert telegram.is_configured() is True


def test_send_message_reports_failure_instead_of_raising(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")

    def broken_opener(request, timeout=None):
        raise OSError("сеть недоступна")

    assert telegram.send_message(555, "текст", opener=broken_opener) is False
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Ожидается: FAIL, `ModuleNotFoundError: No module named 'crewing_bot.telegram'`

- [ ] **Step 3: Написать реализацию**

`crewing_bot/telegram.py`:

```python
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
```

- [ ] **Step 4: Запустить весь набор**

Ожидается: 92 passed.

- [ ] **Step 5: Коммит**

```bash
git add crewing_bot/telegram.py crewing_bot/tests/test_telegram.py
git commit -m "feat: модуль Telegram — разбор апдейта, карточка, отправка

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Ссылка в подтверждении брони

**Files:**
- Modify: `crewing_bot/app.py`, `crewing_bot/tests/test_app.py`

**Interfaces:**
- Consumes: `db.application_token`, `telegram.deep_link`
- Produces: подтверждение брони, дополненное ссылкой, когда Telegram настроен

- [ ] **Step 1: Написать падающие тесты**

Дописать в `crewing_bot/tests/test_app.py` (фикстуры и заглушки — как в соседних тестах этого файла):

```python
def test_confirmation_has_telegram_link_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "crewing_test_bot")
    reply = _run_funnel_to_booking(monkeypatch)
    assert "t.me/crewing_test_bot?start=" in reply


def test_confirmation_has_no_link_when_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    reply = _run_funnel_to_booking(monkeypatch)
    assert "t.me" not in reply
    assert "Записал вас на интервью" in reply
```

Вспомогательная функция `_run_funnel_to_booking(monkeypatch)` доводит воронку до подтверждения и возвращает текст последнего ответа. Не пиши её с нуля: в этом файле уже есть тест `test_happy_path_confirms_booking(bot)` (около строки 190), который проходит воронку целиком на фикстуре `bot`. Вынеси его тело в функцию, а сам тест оставь работающим через неё.

Заглушке базы в фикстуре `bot` нужно добавить метод `application_token(self, conn, application_id)`, возвращающий строку `TESTTOKEN123456` — иначе новый код в подтверждении не найдёт код заявки.

- [ ] **Step 2: Запустить и убедиться, что падают**

Ожидается: FAIL — в подтверждении нет ссылки.

- [ ] **Step 3: Написать реализацию**

В `crewing_bot/app.py` к импортам модулей проекта добавить `telegram`:

```python
from crewing_bot import brain, db, funnel, telegram
```

Найти в `_handle_slot_choice` формирование подтверждения (строки со словами «Записал вас на интервью») и заменить возврат на:

```python
    confirmation = (f"✅ Записал вас на интервью {when_text}. "
                    "Менеджер свяжется с вами по указанному контакту.")

    # Ссылка на Telegram — необязательное дополнение. Не настроен бот или
    # код почему-то не достался — просто подтверждаем бронь без ссылки.
    try:
        link = telegram.deep_link(db.application_token(conn, application_id))
    except Exception:
        link = None
    if link:
        confirmation += (
            "\n\nХотите получить эту заявку в Telegram? "
            f"Откройте ссылку и нажмите «Начать»:\n{link}"
        )

    chosen = funnel.confirm(chosen)
    return confirmation, vars(chosen)
```

Имя переменной с идентификатором заявки взять то, которое уже используется в этой функции при вызове `db.create_application`.

- [ ] **Step 4: Запустить весь набор**

Ожидается: 94 passed.

- [ ] **Step 5: Коммит**

```bash
git add crewing_bot/app.py crewing_bot/tests/test_app.py
git commit -m "feat: ссылка на Telegram в подтверждении брони

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Вебхук и новая точка входа

**Files:**
- Modify: `crewing_bot/app.py`, `crewing_bot/tests/test_app.py`

**Interfaces:**
- Consumes: `telegram.parse_start`, `telegram.build_card`, `telegram.send_message`, `db.find_application_by_token`, `db.bind_telegram_chat`
- Produces:
  - `handle_telegram_update(update: dict, *, conn) -> str` — решение словом: `"sent"`, `"foreign"`, `"unknown"`, `"ignored"`
  - `build_app()` — веб-приложение с Gradio на `/` и вебхуком на `/telegram/webhook`

- [ ] **Step 1: Написать падающие тесты**

Дописать в `crewing_bot/tests/test_app.py`:

```python
class _TelegramFakeDB:
    def __init__(self, application):
        self.application = application
        self.bound = []
        self.sent = []

    def find_application_by_token(self, conn, token):
        return self.application if token == "GOODTOKEN" else None

    def bind_telegram_chat(self, conn, application_id, chat_id):
        already = self.application.get("telegram_chat_id")
        if already is None:
            self.application["telegram_chat_id"] = chat_id
            self.bound.append(chat_id)
            return True
        return False


def _telegram_application():
    from datetime import datetime, timezone
    return {
        "id": 7, "telegram_chat_id": None, "readiness_date": "2026-10-01",
        "full_name": "Petrov Petr", "contact": "petrov@example.com",
        "rank": "AB", "vessel_type": "container",
        "starts_at": datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc),
    }


def _patch_telegram(monkeypatch, fake_db, sent):
    monkeypatch.setattr(app.db, "find_application_by_token", fake_db.find_application_by_token)
    monkeypatch.setattr(app.db, "bind_telegram_chat", fake_db.bind_telegram_chat)
    monkeypatch.setattr(app.telegram, "send_message", lambda chat_id, text, **kw: sent.append((chat_id, text)) or True)


def test_start_with_valid_token_sends_card(monkeypatch):
    fake_db, sent = _TelegramFakeDB(_telegram_application()), []
    _patch_telegram(monkeypatch, fake_db, sent)

    update = {"message": {"chat": {"id": 555}, "text": "/start GOODTOKEN"}}
    assert app.handle_telegram_update(update, conn=None) == "sent"
    assert sent and sent[0][0] == 555
    assert "Petrov Petr" in sent[0][1]


def test_same_chat_gets_card_again(monkeypatch):
    application = _telegram_application()
    application["telegram_chat_id"] = 555
    fake_db, sent = _TelegramFakeDB(application), []
    _patch_telegram(monkeypatch, fake_db, sent)

    update = {"message": {"chat": {"id": 555}, "text": "/start GOODTOKEN"}}
    assert app.handle_telegram_update(update, conn=None) == "sent"
    assert len(sent) == 1


def test_foreign_chat_gets_no_card(monkeypatch):
    application = _telegram_application()
    application["telegram_chat_id"] = 111
    fake_db, sent = _TelegramFakeDB(application), []
    _patch_telegram(monkeypatch, fake_db, sent)

    update = {"message": {"chat": {"id": 999}, "text": "/start GOODTOKEN"}}
    assert app.handle_telegram_update(update, conn=None) == "foreign"
    assert sent == []


def test_unknown_token_sends_nothing_useful(monkeypatch):
    fake_db, sent = _TelegramFakeDB(_telegram_application()), []
    _patch_telegram(monkeypatch, fake_db, sent)

    update = {"message": {"chat": {"id": 555}, "text": "/start НЕТТАКОГО"}}
    assert app.handle_telegram_update(update, conn=None) == "unknown"
    assert all("Petrov" not in text for _, text in sent)


def test_unrelated_update_is_ignored(monkeypatch):
    fake_db, sent = _TelegramFakeDB(_telegram_application()), []
    _patch_telegram(monkeypatch, fake_db, sent)

    assert app.handle_telegram_update({"message": {"chat": {"id": 5}, "text": "привет"}}, conn=None) == "ignored"
    assert sent == []
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Ожидается: FAIL, `AttributeError: module ... has no attribute 'handle_telegram_update'`

- [ ] **Step 3: Написать реализацию**

Добавить в `crewing_bot/app.py`:

```python
def handle_telegram_update(update: dict, *, conn) -> str:
    """Обработать апдейт от Telegram. Возвращает слово-решение.

    Разделено с транспортом нарочно: вся логика проверяется тестами без
    сети и без веб-сервера.
    """
    parsed = telegram.parse_start(update)
    if not parsed:
        return "ignored"
    chat_id, token = parsed

    application = db.find_application_by_token(conn, token)
    if not application:
        telegram.send_message(
            chat_id,
            "Ссылка не найдена или устарела. Запишитесь на интервью заново.",
        )
        return "unknown"

    owner = application.get("telegram_chat_id")
    if owner is None:
        db.bind_telegram_chat(conn, application["id"], chat_id)
    elif owner != chat_id:
        # Ссылку могли переслать или заскринить. Чужому её содержимое
        # не показываем: там ФИО и контакт живого человека.
        telegram.send_message(
            chat_id,
            "Эта ссылка выдана другому кандидату. Запишитесь на интервью сами — "
            "и получите свою заявку.",
        )
        return "foreign"

    telegram.send_message(chat_id, telegram.build_card(application))
    return "sent"
```

И заменить блок запуска в конце файла на:

```python
def build_app():
    """Веб-приложение: чат Gradio на / и приём сообщений Telegram.

    Telegram умеет только вебхук на публичный адрес. Опрос (polling) на
    бесплатном хостинге не годится: пока сервис спит, опрашивать некому,
    и нажатие Start потерялось бы навсегда. Запрос вебхука сервис будит.
    """
    import gradio as gr
    from fastapi import FastAPI, Request, Response

    api = FastAPI()

    @api.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
        header = request.headers.get("x-telegram-bot-api-secret-token")
        if not secret or header != secret:
            # Адрес публичный. Без этой проверки любой мог бы слать
            # поддельные апдейты и подбирать чужие коды заявок.
            return Response(status_code=403)

        try:
            update = await request.json()
        except Exception:
            return {"ok": True}

        conn = None
        try:
            conn = db.connect()
            db.init_schema(conn)
            handle_telegram_update(update, conn=conn)
        except Exception:
            # Отвечаем 200 в любом случае: иначе Telegram будет
            # повторять доставку часами.
            pass
        finally:
            if conn is not None:
                conn.close()
        return {"ok": True}

    return gr.mount_gradio_app(api, build_ui(), path="/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        build_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 7861)),
    )
```

- [ ] **Step 4: Проверить сборку приложения и весь набор**

```bash
PYTHONIOENCODING=utf-8 <python> -c "from crewing_bot import app; a=app.build_app(); print('app ok', type(a).__name__)"
```
Ожидается: `app ok FastAPI`

Затем весь набор — ожидается 99 passed.

- [ ] **Step 5: Коммит**

```bash
git add crewing_bot/app.py crewing_bot/tests/test_app.py
git commit -m "feat: вебхук Telegram и веб-приложение с двумя адресами

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Переменные, документация, деплой

**Files:**
- Modify: `README.md`, `render.yaml`, `.env.example`

**Interfaces:**
- Consumes: готовую фичу из задач 1–5
- Produces: инструкцию, по которой человек заводит бота и включает фичу

- [ ] **Step 1: Дописать переменные в `.env.example`**

```
# 📨 Telegram: карточка заявки кандидату. Необязательно — без этих
# переменных бот работает как обычно, ссылка просто не показывается.
# Бот заводится у @BotFather командой /newbot.
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
# Произвольная случайная строка: ею подписываются входящие запросы от Telegram.
TELEGRAM_WEBHOOK_SECRET=
```

- [ ] **Step 2: Дописать три переменные в `render.yaml`**

В `envVars` сервиса `crewing-ai`, к существующим:

```yaml
      - key: TELEGRAM_BOT_TOKEN
        sync: false
      - key: TELEGRAM_BOT_USERNAME
        sync: false
      - key: TELEGRAM_WEBHOOK_SECRET
        sync: false
```

- [ ] **Step 3: Дописать раздел в `README.md`**

```markdown
## Карточка заявки в Telegram

После брони бот показывает кандидату ссылку. Кандидат жмёт её, нажимает
«Начать» — и получает карточку: вакансия, время интервью, его данные.

Фича необязательная: без переменных ниже бот работает как обычно.

### Настройка

1. Завести бота у **@BotFather**: команда `/newbot`, получить токен и имя.
2. Придумать случайную строку для `TELEGRAM_WEBHOOK_SECRET`.
3. Задать три переменные — в `.env` локально и в дашборде Render.
4. После деплоя один раз зарегистрировать адрес вебхука в Telegram:

```bash
curl -X POST "https://api.telegram.org/bot<ТОКЕН>/setWebhook" \
  -d "url=https://<адрес-сервиса>/telegram/webhook" \
  -d "secret_token=<СЕКРЕТ>"
```

Проверить, что Telegram принял адрес:

```bash
curl "https://api.telegram.org/bot<ТОКЕН>/getWebhookInfo"
```

### Что важно знать

- **Telegram не может написать первым.** Пока кандидат не нажал Start,
  отправить ему нечего — поэтому и нужна ссылка.
- **Ссылка привязывается к первому, кто её открыл.** Переслал другу —
  друг получит вежливый отказ, а не чужие данные.
- **Вердикт модели в карточку не попадает.** Это заметка для рекрутера.
- **Первая карточка может прийти с задержкой до минуты:** на бесплатном
  плане сервис засыпает, и запрос Telegram сначала его будит.
```

- [ ] **Step 4: Проверить, что YAML валиден и переменные на месте**

```bash
PYTHONIOENCODING=utf-8 <python> -c "import yaml;d=yaml.safe_load(open('render.yaml',encoding='utf-8'));print([e['key'] for e in d['services'][0]['envVars']])"
```
Ожидается список из шести ключей, включая три телеграмных.

- [ ] **Step 5: Прогнать весь набор — ничего не сломано**

Ожидается: 99 passed.

- [ ] **Step 6: Коммит**

```bash
git add README.md render.yaml .env.example
git commit -m "docs: настройка Telegram — переменные, вебхук, деплой

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
