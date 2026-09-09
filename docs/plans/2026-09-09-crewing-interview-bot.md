# Крюинг-бот записи на интервью — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Веб-чат крюингового агентства, который проводит моряка по анкете и скринингу, бронирует слот на интервью в Postgres и показывает рекрутеру заявки с вердиктами.

**Architecture:** Диалог ведёт чистый конечный автомат `funnel.py` — без базы и без модели, поэтому проверяется тестами целиком. `db.py` держит всю SQL, включая атомарную бронь. `brain.py` — четыре вызова модели с внедряемой функцией запроса, чтобы тесты шли без сети. `app.py` связывает три модуля с Gradio: вкладка кандидата и вкладка рекрутера под паролем.

**Tech Stack:** Python 3.12, Gradio 6.26, psycopg2-binary, openai-клиент к Gemini, Postgres (Supabase), pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-crewing-interview-bot-design.md`

## Global Constraints

- Python из окружения проекта: `.venv/Scripts/python.exe` (Windows). Установка пакетов — `uv pip install`.
- **Консоль Windows в cp1252 и падает с `UnicodeEncodeError` на кириллице в stdout.** Любую команду python и pytest запускать с `PYTHONIOENCODING=utf-8`. Пример: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests -v`
- Модель: `gemini-2.5-flash` через openai-клиент с `base_url="https://generativelanguage.googleapis.com/v1beta/openai/"`, ключ `GOOGLE_API_KEY`. Схема ровно как в `05-example_bot/bot.py`.
- Gradio 6 отдаёт `content` списком блоков `[{"type":"text","text":"..."}]`, а не строкой. Нормализовать функцией `to_plain_text` — она есть в `05-example_bot/bot.py`.
- Вся SQL — только параметризованная через `%s`. Никаких f-строк в запросах.
- Бот отвечает строго на языке последнего сообщения кандидата.
- Переменные окружения: `GOOGLE_API_KEY`, `DATABASE_URL`, `RECRUITER_PASSWORD`.
- Секреты не коммитить: `.env` уже в `.gitignore`, правки только в `.env.example`.
- Порт для Gradio: `int(os.environ.get("PORT", 7861))`. 7861, а не 7860 — чтобы не конфликтовать с барбершопом при локальном запуске обоих.
- Имя папки начинается с цифры, поэтому обычный `import` не работает. Везде — `import_module("07-crewing_bot.funnel")`.

---

## Структура файлов

| Файл | Ответственность |
|---|---|
| `07-crewing_bot/funnel.py` | Состояния воронки и чистые переходы. Не знает про БД и модель |
| `07-crewing_bot/db.py` | Схема, чтение вакансий и слотов, бронь, заявки. Не знает про модель |
| `07-crewing_bot/brain.py` | Router, Structured Output, RAG-ответ, Judge |
| `07-crewing_bot/app.py` | Gradio: вкладка кандидата + вкладка рекрутера |
| `07-crewing_bot/knowledge.md` | Факты про агентство для RAG |
| `07-crewing_bot/README.md` | Запуск и деплой |
| `07-crewing_bot/tests/test_funnel.py` | Переходы воронки, без сети |
| `07-crewing_bot/tests/test_db.py` | Бронь и правило одного активного интервью, против реальной базы |
| `07-crewing_bot/tests/test_brain.py` | Разбор ответов модели на подставной функции |
| `requirements.txt` | Добавить pytest |
| `.env.example` | Добавить `DATABASE_URL`, `RECRUITER_PASSWORD` |
| `render.yaml` | Добавить второй сервис `crewing-ai` |

---

### Task 1: Окружение и подключение к базе

**Files:**
- Create: `07-crewing_bot/__init__.py`, `07-crewing_bot/tests/__init__.py`
- Modify: `requirements.txt`, `.env.example`

**Interfaces:**
- Consumes: ничего
- Produces: рабочий `pytest`, заполненная переменная `DATABASE_URL`

**Внимание:** проект Supabase создаёт человек, автоматизировать это нельзя. Если `DATABASE_URL` не задан — остановись и попроси его, не выдумывай строку подключения.

- [ ] **Step 1: Установить pytest**

```bash
.venv/Scripts/python.exe -m uv pip install pytest
```

- [ ] **Step 2: Добавить pytest в requirements.txt**

Дописать в конец секции базовых зависимостей:

```
pytest>=8.0            # тесты (07-crewing_bot)
```

- [ ] **Step 3: Дописать переменные в .env.example**

```
# 🚢 Крюинг-бот (07-crewing_bot)
# База обязательна: Supabase → Settings → Database → Connection string → URI.
# Если Render не подключается по прямой строке — взять вариант Session pooler.
DATABASE_URL=postgresql://postgres:ПАРОЛЬ@db.xxxx.supabase.co:5432/postgres
# Пароль вкладки рекрутера — придумай свой
RECRUITER_PASSWORD=...
```

- [ ] **Step 4: Создать пакет**

```bash
mkdir -p 07-crewing_bot/tests
touch 07-crewing_bot/__init__.py 07-crewing_bot/tests/__init__.py
```

- [ ] **Step 5: Проверить, что база отвечает**

Задав `DATABASE_URL` в `.env`, выполнить:

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "import os,psycopg2;from dotenv import load_dotenv;load_dotenv();psycopg2.connect(os.environ['DATABASE_URL']);print('db ok')"
```

Ожидается: `db ok`. Если ошибка сети — попробовать строку Session pooler из Supabase.

- [ ] **Step 6: Коммит**

```bash
git add requirements.txt .env.example 07-crewing_bot/__init__.py 07-crewing_bot/tests/__init__.py
git commit -m "chore: скаффолд крюинг-бота и pytest

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Воронка — состояние и вопросы анкеты

**Files:**
- Create: `07-crewing_bot/funnel.py`, `07-crewing_bot/tests/test_funnel.py`

**Interfaces:**
- Consumes: ничего
- Produces:
  - константы шагов `GREETING`, `CHOOSING_VACANCY`, `COLLECTING_PROFILE`, `SCREENING`, `CHOOSING_SLOT`, `CONFIRMED`, `BLOCKED` (все `str`)
  - `PROFILE_FIELDS: list[tuple[str, str]]` — пары (имя поля, вопрос)
  - `State` — frozen dataclass с полями `step: str`, `vacancy_id: int | None`, `profile: dict`, `screening_questions: list`, `screening: dict`, `slot_ids: list`, `slot_id: int | None`, `blocked_reason: str`, `notes: str`
  - `next_question(state: State) -> str | None`
  - `select_vacancy(state: State, vacancy_id: int, screening_questions: list) -> State`
  - `record_profile(state: State, parsed: dict) -> State`

- [ ] **Step 1: Написать падающий тест**

`07-crewing_bot/tests/test_funnel.py`:

```python
from importlib import import_module

funnel = import_module("07-crewing_bot.funnel")


def test_new_state_starts_at_greeting():
    state = funnel.State()
    assert state.step == funnel.GREETING


def test_select_vacancy_moves_to_profile_and_asks_name():
    state = funnel.select_vacancy(funnel.State(), vacancy_id=7, screening_questions=["STCW?"])
    assert state.step == funnel.COLLECTING_PROFILE
    assert state.vacancy_id == 7
    assert state.screening_questions == ["STCW?"]
    assert "зовут" in funnel.next_question(state)


def test_record_profile_fills_several_fields_at_once():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    state = funnel.record_profile(state, {"full_name": "Ivanov Ivan", "citizenship": "Ukraine"})
    assert state.profile["full_name"] == "Ivanov Ivan"
    assert state.profile["citizenship"] == "Ukraine"
    # контакт ещё не заполнен — его и спрашиваем
    assert "email" in funnel.next_question(state).lower()


def test_record_profile_ignores_empty_values():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    state = funnel.record_profile(state, {"full_name": None, "citizenship": ""})
    assert state.profile == {}


def test_state_is_not_mutated_in_place():
    before = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    funnel.record_profile(before, {"full_name": "Ivanov Ivan"})
    assert before.profile == {}
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_funnel.py -v
```

Ожидается: FAIL, `ModuleNotFoundError: No module named '07-crewing_bot.funnel'`

- [ ] **Step 3: Написать минимальную реализацию**

`07-crewing_bot/funnel.py`:

```python
"""Воронка разговора с моряком: чистые переходы состояния.

Модуль намеренно ничего не знает ни про базу, ни про модель — поэтому
проверяется тестами целиком и мгновенно.
"""

from dataclasses import dataclass, field, replace

GREETING = "greeting"
CHOOSING_VACANCY = "choosing_vacancy"
COLLECTING_PROFILE = "collecting_profile"
SCREENING = "screening"
CHOOSING_SLOT = "choosing_slot"
CONFIRMED = "confirmed"
BLOCKED = "blocked"

# Порядок важен: вопросы задаются сверху вниз, первым — незаполненный.
PROFILE_FIELDS = [
    ("full_name", "Как вас зовут? Фамилия и имя, как в паспорте моряка."),
    ("contact", "Как с вами связаться — email или WhatsApp?"),
    ("citizenship", "Ваше гражданство?"),
    ("rank_experience_months", "Сколько месяцев опыта именно в этой должности?"),
    ("total_experience_months", "Какой общий стаж в море, в месяцах?"),
    ("vessel_types", "На каких типах судов вы работали?"),
    ("readiness_date", "С какой даты готовы заступить на контракт?"),
]


@dataclass(frozen=True)
class State:
    step: str = GREETING
    vacancy_id: int | None = None
    profile: dict = field(default_factory=dict)
    screening_questions: list = field(default_factory=list)
    screening: dict = field(default_factory=dict)
    slot_ids: list = field(default_factory=list)
    slot_id: int | None = None
    blocked_reason: str = ""
    # сырые ответы, которые модель не смогла разобрать — уйдут рекрутеру
    notes: str = ""


def select_vacancy(state: State, vacancy_id: int, screening_questions: list) -> State:
    return replace(
        state,
        step=COLLECTING_PROFILE,
        vacancy_id=vacancy_id,
        screening_questions=list(screening_questions),
    )


def record_profile(state: State, parsed: dict) -> State:
    """Записать распознанные поля анкеты. Пустые значения игнорируются."""
    known = {name for name, _ in PROFILE_FIELDS}
    filled = dict(state.profile)
    for name, value in parsed.items():
        if name in known and value not in (None, "", []):
            filled[name] = value
    return replace(state, profile=filled)


def next_question(state: State) -> str | None:
    """Вопрос, который бот задаёт прямо сейчас.

    Список вакансий и список слотов рисует app.py из данных базы —
    здесь только вопросы анкеты и скрининга.
    """
    if state.step == COLLECTING_PROFILE:
        for name, question in PROFILE_FIELDS:
            if name not in state.profile:
                return question
    return None
```

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_funnel.py -v
```

Ожидается: 5 passed

- [ ] **Step 5: Коммит**

```bash
git add 07-crewing_bot/funnel.py 07-crewing_bot/tests/test_funnel.py
git commit -m "feat: воронка разговора — состояние и вопросы анкеты

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Воронка — скрининг, слоты и блокировка

**Files:**
- Modify: `07-crewing_bot/funnel.py`, `07-crewing_bot/tests/test_funnel.py`

**Interfaces:**
- Consumes: всё из Task 2
- Produces:
  - `profile_complete(state: State) -> bool`
  - `start_screening(state: State) -> State`
  - `record_screening_answer(state: State, answer: str) -> State`
  - `offer_slots(state: State, slot_ids: list) -> State`
  - `select_slot(state: State, number: int) -> State` — бросает `ValueError` при номере вне диапазона
  - `confirm(state: State) -> State`
  - `block(state: State, reason: str) -> State`
  - `add_note(state: State, text: str) -> State`
  - `next_question` дополнен веткой `SCREENING`

- [ ] **Step 1: Написать падающие тесты**

Дописать в `07-crewing_bot/tests/test_funnel.py`:

```python
import pytest


def _filled_profile_state():
    state = funnel.select_vacancy(funnel.State(), 7, ["Есть ли действующий STCW?", "Виза US C1/D?"])
    return funnel.record_profile(state, {name: "x" for name, _ in funnel.PROFILE_FIELDS})


def test_profile_complete_only_when_all_fields_filled():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    assert funnel.profile_complete(state) is False
    assert funnel.profile_complete(_filled_profile_state()) is True


def test_screening_asks_questions_one_by_one():
    state = funnel.start_screening(_filled_profile_state())
    assert state.step == funnel.SCREENING
    assert funnel.next_question(state) == "Есть ли действующий STCW?"

    state = funnel.record_screening_answer(state, "Да, до 2028 года")
    assert state.screening["Есть ли действующий STCW?"] == "Да, до 2028 года"
    assert funnel.next_question(state) == "Виза US C1/D?"


def test_screening_finished_moves_to_slots():
    state = funnel.start_screening(_filled_profile_state())
    state = funnel.record_screening_answer(state, "Да")
    state = funnel.record_screening_answer(state, "Нет")
    assert state.step == funnel.CHOOSING_SLOT
    assert funnel.next_question(state) is None


def test_select_slot_by_number_picks_id_from_database_list():
    state = funnel.offer_slots(funnel.State(step=funnel.CHOOSING_SLOT), [101, 102, 103])
    state = funnel.select_slot(state, 2)
    assert state.slot_id == 102


def test_select_slot_out_of_range_is_rejected():
    state = funnel.offer_slots(funnel.State(step=funnel.CHOOSING_SLOT), [101, 102])
    with pytest.raises(ValueError):
        funnel.select_slot(state, 5)
    with pytest.raises(ValueError):
        funnel.select_slot(state, 0)
    # состояние не испорчено
    assert state.slot_id is None


def test_add_note_accumulates_raw_answers():
    state = funnel.add_note(funnel.State(), "24 мес наверное")
    state = funnel.add_note(state, "виза в процессе")
    assert "24 мес наверное" in state.notes
    assert "виза в процессе" in state.notes


def test_block_stops_the_funnel():
    state = funnel.block(funnel.State(), "уже записан на 12 марта, 10:00")
    assert state.step == funnel.BLOCKED
    assert "12 марта" in state.blocked_reason
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_funnel.py -v
```

Ожидается: FAIL, `AttributeError: module ... has no attribute 'profile_complete'`

- [ ] **Step 3: Дописать реализацию**

Добавить в `07-crewing_bot/funnel.py`:

```python
def profile_complete(state: State) -> bool:
    return all(name in state.profile for name, _ in PROFILE_FIELDS)


def start_screening(state: State) -> State:
    return replace(state, step=SCREENING)


def record_screening_answer(state: State, answer: str) -> State:
    """Записать ответ на текущий вопрос скрининга и перейти к следующему.

    Когда вопросы кончились — воронка переходит к выбору слота.
    """
    pending = [q for q in state.screening_questions if q not in state.screening]
    if not pending:
        return replace(state, step=CHOOSING_SLOT)
    answers = dict(state.screening)
    answers[pending[0]] = answer
    step = SCREENING if len(pending) > 1 else CHOOSING_SLOT
    return replace(state, screening=answers, step=step)


def offer_slots(state: State, slot_ids: list) -> State:
    """Запомнить слоты, показанные кандидату. Номер в чате — индекс в этом списке."""
    return replace(state, step=CHOOSING_SLOT, slot_ids=list(slot_ids))


def select_slot(state: State, number: int) -> State:
    """Выбрать слот по номеру из показанного списка.

    Номер — индекс в списке из базы, поэтому несуществующее время
    выбрать невозможно: за пределами списка будет ValueError.
    """
    if not 1 <= number <= len(state.slot_ids):
        raise ValueError(f"нет слота с номером {number}")
    return replace(state, slot_id=state.slot_ids[number - 1])


def confirm(state: State) -> State:
    return replace(state, step=CONFIRMED)


def block(state: State, reason: str) -> State:
    return replace(state, step=BLOCKED, blocked_reason=reason)


def add_note(state: State, text: str) -> State:
    """Копить сырые ответы, которые не удалось разобрать.

    Рекрутер увидит их в заявке — лучше показать человеку исходный
    текст, чем молча потерять его.
    """
    joined = f"{state.notes}
{text}".strip() if state.notes else text.strip()
    return replace(state, notes=joined)
```

И дополнить `next_question` веткой скрининга — вставить перед `return None`:

```python
    if state.step == SCREENING:
        for question in state.screening_questions:
            if question not in state.screening:
                return question
```

- [ ] **Step 4: Запустить тесты**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_funnel.py -v
```

Ожидается: 12 passed

- [ ] **Step 5: Коммит**

```bash
git add 07-crewing_bot/funnel.py 07-crewing_bot/tests/test_funnel.py
git commit -m "feat: воронка — скрининг, выбор слота по номеру, блокировка

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: База — схема, вакансии, слоты

**Files:**
- Create: `07-crewing_bot/db.py`, `07-crewing_bot/tests/test_db.py`

**Interfaces:**
- Consumes: ничего
- Produces:
  - `connect()` — соединение psycopg2 по `DATABASE_URL`, бросает `RuntimeError` если переменной нет
  - `init_schema(conn) -> None`
  - `create_vacancy(conn, rank, vessel_type, contract_months, salary_usd, requirements, screening_questions: list) -> int`
  - `list_active_vacancies(conn) -> list` — словари с ключами `id, rank, vessel_type, contract_months, salary_usd, requirements, screening_questions`
  - `get_vacancy(conn, vacancy_id) -> dict | None`
  - `open_slots(conn, day: date, start_hhmm: str, end_hhmm: str, step_min: int = 30) -> int`
  - `list_open_slots(conn, limit: int = 10) -> list` — словари с ключами `id, starts_at`, только будущие, по возрастанию времени

- [ ] **Step 1: Написать падающий тест**

`07-crewing_bot/tests/test_db.py`:

```python
import os
from datetime import date, timedelta
from importlib import import_module

import pytest

db = import_module("07-crewing_bot.db")

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="нужен DATABASE_URL — тесты идут против реальной базы",
)


@pytest.fixture
def conn():
    """Чистая схема на каждый тест. База тестовая, данные в ней одноразовые."""
    connection = db.connect()
    db.init_schema(connection)
    with connection, connection.cursor() as cur:
        cur.execute("TRUNCATE applications, candidates, slots, vacancies RESTART IDENTITY CASCADE")
    yield connection
    connection.close()


def test_create_and_list_vacancy(conn):
    vacancy_id = db.create_vacancy(
        conn,
        rank="2nd Engineer",
        vessel_type="bulk carrier",
        contract_months=6,
        salary_usd=6500,
        requirements="Опыт на балкерах от 12 месяцев",
        screening_questions=["Действующий STCW?", "Виза US C1/D?"],
    )
    assert isinstance(vacancy_id, int)

    vacancies = db.list_active_vacancies(conn)
    assert len(vacancies) == 1
    assert vacancies[0]["rank"] == "2nd Engineer"
    assert vacancies[0]["screening_questions"] == ["Действующий STCW?", "Виза US C1/D?"]


def test_open_slots_creates_slots_by_step(conn):
    tomorrow = date.today() + timedelta(days=1)
    created = db.open_slots(conn, tomorrow, "10:00", "12:00", step_min=30)
    assert created == 4

    slots = db.list_open_slots(conn)
    assert len(slots) == 4
    assert slots[0]["starts_at"] < slots[1]["starts_at"]


def test_list_open_slots_hides_past(conn):
    db.open_slots(conn, date.today() - timedelta(days=2), "10:00", "11:00", step_min=30)
    assert db.list_open_slots(conn) == []
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_db.py -v
```

Ожидается: FAIL с `ModuleNotFoundError`. Если вместо этого все тесты `SKIPPED` — не задан `DATABASE_URL`, вернись к Task 1.

- [ ] **Step 3: Написать реализацию**

`07-crewing_bot/db.py`:

```python
"""Вся работа с Postgres: схема, вакансии, слоты, бронь, заявки.

Модуль не знает про модель — здесь только данные.
Все запросы параметризованы через %s: подстановка f-строкой в SQL
открыла бы инъекцию.
"""

import json
import os
from datetime import date, datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id SERIAL PRIMARY KEY,
    rank TEXT NOT NULL,
    vessel_type TEXT NOT NULL,
    contract_months INT,
    salary_usd INT,
    requirements TEXT,
    screening_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS slots (
    id SERIAL PRIMARY KEY,
    starts_at TIMESTAMPTZ NOT NULL,
    duration_min INT NOT NULL DEFAULT 30,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS candidates (
    id SERIAL PRIMARY KEY,
    full_name TEXT NOT NULL,
    contact TEXT NOT NULL UNIQUE,
    citizenship TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS applications (
    id SERIAL PRIMARY KEY,
    candidate_id INT NOT NULL REFERENCES candidates(id),
    vacancy_id INT NOT NULL REFERENCES vacancies(id),
    slot_id INT NOT NULL UNIQUE REFERENCES slots(id),
    rank_experience_months INT,
    total_experience_months INT,
    vessel_types TEXT,
    readiness_date TEXT,
    screening JSONB NOT NULL DEFAULT '{}'::jsonb,
    verdict TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Одна активная заявка на моряка: страховка от гонки двух вкладок.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_application
    ON applications (candidate_id) WHERE status = 'new';
"""


def connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Нет DATABASE_URL. Supabase → Settings → Database → Connection string → URI."
        )
    return psycopg2.connect(url)


def init_schema(conn) -> None:
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA)


def create_vacancy(conn, rank, vessel_type, contract_months, salary_usd,
                   requirements, screening_questions) -> int:
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vacancies (rank, vessel_type, contract_months, salary_usd,"
            " requirements, screening_questions) VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (rank, vessel_type, contract_months, salary_usd, requirements,
             json.dumps(list(screening_questions), ensure_ascii=False)),
        )
        return cur.fetchone()[0]


def list_active_vacancies(conn) -> list:
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, rank, vessel_type, contract_months, salary_usd,"
            " requirements, screening_questions FROM vacancies"
            " WHERE is_active ORDER BY id"
        )
        return [dict(row) for row in cur.fetchall()]


def get_vacancy(conn, vacancy_id: int):
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, rank, vessel_type, contract_months, salary_usd,"
            " requirements, screening_questions FROM vacancies WHERE id = %s",
            (vacancy_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def open_slots(conn, day: date, start_hhmm: str, end_hhmm: str, step_min: int = 30) -> int:
    """Открыть слоты интервалом. Возвращает число созданных слотов."""
    start = datetime.combine(day, datetime.strptime(start_hhmm, "%H:%M").time())
    end = datetime.combine(day, datetime.strptime(end_hhmm, "%H:%M").time())
    moments = []
    current = start
    while current < end:
        moments.append(current)
        current += timedelta(minutes=step_min)

    with conn, conn.cursor() as cur:
        for moment in moments:
            cur.execute(
                "INSERT INTO slots (starts_at, duration_min) VALUES (%s, %s)",
                (moment, step_min),
            )
    return len(moments)


def list_open_slots(conn, limit: int = 10) -> list:
    """Свободные слоты в будущем, по возрастанию времени."""
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, starts_at FROM slots"
            " WHERE status = 'open' AND starts_at > NOW()"
            " ORDER BY starts_at LIMIT %s",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
```

- [ ] **Step 4: Запустить тесты**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_db.py -v
```

Ожидается: 3 passed

- [ ] **Step 5: Коммит**

```bash
git add 07-crewing_bot/db.py 07-crewing_bot/tests/test_db.py
git commit -m "feat: схема базы, вакансии и слоты интервью

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: База — бронь слота и правило одного активного интервью

**Files:**
- Modify: `07-crewing_bot/db.py`, `07-crewing_bot/tests/test_db.py`

**Interfaces:**
- Consumes: всё из Task 4
- Produces:
  - `book_slot(conn, slot_id: int) -> bool` — `False` если слот уже занят
  - `upsert_candidate(conn, full_name, contact, citizenship) -> int`
  - `find_active_application(conn, contact: str) -> dict | None` — ключи `id, starts_at`; прошедшие заявки сама переводит в `done`
  - `create_application(conn, candidate_id, vacancy_id, slot_id, profile: dict, screening: dict, notes: str = "") -> int`
  - `set_verdict(conn, application_id: int, verdict: str) -> None`
  - `list_applications(conn, limit: int = 50) -> list`

- [ ] **Step 1: Написать падающие тесты**

Дописать в `07-crewing_bot/tests/test_db.py`:

```python
def _vacancy(conn):
    return db.create_vacancy(conn, "AB", "container", 6, 1800, "Опыт от 12 мес", ["STCW?"])


def _future_slots(conn, count=2):
    db.open_slots(conn, date.today() + timedelta(days=1), "10:00", "12:00", step_min=30)
    return [slot["id"] for slot in db.list_open_slots(conn)][:count]


def _profile(**overrides):
    profile = {
        "full_name": "Petrov Petr",
        "contact": "petrov@example.com",
        "citizenship": "Ukraine",
        "rank_experience_months": 24,
        "total_experience_months": 60,
        "vessel_types": "container",
        "readiness_date": "2026-10-01",
    }
    profile.update(overrides)
    return profile


def test_second_booking_of_same_slot_is_refused(conn):
    slot_id = _future_slots(conn)[0]
    assert db.book_slot(conn, slot_id) is True
    assert db.book_slot(conn, slot_id) is False


def test_booked_slot_disappears_from_open_list(conn):
    slot_id = _future_slots(conn)[0]
    db.book_slot(conn, slot_id)
    assert slot_id not in [slot["id"] for slot in db.list_open_slots(conn)]


def test_same_contact_reuses_candidate_row(conn):
    first = db.upsert_candidate(conn, "Petrov Petr", "petrov@example.com", "Ukraine")
    second = db.upsert_candidate(conn, "Petrov P.", "petrov@example.com", "Ukraine")
    assert first == second


def test_candidate_with_active_application_is_found(conn):
    vacancy_id = _vacancy(conn)
    slot_id = _future_slots(conn)[0]
    candidate_id = db.upsert_candidate(conn, "Petrov Petr", "petrov@example.com", "Ukraine")
    db.book_slot(conn, slot_id)
    db.create_application(conn, candidate_id, vacancy_id, slot_id, _profile(), {"STCW?": "да"})

    active = db.find_active_application(conn, "petrov@example.com")
    assert active is not None
    assert active["starts_at"] is not None


def test_unknown_contact_has_no_active_application(conn):
    assert db.find_active_application(conn, "nobody@example.com") is None


def test_past_interview_stops_blocking_and_is_marked_done(conn):
    vacancy_id = _vacancy(conn)
    candidate_id = db.upsert_candidate(conn, "Petrov Petr", "petrov@example.com", "Ukraine")
    # слот в прошлом заводим напрямую: open_slots рассчитан на будущее
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slots (starts_at, status) VALUES (NOW() - INTERVAL '2 days', 'booked')"
            " RETURNING id"
        )
        past_slot_id = cur.fetchone()[0]
    db.create_application(conn, candidate_id, vacancy_id, past_slot_id, _profile(), {})

    assert db.find_active_application(conn, "petrov@example.com") is None

    with conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM applications WHERE slot_id = %s", (past_slot_id,))
        assert cur.fetchone()[0] == "done"


def test_verdict_is_written_after_application(conn):
    vacancy_id = _vacancy(conn)
    slot_id = _future_slots(conn)[0]
    candidate_id = db.upsert_candidate(conn, "Petrov Petr", "petrov@example.com", "Ukraine")
    db.book_slot(conn, slot_id)
    application_id = db.create_application(
        conn, candidate_id, vacancy_id, slot_id, _profile(), {"STCW?": "да"}
    )
    db.set_verdict(conn, application_id, "Подходит: 24 месяца в должности")

    rows = db.list_applications(conn)
    assert rows[0]["verdict"].startswith("Подходит")
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_db.py -v
```

Ожидается: FAIL, `AttributeError: module ... has no attribute 'book_slot'`

- [ ] **Step 3: Дописать реализацию**

Добавить в `07-crewing_bot/db.py`:

```python
def book_slot(conn, slot_id: int) -> bool:
    """Занять слот. False означает, что его уже заняли — предложи другой.

    Условие status='open' прямо в UPDATE делает двойную бронь
    невозможной на уровне базы, а не на уровне аккуратности кода.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE slots SET status = 'booked' WHERE id = %s AND status = 'open' RETURNING id",
            (slot_id,),
        )
        return cur.fetchone() is not None


def upsert_candidate(conn, full_name: str, contact: str, citizenship: str) -> int:
    """Найти моряка по контакту или завести. Один человек — одна строка."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO candidates (full_name, contact, citizenship) VALUES (%s, %s, %s)"
            " ON CONFLICT (contact) DO UPDATE SET full_name = EXCLUDED.full_name,"
            " citizenship = EXCLUDED.citizenship RETURNING id",
            (full_name, contact, citizenship),
        )
        return cur.fetchone()[0]


def find_active_application(conn, contact: str):
    """Активная заявка моряка, если есть.

    Активная — статус 'new' и слот ещё в будущем. Заявки с прошедшим
    интервью переводятся в 'done' здесь же, чтобы рекрутеру не
    приходилось чистить статусы руками.
    """
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE applications a SET status = 'done' FROM slots s, candidates c"
            " WHERE a.slot_id = s.id AND a.candidate_id = c.id"
            " AND c.contact = %s AND a.status = 'new' AND s.starts_at <= NOW()",
            (contact,),
        )
        cur.execute(
            "SELECT a.id, s.starts_at FROM applications a"
            " JOIN slots s ON s.id = a.slot_id"
            " JOIN candidates c ON c.id = a.candidate_id"
            " WHERE c.contact = %s AND a.status = 'new' AND s.starts_at > NOW()",
            (contact,),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def create_application(conn, candidate_id: int, vacancy_id: int, slot_id: int,
                       profile: dict, screening: dict, notes: str = "") -> int:
    """Сохранить заявку. Вердикт дописывается позже отдельным запросом:
    отказ модели на последнем шаге не должен стоить кандидату брони."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applications (candidate_id, vacancy_id, slot_id,"
            " rank_experience_months, total_experience_months, vessel_types,"
            " readiness_date, screening, notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " RETURNING id",
            (
                candidate_id, vacancy_id, slot_id,
                _as_int(profile.get("rank_experience_months")),
                _as_int(profile.get("total_experience_months")),
                profile.get("vessel_types"),
                profile.get("readiness_date"),
                json.dumps(screening, ensure_ascii=False),
                notes,
            ),
        )
        return cur.fetchone()[0]


def _as_int(value):
    """Модель может вернуть '24 месяца' вместо 24 — цифры важнее формата."""
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def set_verdict(conn, application_id: int, verdict: str) -> None:
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE applications SET verdict = %s WHERE id = %s",
            (verdict, application_id),
        )


def list_applications(conn, limit: int = 50) -> list:
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT a.id, c.full_name, c.contact, c.citizenship, v.rank, v.vessel_type,"
            " s.starts_at, a.rank_experience_months, a.total_experience_months,"
            " a.vessel_types, a.readiness_date, a.screening, a.verdict, a.status,"
            " a.created_at FROM applications a"
            " JOIN candidates c ON c.id = a.candidate_id"
            " JOIN vacancies v ON v.id = a.vacancy_id"
            " JOIN slots s ON s.id = a.slot_id"
            " ORDER BY a.created_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
```

- [ ] **Step 4: Запустить все тесты базы**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_db.py -v
```

Ожидается: 10 passed

- [ ] **Step 5: Коммит**

```bash
git add 07-crewing_bot/db.py 07-crewing_bot/tests/test_db.py
git commit -m "feat: атомарная бронь слота и одно активное интервью на моряка

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Мозг — четыре вызова модели

**Files:**
- Create: `07-crewing_bot/brain.py`, `07-crewing_bot/tests/test_brain.py`, `07-crewing_bot/knowledge.md`

**Interfaces:**
- Consumes: ничего
- Produces (у всех функций есть параметр `ask` — функция `(str) -> str`, по умолчанию реальная модель; тесты подставляют свою):
  - `ask_model(prompt: str, system: str = ...) -> str`
  - `classify(message: str, current_question: str, *, ask=ask_model) -> str` — `"ответ"` или `"вопрос"`
  - `extract(message: str, fields: list, *, ask=ask_model) -> dict`
  - `answer(message: str, knowledge: str, vacancy_text: str, *, ask=ask_model) -> str`
  - `verdict(vacancy: dict, profile: dict, screening: dict, *, ask=ask_model) -> str`

- [ ] **Step 1: Написать падающий тест**

`07-crewing_bot/tests/test_brain.py`:

```python
from importlib import import_module

brain = import_module("07-crewing_bot.brain")


def test_classify_recognises_counter_question():
    assert brain.classify("а какая зарплата?", "Гражданство?", ask=lambda p, **k: "вопрос") == "вопрос"


def test_classify_falls_back_to_answer_on_garbage():
    # модель ответила мусором — считаем это ответом по анкете и не ломаем воронку
    assert brain.classify("Украина", "Гражданство?", ask=lambda p, **k: "!!!") == "ответ"


def test_extract_pulls_json_out_of_chatty_reply():
    reply = 'Конечно! Вот данные: {"full_name": "Ivanov Ivan", "citizenship": "Ukraine"} — готово'
    result = brain.extract("Иванов Иван, Украина", ["full_name", "citizenship"], ask=lambda p, **k: reply)
    assert result == {"full_name": "Ivanov Ivan", "citizenship": "Ukraine"}


def test_extract_returns_empty_dict_when_model_gives_no_json():
    assert brain.extract("что-то", ["full_name"], ask=lambda p, **k: "не понял") == {}


def test_extract_drops_fields_that_were_not_requested():
    reply = '{"full_name": "Ivanov Ivan", "salary": 9000}'
    result = brain.extract("...", ["full_name"], ask=lambda p, **k: reply)
    assert result == {"full_name": "Ivanov Ivan"}


def test_verdict_receives_vacancy_and_screening_in_prompt():
    seen = {}

    def fake_ask(prompt, **kwargs):
        seen["prompt"] = prompt
        return "Подходит"

    brain.verdict(
        {"rank": "2nd Engineer", "vessel_type": "bulk carrier", "requirements": "опыт 12 мес"},
        {"rank_experience_months": 24},
        {"STCW?": "да"},
        ask=fake_ask,
    )
    assert "2nd Engineer" in seen["prompt"]
    assert "STCW?" in seen["prompt"]
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_brain.py -v
```

Ожидается: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Написать реализацию**

`07-crewing_bot/brain.py`:

```python
"""Четыре вызова модели: Router, Structured Output, RAG-ответ, Judge.

У каждой функции есть параметр ask — так тесты подставляют свою
функцию и проверяют разбор ответа, не ходя в сеть.
"""

import json
import os

from openai import OpenAI

MODEL = "gemini-2.5-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def ask_model(prompt: str, system: str = "Ты — помощник крюингового агентства.") -> str:
    key = os.getenv("GOOGLE_API_KEY")
    if not key or "..." in key:
        return "⚠️ Нет GOOGLE_API_KEY в .env"
    try:
        client = OpenAI(api_key=key, base_url=BASE_URL)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content
    except Exception as error:
        return f"⚠️ Модель не отвечает: {error}"


def classify(message: str, current_question: str, *, ask=ask_model) -> str:
    """Router: кандидат отвечает на вопрос анкеты или задаёт свой?

    При любом непонятном ответе модели считаем, что это ответ по анкете:
    так воронка продолжается, а не встаёт.
    """
    reply = ask(
        "Кандидату задали вопрос анкеты. Он ответил на него или задал встречный "
        "вопрос про вакансию? Верни ОДНО слово: ответ или вопрос.\n\n"
        f"Вопрос анкеты: {current_question}\nСообщение кандидата: {message}"
    )
    return "вопрос" if "вопрос" in reply.strip().lower() else "ответ"


def extract(message: str, fields: list, *, ask=ask_model) -> dict:
    """Structured Output: свободный текст → словарь только запрошенных полей."""
    reply = ask(
        "Извлеки из сообщения перечисленные поля. Верни СТРОГО JSON-объект, "
        "только его, без пояснений. Чего нет в сообщении — не включай.\n\n"
        f"Поля: {', '.join(fields)}\nСообщение: {message}"
    )
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


def verdict(vacancy: dict, profile: dict, screening: dict, *, ask=ask_model) -> str:
    """Judge: короткая оценка кандидата для рекрутера. Решение принимает человек."""
    return ask(
        "Оцени кандидата под вакансию для крюинг-менеджера. Два-три предложения: "
        "подходит или нет и почему. Не отказывай кандидату — это заметка "
        "для менеджера, решение принимает он.\n\n"
        f"ВАКАНСИЯ: {vacancy.get('rank')} на {vacancy.get('vessel_type')}. "
        f"Требования: {vacancy.get('requirements')}\n\n"
        f"АНКЕТА: {json.dumps(profile, ensure_ascii=False)}\n\n"
        f"СКРИНИНГ: {json.dumps(screening, ensure_ascii=False)}"
    )
```

- [ ] **Step 4: Запустить тесты**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests/test_brain.py -v
```

Ожидается: 6 passed

- [ ] **Step 5: Написать базу знаний**

`07-crewing_bot/knowledge.md` — факты для RAG. Заменить на реальные данные агентства, структура такая:

```markdown
# Крюинговое агентство «Меридиан» — факты для бота

## О компании
Подбираем экипажи на суда под флагами Панамы, Мальты и Либерии с 2009 года.
Офис в Одессе, работаем со всеми регионами удалённо.

## Как проходит найм
1. Собеседование с крюинг-менеджером — 30 минут, онлайн.
2. Проверка документов и рекомендаций с предыдущих судов.
3. Оффер, подписание контракта, оформление визы.
4. Посадка на судно за счёт компании.

## Документы, которые нужны всегда
Паспорт моряка (SID), STCW basic safety training, действующая медкомиссия,
рекомендации с последних двух судов.

## Частые вопросы
- **Зарплата** — указана в каждой вакансии, выплата ежемесячно на карту.
- **Длительность контракта** — указана в вакансии, обычно 4–6 месяцев плюс месяц.
- **Оплата перелёта и визы** — за счёт судовладельца.
- **Английский** — для офицеров Marlins от 70%, для рядового состава базовый.
```

- [ ] **Step 6: Коммит**

```bash
git add 07-crewing_bot/brain.py 07-crewing_bot/tests/test_brain.py 07-crewing_bot/knowledge.md
git commit -m "feat: вызовы модели — router, разбор анкеты, RAG-ответ, вердикт

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Вкладка кандидата

**Files:**
- Create: `07-crewing_bot/app.py`

**Interfaces:**
- Consumes: всё из `funnel`, `db`, `brain`
- Produces:
  - `to_plain_text(content) -> str`
  - `render_vacancies(vacancies: list) -> str`
  - `render_slots(slots: list) -> str`
  - `handle(message: str, state_dict: dict) -> tuple` — ответ бота и новое состояние словарём
  - `candidate_chat(message, history, state_dict)` — обёртка для Gradio

Отдельных тестов у `app.py` в этом плане нет намеренно: воронка покрыта в Task 2–3, база в Task 4–5, разбор в Task 6, здесь остаётся склейка и Gradio. Проверка — ручной сценарий в Task 8.

- [ ] **Step 1: Написать модуль**

`07-crewing_bot/app.py`:

```python
"""Крюинг-бот: вкладка кандидата и вкладка рекрутера.

Запуск локально:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe 07-crewing_bot/app.py
"""

import os
import sys
from importlib import import_module
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
funnel = import_module("07-crewing_bot.funnel")
db = import_module("07-crewing_bot.db")
brain = import_module("07-crewing_bot.brain")

load_dotenv()

KNOWLEDGE = (Path(__file__).parent / "knowledge.md").read_text(encoding="utf-8")


def to_plain_text(content) -> str:
    """Gradio 6 отдаёт content списком блоков, а модель ждёт строку."""
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        )
    return content


def render_vacancies(vacancies: list) -> str:
    return "\n".join(
        f"{number}. {v['rank']} — {v['vessel_type']}, "
        f"{v['contract_months']} мес, ${v['salary_usd']}/мес"
        for number, v in enumerate(vacancies, start=1)
    )


def render_slots(slots: list) -> str:
    return "\n".join(
        f"{number}. {slot['starts_at'].strftime('%d.%m %H:%M')}"
        for number, slot in enumerate(slots, start=1)
    )


def _vacancy_text(vacancy) -> str:
    if not vacancy:
        return ""
    return (
        f"{vacancy['rank']} на {vacancy['vessel_type']}, контракт "
        f"{vacancy['contract_months']} мес, ставка ${vacancy['salary_usd']}. "
        f"Требования: {vacancy['requirements']}"
    )


def _parse_number(message: str):
    digits = "".join(ch for ch in str(message) if ch.isdigit())
    return int(digits) if digits else None


def handle(message: str, state_dict: dict):
    """Один ход разговора: возвращает ответ бота и новое состояние."""
    state = funnel.State(**state_dict) if state_dict else funnel.State()
    try:
        conn = db.connect()
    except RuntimeError as error:
        return f"⚠️ {error}", vars(state)
    try:
        return _handle_with_db(conn, message, state)
    finally:
        conn.close()


def _handle_with_db(conn, message: str, state):
    db.init_schema(conn)
    vacancies = db.list_active_vacancies(conn)

    if state.step == funnel.BLOCKED:
        return state.blocked_reason, vars(state)

    if state.step == funnel.CONFIRMED:
        return "Вы уже записаны. Менеджер свяжется с вами перед интервью.", vars(state)

    # Приветствие: показываем вакансии и ждём номер
    if state.step == funnel.GREETING:
        if not vacancies:
            return "Сейчас открытых вакансий нет. Загляните позже.", vars(state)
        state = funnel.State(step=funnel.CHOOSING_VACANCY)
        return (
            "Здравствуйте! Я помощник крюингового агентства. "
            "Вот открытые вакансии — ответьте номером:\n\n"
            + render_vacancies(vacancies)
        ), vars(state)

    if state.step == funnel.CHOOSING_VACANCY:
        number = _parse_number(message)
        if number is None or not 1 <= number <= len(vacancies):
            return ("Не понял номер. Ответьте номером из списка:\n\n"
                    + render_vacancies(vacancies)), vars(state)
        chosen = vacancies[number - 1]
        state = funnel.select_vacancy(state, chosen["id"], chosen["screening_questions"])
        return (f"Отлично, {chosen['rank']} на {chosen['vessel_type']}.\n\n"
                + funnel.next_question(state)), vars(state)

    vacancy = db.get_vacancy(conn, state.vacancy_id) if state.vacancy_id else None
    question = funnel.next_question(state)

    # Router: встречный вопрос не сбивает воронку
    if question and brain.classify(message, question) == "вопрос":
        reply = brain.answer(message, KNOWLEDGE, _vacancy_text(vacancy))
        return f"{reply}\n\nВернёмся к анкете. {question}", vars(state)

    if state.step == funnel.COLLECTING_PROFILE:
        return _handle_profile(conn, message, state)

    if state.step == funnel.SCREENING:
        state = funnel.record_screening_answer(state, message)
        following = funnel.next_question(state)
        if following:
            return following, vars(state)
        return _offer_slots(conn, state)

    if state.step == funnel.CHOOSING_SLOT:
        return _handle_slot_choice(conn, message, state, vacancy)

    return "Не понял. Напишите ещё раз, пожалуйста.", vars(state)


def _handle_profile(conn, message: str, state):
    pending = [name for name, _ in funnel.PROFILE_FIELDS if name not in state.profile]
    parsed = brain.extract(message, pending)
    before = dict(state.profile)
    state = funnel.record_profile(state, parsed)

    # Модель ничего не распознала — записываем ответ в текущее поле как есть
    # и сохраняем сырой текст рекрутеру, чтобы разговор не зациклился
    # на одном вопросе, а исходная формулировка не потерялась.
    if state.profile == before and pending:
        state = funnel.record_profile(state, {pending[0]: message.strip()})
        state = funnel.add_note(state, f"{pending[0]}: {message.strip()}")

    # Контакт стал известен — проверяем, не записан ли моряк уже
    contact = state.profile.get("contact")
    if contact and "contact" not in before:
        active = db.find_active_application(conn, contact)
        if active:
            when = active["starts_at"].strftime("%d.%m в %H:%M")
            state = funnel.block(
                state,
                f"Вы уже записаны на интервью {when}. Второе интервью не нужно — "
                "если требуется перенос, напишите менеджеру.",
            )
            return state.blocked_reason, vars(state)

    if funnel.profile_complete(state):
        state = funnel.start_screening(state)
        return ("Спасибо! Теперь несколько вопросов по вакансии.\n\n"
                + funnel.next_question(state)), vars(state)
    return funnel.next_question(state), vars(state)


def _offer_slots(conn, state):
    slots = db.list_open_slots(conn)
    if not slots:
        return ("Свободных слотов сейчас нет — менеджер свяжется с вами "
                "и предложит время."), vars(state)
    state = funnel.offer_slots(state, [slot["id"] for slot in slots])
    return ("Выберите время интервью — ответьте номером:\n\n"
            + render_slots(slots)), vars(state)


def _handle_slot_choice(conn, message: str, state, vacancy):
    slots = db.list_open_slots(conn)
    number = _parse_number(message)
    try:
        if number is None:
            raise ValueError("нет номера")
        chosen = funnel.select_slot(state, number)
    except ValueError:
        state = funnel.offer_slots(state, [slot["id"] for slot in slots])
        return ("Такого номера нет. Вот свободное время:\n\n"
                + render_slots(slots)), vars(state)

    if not db.book_slot(conn, chosen.slot_id):
        state = funnel.offer_slots(state, [slot["id"] for slot in slots])
        return ("Этот слот только что заняли. Выберите другой:\n\n"
                + render_slots(slots)), vars(state)

    candidate_id = db.upsert_candidate(
        conn,
        chosen.profile.get("full_name", ""),
        chosen.profile.get("contact", ""),
        chosen.profile.get("citizenship", ""),
    )
    application_id = db.create_application(
        conn, candidate_id, chosen.vacancy_id, chosen.slot_id,
        chosen.profile, chosen.screening, chosen.notes,
    )
    # Вердикт — после сохранения: отказ модели не должен стоить кандидату брони.
    db.set_verdict(
        conn, application_id,
        brain.verdict(vacancy or {}, chosen.profile, chosen.screening),
    )

    when = next((slot["starts_at"] for slot in slots if slot["id"] == chosen.slot_id), None)
    when_text = when.strftime("%d.%m в %H:%M") if when else "выбранное время"
    chosen = funnel.confirm(chosen)
    return (f"✅ Записал вас на интервью {when_text}. "
            "Менеджер свяжется с вами по указанному контакту."), vars(chosen)


def candidate_chat(message, history, state_dict):
    return handle(to_plain_text(message), state_dict)
```

- [ ] **Step 2: Проверить, что модуль импортируется**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "from importlib import import_module; import_module('07-crewing_bot.app'); print('import ok')"
```

Ожидается: `import ok`

- [ ] **Step 3: Прогнать все тесты — ничего не сломалось**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests -v
```

Ожидается: 28 passed

- [ ] **Step 4: Коммит**

```bash
git add 07-crewing_bot/app.py
git commit -m "feat: вкладка кандидата — воронка от вакансии до брони слота

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Вкладка рекрутера и запуск приложения

**Files:**
- Modify: `07-crewing_bot/app.py`

**Interfaces:**
- Consumes: `db` из Task 4–5, `candidate_chat` из Task 7
- Produces:
  - `DEFAULT_SCREENING_QUESTIONS: list`
  - `check_password(entered: str) -> bool`
  - `recruiter_vacancies(password) -> str`
  - `recruiter_add_vacancy(password, rank, vessel_type, contract_months, salary_usd, requirements, questions_text) -> str`
  - `recruiter_open_slots(password, day_text, start_hhmm, end_hhmm, step_min) -> str`
  - `recruiter_applications(password) -> str`
  - `build_ui()` и блок `if __name__ == "__main__"`

- [ ] **Step 1: Дописать функции рекрутера**

Добавить в `07-crewing_bot/app.py`:

```python
DEFAULT_SCREENING_QUESTIONS = [
    "Сколько месяцев опыта в этой должности на таком типе судна?",
    "Какие STCW-сертификаты действующие и до какой даты?",
    "Есть ли действующий паспорт моряка (SID)?",
    "Есть ли виза US C1/D и шенген?",
    "Уровень английского — Marlins или CES, сколько процентов?",
    "До какой даты действует медкомиссия?",
]


def check_password(entered: str) -> bool:
    expected = os.getenv("RECRUITER_PASSWORD")
    return bool(expected) and entered == expected


def _guard(password: str):
    """Текст ошибки, если доступа нет, иначе None."""
    if not os.getenv("RECRUITER_PASSWORD"):
        return "⚠️ Не задан RECRUITER_PASSWORD в .env — вкладка закрыта."
    if not check_password(password):
        return "⚠️ Неверный пароль."
    return None


def recruiter_vacancies(password: str) -> str:
    error = _guard(password)
    if error:
        return error
    conn = db.connect()
    try:
        db.init_schema(conn)
        vacancies = db.list_active_vacancies(conn)
    finally:
        conn.close()
    if not vacancies:
        return "Вакансий пока нет."
    return "\n".join(
        f"#{v['id']} {v['rank']} — {v['vessel_type']}, {v['contract_months']} мес, "
        f"${v['salary_usd']}, вопросов скрининга: {len(v['screening_questions'])}"
        for v in vacancies
    )


def recruiter_add_vacancy(password, rank, vessel_type, contract_months,
                          salary_usd, requirements, questions_text) -> str:
    error = _guard(password)
    if error:
        return error
    if not rank or not vessel_type:
        return "⚠️ Должность и тип судна обязательны."
    questions = [line.strip() for line in (questions_text or "").splitlines() if line.strip()]
    conn = db.connect()
    try:
        db.init_schema(conn)
        vacancy_id = db.create_vacancy(
            conn, rank, vessel_type, int(contract_months), int(salary_usd),
            requirements, questions or DEFAULT_SCREENING_QUESTIONS,
        )
    finally:
        conn.close()
    return f"✅ Вакансия #{vacancy_id} создана."


def recruiter_open_slots(password, day_text, start_hhmm, end_hhmm, step_min) -> str:
    error = _guard(password)
    if error:
        return error
    from datetime import date as date_type
    try:
        day = date_type.fromisoformat((day_text or "").strip())
    except ValueError:
        return "⚠️ Дата в формате ГГГГ-ММ-ДД, например 2026-09-15."
    conn = db.connect()
    try:
        db.init_schema(conn)
        created = db.open_slots(conn, day, start_hhmm.strip(), end_hhmm.strip(), int(step_min))
    finally:
        conn.close()
    return f"✅ Открыто слотов: {created}."


def recruiter_applications(password: str) -> str:
    error = _guard(password)
    if error:
        return error
    conn = db.connect()
    try:
        db.init_schema(conn)
        rows = db.list_applications(conn)
    finally:
        conn.close()
    if not rows:
        return "Заявок пока нет."
    return "\n\n".join(
        f"#{row['id']} {row['full_name']} ({row['contact']}, {row['citizenship']})\n"
        f"  {row['rank']} — {row['vessel_type']}, интервью "
        f"{row['starts_at'].strftime('%d.%m %H:%M')}, статус {row['status']}\n"
        f"  опыт в должности: {row['rank_experience_months']} мес, "
        f"общий: {row['total_experience_months']} мес, "
        f"готов с {row['readiness_date']}\n"
        f"  вердикт: {row['verdict'] or '—'}"
        for row in rows
    )
```

- [ ] **Step 2: Собрать интерфейс и запуск**

Добавить в конец `07-crewing_bot/app.py`:

```python
def build_ui():
    with gr.Blocks(title="Крюинг-агентство «Меридиан»") as demo:
        with gr.Tab("Кандидат"):
            state = gr.State({})
            gr.ChatInterface(
                fn=candidate_chat,
                additional_inputs=[state],
                additional_outputs=[state],
                title="Запись на интервью",
                description="Выберите вакансию, ответьте на вопросы и заберите время интервью.",
            )

        with gr.Tab("Рекрутер"):
            password = gr.Textbox(label="Пароль", type="password")

            gr.Markdown("### Вакансии")
            vacancies_out = gr.Textbox(label="Открытые вакансии", lines=6)
            gr.Button("Показать вакансии").click(
                recruiter_vacancies, inputs=password, outputs=vacancies_out
            )

            rank = gr.Textbox(label="Должность", placeholder="2nd Engineer")
            vessel_type = gr.Textbox(label="Тип судна", placeholder="bulk carrier")
            contract_months = gr.Number(label="Контракт, мес", value=6)
            salary_usd = gr.Number(label="Ставка, $", value=6500)
            requirements = gr.Textbox(label="Требования", lines=3)
            questions_text = gr.Textbox(
                label="Вопросы скрининга — по одному в строке. Пусто = набор по умолчанию",
                lines=6,
            )
            add_out = gr.Textbox(label="Результат")
            gr.Button("Добавить вакансию").click(
                recruiter_add_vacancy,
                inputs=[password, rank, vessel_type, contract_months,
                        salary_usd, requirements, questions_text],
                outputs=add_out,
            )

            gr.Markdown("### Слоты интервью")
            day_text = gr.Textbox(label="Дата (ГГГГ-ММ-ДД)")
            start_hhmm = gr.Textbox(label="С", value="10:00")
            end_hhmm = gr.Textbox(label="До", value="17:00")
            step_min = gr.Number(label="Шаг, мин", value=30)
            slots_out = gr.Textbox(label="Результат")
            gr.Button("Открыть слоты").click(
                recruiter_open_slots,
                inputs=[password, day_text, start_hhmm, end_hhmm, step_min],
                outputs=slots_out,
            )

            gr.Markdown("### Заявки")
            applications_out = gr.Textbox(label="Заявки кандидатов", lines=20)
            gr.Button("Показать заявки").click(
                recruiter_applications, inputs=password, outputs=applications_out
            )
    return demo


if __name__ == "__main__":
    build_ui().launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7861)),
    )
```

- [ ] **Step 3: Запустить приложение и пройти сценарий руками**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe 07-crewing_bot/app.py
```

Открыть http://localhost:7861 и проверить по шагам:

1. Вкладка «Рекрутер», неверный пароль → «Неверный пароль», данных не видно.
2. Верный пароль → добавить вакансию, открыть слоты на завтра с 10:00 до 12:00.
3. Вкладка «Кандидат» → пройти воронку целиком до подтверждения времени.
4. В середине анкеты спросить «а какая зарплата?» → бот отвечает по вакансии и повторяет текущий вопрос, поля анкеты не сбрасываются.
5. Начать разговор заново с тем же контактом → бот говорит, что интервью уже назначено, и не даёт второй слот.
6. Вкладка «Рекрутер» → заявка видна с вердиктом и данными кандидата.

Если Gradio 6 ругается на `additional_outputs` у `ChatInterface` — заменить вкладку кандидата на связку `gr.Chatbot` + `gr.Textbox` + `gr.Button` с той же функцией `handle`, состояние держать в том же `gr.State`.

- [ ] **Step 4: Прогнать все тесты**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest 07-crewing_bot/tests -v
```

Ожидается: 28 passed

- [ ] **Step 5: Коммит**

```bash
git add 07-crewing_bot/app.py
git commit -m "feat: вкладка рекрутера — вакансии, слоты, заявки под паролем

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Документация и деплой на Render

**Files:**
- Create: `07-crewing_bot/README.md`
- Modify: `render.yaml`

**Interfaces:**
- Consumes: готовое приложение из Task 8
- Produces: рабочий деплой

- [ ] **Step 1: Написать README**

`07-crewing_bot/README.md`:

```markdown
# Крюинг-бот записи на интервью

Веб-чат крюингового агентства: моряк выбирает вакансию, проходит анкету и
скрининг, бронирует слот на интервью. Всё пишется в Postgres. Вторая
вкладка — рабочее место рекрутера под паролем.

## Паттерны внутри

| Паттерн | Где | Что делает |
|---|---|---|
| RAG | `knowledge.md` + `brain.answer` | отвечает по фактам агентства и вакансии |
| Router | `brain.classify` | различает ответ по анкете и встречный вопрос |
| Structured Output | `brain.extract` | свободный текст → поля анкеты |
| Tool Calling | `db.book_slot`, `db.create_application` | реальная бронь и запись в базу |
| Memory | `funnel.State` в `gr.State` | воронка помнит, на чём остановились |
| Judge | `brain.verdict` | оценка кандидата для менеджера |

## Запуск

```bash
uv pip install -r requirements.txt
# .env: GOOGLE_API_KEY, DATABASE_URL, RECRUITER_PASSWORD
python 07-crewing_bot/app.py
```

Откроется на http://localhost:7861

## Тесты

```bash
python -m pytest 07-crewing_bot/tests -v
```

Тесты базы идут против реальной базы из `DATABASE_URL` и **очищают
таблицы**. Заведи для них отдельный проект Supabase, не рабочий.

На Windows добавляй `PYTHONIOENCODING=utf-8` — иначе консоль падает на
кириллице в выводе.

## База данных

Четыре таблицы, создаются сами при первом запуске: `vacancies`, `slots`,
`candidates`, `applications`.

Две гарантии зашиты в базу, а не в код:
- слот нельзя забронировать дважды — условие `status='open'` прямо в `UPDATE`;
- у моряка не может быть двух назначенных интервью — частичный уникальный
  индекс по `candidate_id` при `status='new'`.

## Деплой

Сервис `crewing-ai` в `render.yaml`. В дашборде Render задать
`GOOGLE_API_KEY`, `DATABASE_URL`, `RECRUITER_PASSWORD`.

Если Render не подключается к Supabase по прямой строке — взять вариант
**Session pooler** из окна Connection string: прямое подключение идёт по
IPv6, а Render ходит по IPv4.
```

- [ ] **Step 2: Добавить сервис в render.yaml**

Дописать в конец списка `services:`:

```yaml
  - type: web
    name: crewing-ai
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: python 07-crewing_bot/app.py
    envVars:
      - key: PYTHON_VERSION
        value: "3.12.6"
      - key: GOOGLE_API_KEY
        sync: false
      - key: DATABASE_URL
        sync: false
      - key: RECRUITER_PASSWORD
        sync: false
```

- [ ] **Step 3: Проверить, что YAML валиден и сервисов два**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "import yaml;d=yaml.safe_load(open('render.yaml',encoding='utf-8'));print([s['name'] for s in d['services']])"
```

Ожидается: `['barber-ai', 'crewing-ai']`

Если `yaml` не установлен — `.venv/Scripts/python.exe -m uv pip install pyyaml`.

- [ ] **Step 4: Коммит**

```bash
git add 07-crewing_bot/README.md render.yaml
git commit -m "docs: README крюинг-бота и второй сервис в render.yaml

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Задеплоить и проверить**

В дашборде Render: Blueprint → применить, задать три переменные, дождаться
сборки. Открыть адрес сервиса, пройти сценарий кандидата целиком и
проверить вкладку рекрутера паролем.

Если подключение к базе не устанавливается — заменить `DATABASE_URL` на
строку Session pooler из Supabase и передеплоить.
