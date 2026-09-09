import os
import psycopg2
from datetime import date, timedelta

import pytest

from crewing_bot import db

# Только TEST_DATABASE_URL и никакого отката на DATABASE_URL: фикстура ниже
# делает TRUNCATE, и отката хватило бы, чтобы команда из README вычистила
# рабочую базу вместе с настоящими заявками кандидатов.
TEST_DB_URL = os.getenv("TEST_DATABASE_URL")
MAIN_DB_URL = os.getenv("DATABASE_URL")


def _same_database() -> bool:
    """Указывают ли обе переменные на одну и ту же базу.

    Проверять имя переменной мало: строку подключения легко скопировать
    из DATABASE_URL по невнимательности, и тогда TRUNCATE в фикстуре
    честно вычистит рабочую базу. Сравниваем без краевых пробелов —
    строка, отличающаяся только ими, ведёт ровно туда же.
    """
    if not TEST_DB_URL or not MAIN_DB_URL:
        return False
    return TEST_DB_URL.strip() == MAIN_DB_URL.strip()


pytestmark = [
    pytest.mark.skipif(
        not TEST_DB_URL,
        reason=(
            "не задан TEST_DATABASE_URL. Нужна ОТДЕЛЬНАЯ тестовая база: прогон "
            "делает TRUNCATE таблиц, поэтому рабочую базу из DATABASE_URL "
            "использовать нельзя"
        ),
    ),
    pytest.mark.skipif(
        _same_database(),
        reason=(
            "TEST_DATABASE_URL совпадает с DATABASE_URL — это одна и та же "
            "база, а прогон делает TRUNCATE и стёр бы рабочие данные вместе "
            "с настоящими заявками кандидатов"
        ),
    ),
]


@pytest.fixture
def conn():
    """Чистая схема на каждый тест. База тестовая, данные в ней одноразовые."""
    connection = psycopg2.connect(TEST_DB_URL, options="-c timezone=UTC")
    db.init_schema(connection, force=True)
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


def test_release_slot_returns_it_to_open_list_and_is_noop_when_already_open(conn):
    slot_id = _future_slots(conn)[0]
    db.book_slot(conn, slot_id)
    assert slot_id not in [slot["id"] for slot in db.list_open_slots(conn)]

    assert db.release_slot(conn, slot_id) is True
    assert slot_id in [slot["id"] for slot in db.list_open_slots(conn)]

    # слот уже свободен — освобождать нечего
    assert db.release_slot(conn, slot_id) is False


def test_contact_in_other_case_is_the_same_person(conn):
    """Регистр контакта не должен плодить вторую строку кандидата."""
    first = db.upsert_candidate(conn, "Petrov Petr", "Ivan@Example.com ", "Ukraine")
    second = db.upsert_candidate(conn, "Petrov Petr", "ivan@example.com", "Ukraine")
    assert first == second

    with conn, conn.cursor() as cur:
        cur.execute("SELECT contact FROM candidates WHERE id = %s", (first,))
        # хранится нормализованный контакт
        assert cur.fetchone()[0] == "ivan@example.com"


def test_active_application_is_found_by_contact_in_other_case(conn):
    """Правило «одно активное интервью» не обходится сменой регистра."""
    vacancy_id = _vacancy(conn)
    slot_id = _future_slots(conn)[0]
    candidate_id = db.upsert_candidate(conn, "Petrov Petr", "ivan@example.com", "Ukraine")
    db.book_slot(conn, slot_id)
    db.create_application(conn, candidate_id, vacancy_id, slot_id, _profile(), {})

    assert db.find_active_application(conn, "  IVAN@Example.COM ") is not None


def test_open_slots_refuses_zero_step(conn):
    """Шаг 0 раньше вешал процесс бесконечным циклом."""
    with pytest.raises(ValueError):
        db.open_slots(conn, date.today() + timedelta(days=1), "10:00", "12:00", step_min=0)
    assert db.list_open_slots(conn) == []


def test_open_slots_refuses_negative_step(conn):
    with pytest.raises(ValueError):
        db.open_slots(conn, date.today() + timedelta(days=1), "10:00", "12:00", step_min=-30)


def test_open_slots_refuses_too_many_slots_at_once(conn):
    """Минутный шаг на сутки — это тысячи слотов, такую пачку не создаём."""
    with pytest.raises(ValueError):
        db.open_slots(conn, date.today() + timedelta(days=1), "00:00", "23:59", step_min=1)
    assert db.list_open_slots(conn) == []


def test_open_slots_refuses_broken_time(conn):
    with pytest.raises(ValueError):
        db.open_slots(conn, date.today() + timedelta(days=1), "10-00", "12:00", step_min=30)


def _legacy_duplicates(conn, vacancy_id, slot_ids):
    """Развернуть базу, пожившую со старым багом.

    Индекс по lower(contact) снимается: на легаси-базе его ещё нет — именно
    поэтому там и завелись две строки, отличающиеся только регистром почты.
    У каждой строки своя заявка со своим слотом.
    """
    with conn, conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS candidates_contact_lower")
        cur.execute(
            "INSERT INTO candidates (full_name, contact, citizenship)"
            " VALUES (%s, %s, %s) RETURNING id",
            ("Petrov Petr", "Ivan@Example.com", "Ukraine"),
        )
        older_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO candidates (full_name, contact, citizenship)"
            " VALUES (%s, %s, %s) RETURNING id",
            ("Petrov P.", " ivan@example.com ", "Ukraine"),
        )
        newer_id = cur.fetchone()[0]
    for candidate_id, slot_id in ((older_id, slot_ids[0]), (newer_id, slot_ids[1])):
        db.book_slot(conn, slot_id)
        db.create_application(conn, candidate_id, vacancy_id, slot_id, _profile(), {})
    return older_id, newer_id


def test_schema_init_survives_legacy_rows_differing_only_by_case(conn):
    """База, пожившая со старым багом, не должна ронять инициализацию.

    Две строки кандидата, отличающиеся только регистром контакта, — ровно
    то, что плодил старый код. На таких данных CREATE UNIQUE INDEX по
    lower(contact) падал, и бот вставал целиком: любое сообщение кандидата
    получало «⚠️ Сейчас не получилось обработать сообщение».
    """
    vacancy_id = _vacancy(conn)
    slot_ids = _future_slots(conn, count=2)
    older_id, _ = _legacy_duplicates(conn, vacancy_id, slot_ids)

    # сама инициализация проходит на легаси-данных
    db.init_schema(conn, force=True)

    # обе заявки на месте и висят на оставшейся строке кандидата
    with conn, conn.cursor() as cur:
        cur.execute("SELECT candidate_id FROM applications ORDER BY id")
        owners = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT contact FROM candidates ORDER BY id")
        contacts = [row[0] for row in cur.fetchall()]
    assert len(owners) == 2, "заявки не должны теряться при слиянии дубликатов"
    assert owners == [older_id, older_id]
    assert contacts == ["ivan@example.com"]

    # и запись по этому контакту снова работает в любом регистре
    first = db.upsert_candidate(conn, "Petrov Petr", "IVAN@Example.com ", "Ukraine")
    second = db.upsert_candidate(conn, "Petrov Petr", "ivan@example.com", "Ukraine")
    assert first == second == older_id


def test_migration_keeps_one_active_application_per_seaman(conn):
    """После слияния дубликатов активной остаётся ровно одна заявка.

    Иначе перенос заявок на одну строку кандидата нарушил бы частичный
    индекс one_active_application и миграция упала бы на своём же шаге.
    """
    vacancy_id = _vacancy(conn)
    slot_ids = _future_slots(conn, count=2)
    _legacy_duplicates(conn, vacancy_id, slot_ids)

    db.init_schema(conn, force=True)

    with conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM applications ORDER BY id")
        statuses = [row[0] for row in cur.fetchall()]
    assert statuses == ["new", "duplicate"]
    assert db.find_active_application(conn, "Ivan@Example.com") is not None


def test_connect_sets_utc_session_timezone(monkeypatch):
    """db.connect обязан сам ставить пояс сессии в UTC.

    Время слотов пишется наивным datetime в TIMESTAMPTZ и читается обратно
    в поясе сессии: без явного пояса подпись «UTC» в интерфейсе была бы
    верна только на базе, у которой пояс и так UTC.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    connection = db.connect()
    try:
        with connection.cursor() as cur:
            cur.execute("SHOW timezone")
            assert cur.fetchone()[0].upper() in ("UTC", "ETC/UTC")
    finally:
        connection.close()


def test_schema_is_prepared_once_per_process(conn):
    """init_schema не должна ходить в базу на каждый ход диалога."""
    db.init_schema(conn, force=True)
    with conn, conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS candidates_contact_lower")

    db.init_schema(conn)  # схема уже готова — повторный вызов ничего не делает

    with conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('candidates_contact_lower')")
        assert cur.fetchone()[0] is None

    db.init_schema(conn, force=True)  # вернуть базу в рабочее состояние
    with conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('candidates_contact_lower')")
        assert cur.fetchone()[0] is not None


def test_failed_init_is_not_remembered_as_success(conn, monkeypatch):
    """Упавшая инициализация не должна запоминаться: следующий вызов пробует снова."""
    monkeypatch.setattr(db, "_schema_ready", False)
    monkeypatch.setattr(db, "MIGRATIONS", ("SELECT 1 / 0",))

    with pytest.raises(psycopg2.Error):
        db.init_schema(conn)
    assert db._schema_ready is False

    monkeypatch.undo()
    db.init_schema(conn, force=True)
    assert db._schema_ready is True
