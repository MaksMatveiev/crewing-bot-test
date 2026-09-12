import os
import psycopg2
from datetime import date, timedelta

import pytest

from crewing_bot import db
from crewing_bot.tests.dbguard import same_database

# Только TEST_DATABASE_URL и никакого отката на DATABASE_URL: фикстура ниже
# делает TRUNCATE, и отката хватило бы, чтобы команда из README вычистила
# рабочую базу вместе с настоящими заявками кандидатов.
TEST_DB_URL = os.getenv("TEST_DATABASE_URL")
_LIVE_DB_URL = os.getenv("DATABASE_URL")

if same_database(TEST_DB_URL, _LIVE_DB_URL):
    # Молча пропустить нельзя: человек думает, что тесты идут, а они бы
    # стёрли рабочие данные.
    TEST_DB_URL = None
    _SKIP_REASON = (
        "TEST_DATABASE_URL ведёт в ту же базу, что и DATABASE_URL. "
        "Прогон стёр бы рабочие данные: нужна отдельная база."
    )
else:
    _SKIP_REASON = (
        "нужен TEST_DATABASE_URL — отдельная база: прогон делает TRUNCATE"
    )

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason=_SKIP_REASON)


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


# --- подбор вакансий по должности и типу флота ---

def _three_vacancies(conn):
    db.create_vacancy(conn, "2nd Engineer", "bulk carrier", 6, 6500, "опыт", ["STCW?"])
    db.create_vacancy(conn, "2nd Engineer", "tanker", 4, 7200, "опыт", ["STCW?"])
    db.create_vacancy(conn, "AB", "tanker", 9, 1800, "опыт", ["STCW?"])


def test_open_ranks_are_listed_without_duplicates(conn):
    _three_vacancies(conn)
    assert db.list_open_ranks(conn) == ["2nd Engineer", "AB"]


def test_vessel_types_are_limited_to_chosen_rank(conn):
    _three_vacancies(conn)
    assert db.list_open_vessel_types(conn, "2nd Engineer") == ["bulk carrier", "tanker"]
    assert db.list_open_vessel_types(conn, "AB") == ["tanker"]


def test_vacancies_are_filtered_by_rank_and_vessel(conn):
    _three_vacancies(conn)
    found = db.list_active_vacancies(conn, rank="2nd Engineer", vessel_type="tanker")
    assert len(found) == 1
    assert found[0]["salary_usd"] == 7200


def test_filter_without_matches_returns_empty(conn):
    _three_vacancies(conn)
    assert db.list_active_vacancies(conn, rank="AB", vessel_type="bulk carrier") == []


def test_listing_without_filter_returns_everything(conn):
    _three_vacancies(conn)
    assert len(db.list_active_vacancies(conn)) == 3


def test_inactive_vacancy_is_not_offered(conn):
    _three_vacancies(conn)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE vacancies SET is_active = FALSE WHERE rank = %s", ("AB",))
    assert db.list_open_ranks(conn) == ["2nd Engineer"]


def _mixed_vacancies(conn):
    """Две открытые вакансии и одна закрытая — рекрутер видит все три."""
    first = db.create_vacancy(conn, "2nd Engineer", "bulk carrier", 6, 6500,
                              "опыт от 12 мес", ["STCW?"])
    second = db.create_vacancy(conn, "AB", "tanker", 9, 1800,
                               "танкерный опыт", ["Танкеры?"])
    closed = db.create_vacancy(conn, "Cook", "container", 4, 1500,
                               "камбуз", ["Опыт?"])
    db.set_vacancy_active(conn, closed, False)
    return first, second, closed


def test_recruiter_sees_closed_vacancies_too(conn):
    """Кандидату закрытые не видны, рекрутеру — обязаны быть видны."""
    _mixed_vacancies(conn)

    assert len(db.list_active_vacancies(conn)) == 2
    assert len(db.list_all_vacancies(conn)) == 3


def test_only_open_and_only_closed(conn):
    first, second, closed = _mixed_vacancies(conn)

    open_ids = [v["id"] for v in db.list_all_vacancies(conn, only="open")]
    closed_ids = [v["id"] for v in db.list_all_vacancies(conn, only="closed")]

    assert sorted(open_ids) == sorted([first, second])
    assert closed_ids == [closed]


def test_search_looks_at_rank_vessel_and_requirements(conn):
    first, second, _ = _mixed_vacancies(conn)

    by_rank = db.list_all_vacancies(conn, search="engineer")
    by_vessel = db.list_all_vacancies(conn, search="TANKER")
    by_requirement = db.list_all_vacancies(conn, search="камбуз")

    assert [v["id"] for v in by_rank] == [first]
    assert [v["id"] for v in by_vessel] == [second]
    assert len(by_requirement) == 1


def test_search_without_matches_returns_empty(conn):
    _mixed_vacancies(conn)
    assert db.list_all_vacancies(conn, search="подводная лодка") == []


def test_blank_search_is_no_filter(conn):
    """Пустое поле поиска не должно ничего отсекать."""
    _mixed_vacancies(conn)
    assert len(db.list_all_vacancies(conn, search="   ")) == 3


def test_open_vacancies_come_first(conn):
    _, _, closed = _mixed_vacancies(conn)
    listed = db.list_all_vacancies(conn)
    assert listed[-1]["id"] == closed


def test_update_vacancy_changes_every_field(conn):
    vacancy_id = _vacancy(conn)

    changed = db.update_vacancy(conn, vacancy_id, "Bosun", "tanker", 8, 2400,
                                "новые требования", ["Первый?", "Второй?"])

    saved = db.get_vacancy(conn, vacancy_id)
    assert changed is True
    assert saved["rank"] == "Bosun"
    assert saved["vessel_type"] == "tanker"
    assert saved["contract_months"] == 8
    assert saved["salary_usd"] == 2400
    assert saved["requirements"] == "новые требования"
    assert saved["screening_questions"] == ["Первый?", "Второй?"]


def test_update_of_missing_vacancy_says_no(conn):
    assert db.update_vacancy(conn, 999999, "AB", "tanker", 6, 1800, "", ["?"]) is False


def test_closed_vacancy_disappears_from_chat_but_not_from_base(conn):
    """Закрытие — не удаление: запись остаётся, её просто не предлагают."""
    vacancy_id = _vacancy(conn)

    assert db.set_vacancy_active(conn, vacancy_id, False) is True
    assert db.list_active_vacancies(conn) == []
    assert db.get_vacancy(conn, vacancy_id)["is_active"] is False


def test_closed_vacancy_can_be_opened_again(conn):
    vacancy_id = _vacancy(conn)
    db.set_vacancy_active(conn, vacancy_id, False)

    assert db.set_vacancy_active(conn, vacancy_id, True) is True
    assert len(db.list_active_vacancies(conn)) == 1


def test_toggle_of_missing_vacancy_says_no(conn):
    assert db.set_vacancy_active(conn, 999999, False) is False


def test_application_survives_closing_its_vacancy(conn):
    """Заявка ссылается на вакансию — закрытие не должно её задеть.

    Ради этого удаления в панели и нет: стереть вакансию значило бы
    потерять или порвать заявки кандидатов.
    """
    vacancy_id = _vacancy(conn)
    slot_id = _future_slots(conn, 1)[0]
    candidate_id = db.upsert_candidate(conn, "Ivanov Ivan", "ivan@example.com", "Ukraine")
    db.book_slot(conn, slot_id)
    db.create_application(conn, candidate_id, vacancy_id, slot_id,
                          _profile(), {"Опыт?": "24"})

    db.set_vacancy_active(conn, vacancy_id, False)

    rows = db.list_applications(conn)
    assert len(rows) == 1
    assert rows[0]["rank"] == "AB"


def test_get_vacancy_tells_whether_it_is_open(conn):
    """Без этой колонки кандидату можно было бы открыть закрытую вакансию."""
    vacancy_id = _vacancy(conn)

    assert db.get_vacancy(conn, vacancy_id)["is_active"] is True

    db.set_vacancy_active(conn, vacancy_id, False)
    assert db.get_vacancy(conn, vacancy_id)["is_active"] is False
