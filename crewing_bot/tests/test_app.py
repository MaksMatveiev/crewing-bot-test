"""Тесты склейки: handle() без Gradio, без базы и без сети.

Модули db и brain подменяются заглушками через monkeypatch, поэтому
проверяются именно решения app.py: что происходит с бронью, с состоянием
воронки и с ответом кандидату.
"""

import types
from pathlib import Path
from datetime import date, datetime, timedelta

import pytest

from crewing_bot import app, funnel
from crewing_bot import brain as brain_module

FIRST_SLOT_ID = 100

PROFILE_ANSWERS = [
    "Ivanov Ivan",
    "ivan@example.com",
    "Ukraine",
    "24",
    "60",
    "container",
    "2026-10-01",
]


class FakeConn:
    def close(self):
        pass


class FakeDB:
    """Заглушка базы: помнит вызовы, никуда ничего не пишет."""

    def __init__(self, slot_count=2):
        self.vacancy = {
            "id": 1,
            "rank": "AB",
            "vessel_type": "container",
            "contract_months": 6,
            "salary_usd": 1800,
            "requirements": "опыт от 12 мес",
            "screening_questions": ["Действующий STCW?"],
        }
        # Вакансий заведомо больше одной: иначе номер «2» и так вне диапазона,
        # и тест про «а можно 2 контракта подряд?» проходил бы даже на коде,
        # который считает такое сообщение выбором вакансии.
        self.second_vacancy = {
            "id": 2,
            "rank": "2nd Engineer",
            "vessel_type": "bulk carrier",
            "contract_months": 4,
            "salary_usd": 6500,
            "requirements": "опыт на балкерах от 12 мес",
            "screening_questions": ["Виза US C1/D?"],
        }
        self.vacancies = [self.vacancy, self.second_vacancy]
        start = datetime(2026, 10, 1, 10, 0)
        self.slots = [
            {"id": FIRST_SLOT_ID + number, "starts_at": start + timedelta(minutes=30 * number)}
            for number in range(slot_count)
        ]
        self.active_application = None
        self.booked = []
        self.released = []
        self.applications = []
        self.verdicts = []
        self.fail_on_create_application = False
        self.fail_on_set_verdict = False

    def connect(self):
        return FakeConn()

    def init_schema(self, conn):
        pass

    def list_active_vacancies(self, conn, rank=None, vessel_type=None):
        found = list(self.vacancies)
        if rank:
            found = [v for v in found if v["rank"] == rank]
        if vessel_type:
            found = [v for v in found if v["vessel_type"] == vessel_type]
        return found

    def list_open_ranks(self, conn):
        return sorted({v["rank"] for v in self.vacancies})

    def list_open_vessel_types(self, conn, rank):
        return sorted({v["vessel_type"] for v in self.vacancies if v["rank"] == rank})

    def get_vacancy(self, conn, vacancy_id):
        for vacancy in self.vacancies:
            if vacancy["id"] == vacancy_id:
                return vacancy
        return None

    def list_open_slots(self, conn):
        return [slot for slot in self.slots if slot["id"] not in self.booked]

    def book_slot(self, conn, slot_id):
        if slot_id in self.booked:
            return False
        self.booked.append(slot_id)
        return True

    def release_slot(self, conn, slot_id):
        self.released.append(slot_id)
        if slot_id in self.booked:
            self.booked.remove(slot_id)
        return True

    def find_active_application(self, conn, contact):
        return self.active_application

    def upsert_candidate(self, conn, full_name, contact, citizenship):
        return 7

    def create_application(self, conn, candidate_id, vacancy_id, slot_id,
                           profile, screening, notes=""):
        if self.fail_on_create_application:
            raise RuntimeError("обрыв соединения")
        self.applications.append(
            {"slot_id": slot_id, "profile": dict(profile), "screening": dict(screening)}
        )
        return 42

    def set_verdict(self, conn, application_id, verdict):
        if self.fail_on_set_verdict:
            raise RuntimeError("обрыв соединения")
        self.verdicts.append((application_id, verdict))

    def application_token(self, conn, application_id):
        return "TESTTOKEN123456"


class FakeBrain:
    """Заглушка модели.

    По умолчанию считает любое сообщение ответом по анкете и кладёт его
    в первое незаполненное поле. Флаг down изображает недоступность.
    """

    ModelUnavailable = brain_module.ModelUnavailable
    UnavailableFields = brain_module.UnavailableFields
    is_unavailable = staticmethod(brain_module.is_unavailable)

    def __init__(self):
        self.down = False
        self.fail_verdict = False
        self.verdict_calls = []

    def looks_like_question(self, text):
        # Эвристика настоящая: она чистая, ходить никуда не надо, и
        # подменять её значило бы проверять заглушку вместо кода.
        return brain_module.looks_like_question(text)

    def extract(self, message, fields):
        if self.down:
            return brain_module.UnavailableFields()
        return {fields[0]: message.strip()} if fields else {}

    def answer(self, message, knowledge, vacancy_text):
        if self.down:
            return brain_module.ModelUnavailable("⚠️ модель недоступна")
        return "Ответ по фактам."

    def verdict(self, vacancy, profile, screening):
        self.verdict_calls.append(dict(profile))
        if self.fail_verdict:
            raise RuntimeError("обрыв соединения")
        if self.down:
            return brain_module.ModelUnavailable("⚠️ модель недоступна")
        return "Подходит."


@pytest.fixture
def bot(monkeypatch):
    fake_db, fake_brain = FakeDB(), FakeBrain()
    monkeypatch.setattr(app, "db", fake_db)
    monkeypatch.setattr(app, "brain", fake_brain)
    return types.SimpleNamespace(db=fake_db, brain=fake_brain)


def _to_profile():
    """Дойти до первого вопроса анкеты: должность, флот, вакансия."""
    _, state = app.handle("привет", {})       # приветствие и список должностей
    _, state = app.handle("AB", state)        # должность
    _, state = app.handle("container", state)  # тип флота
    _, state = app.handle("1", state)         # вакансия из подобранных
    return state


def _to_slot_list():
    """Пройти воронку до показанного списка слотов."""
    state = _to_profile()
    for answer in PROFILE_ANSWERS:
        _, state = app.handle(answer, state)
    reply, state = app.handle("да, STCW до 2027", state)
    return reply, state


# --- сценарий 1: счастливый путь -------------------------------------------

def _run_funnel_to_booking(monkeypatch):
    """Пройти воронку целиком и вернуть текст подтверждения брони."""
    fake_db, fake_brain = FakeDB(), FakeBrain()
    monkeypatch.setattr(app, "db", fake_db)
    monkeypatch.setattr(app, "brain", fake_brain)
    bot = types.SimpleNamespace(db=fake_db, brain=fake_brain)

    reply, state = _to_slot_list()
    assert "1." in reply and "UTC" in reply

    reply, state = app.handle("1", state)

    assert reply.startswith("✅")
    assert state["step"] == funnel.CONFIRMED
    assert len(bot.db.applications) == 1
    assert bot.db.applications[0]["slot_id"] == FIRST_SLOT_ID
    assert bot.db.booked == [FIRST_SLOT_ID]
    assert bot.db.released == []
    assert bot.db.verdicts == [(42, "Подходит.")]
    return reply


def test_happy_path_confirms_booking(monkeypatch):
    _run_funnel_to_booking(monkeypatch)


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


# --- сценарий 2: сбой при создании заявки ----------------------------------

def test_failed_application_releases_slot_and_does_not_confirm(bot):
    _, state = _to_slot_list()
    bot.db.fail_on_create_application = True

    reply, state = app.handle("1", state)

    assert "✅" not in reply
    assert bot.db.applications == []
    assert bot.db.released == [FIRST_SLOT_ID]
    assert FIRST_SLOT_ID not in bot.db.booked
    assert state["step"] == funnel.CHOOSING_SLOT


# --- сценарий 3: сбой при записи вердикта (C1) -----------------------------

def test_failed_verdict_keeps_booking_and_confirms(bot):
    """Заявка уже закоммичена — бронь не отменяем, слот не освобождаем."""
    _, state = _to_slot_list()
    bot.db.fail_on_set_verdict = True

    reply, state = app.handle("1", state)

    assert reply.startswith("✅")
    assert state["step"] == funnel.CONFIRMED
    assert len(bot.db.applications) == 1
    assert bot.db.released == []
    assert bot.db.booked == [FIRST_SLOT_ID]


def test_failed_verdict_call_keeps_booking_too(bot):
    """Отказ самой модели на шаге вердикта тоже не стоит кандидату брони."""
    _, state = _to_slot_list()
    bot.brain.fail_verdict = True

    reply, state = app.handle("1", state)

    assert reply.startswith("✅")
    assert len(bot.db.applications) == 1
    assert bot.db.released == []


def test_unavailable_model_leaves_verdict_empty_but_keeps_booking(bot):
    _, state = _to_slot_list()
    bot.brain.down = True

    reply, state = app.handle("1", state)

    assert reply.startswith("✅")
    assert len(bot.db.applications) == 1
    # предупреждение модели вместо оценки рекрутеру не пишем
    assert bot.db.verdicts == []


def test_verdict_gets_no_personal_data_from_app(bot):
    """ФИО и контакт не должны уезжать в оценку (I9)."""
    _, state = _to_slot_list()
    app.handle("1", state)

    sent = bot.brain.verdict_calls[0]
    assert sent["rank_experience_months"] == "24"
    # app отдаёт анкету целиком, обезличивание живёт в brain.verdict —
    # проверяем сам промпт
    seen = {}
    brain_module.verdict(
        bot.db.vacancy, sent, {"Действующий STCW?": "да"},
        ask=lambda prompt, **kwargs: seen.setdefault("prompt", prompt) or "Подходит",
    )
    assert "Ivanov Ivan" not in seen["prompt"]
    assert "ivan@example.com" not in seen["prompt"]


# --- сценарий 4: встречный вопрос с числом (C2) ----------------------------

def test_message_with_digits_is_not_a_slot_number(bot):
    """«а можно 2 марта?» не должно бронировать второй слот."""
    _, state = _to_slot_list()

    reply, state = app.handle("а можно 2 марта?", state)

    assert bot.db.booked == []
    assert bot.db.applications == []
    assert state["step"] == funnel.CHOOSING_SLOT


def test_counter_question_at_slot_step_is_answered_and_list_repeated(bot):
    """Роутер работает и на выборе слота (I1)."""
    _, state = _to_slot_list()

    reply, state = app.handle("а можно 2 марта?", state)

    assert "Ответ по фактам." in reply
    assert "1." in reply and "UTC" in reply
    assert bot.db.booked == []
    assert state["step"] == funnel.CHOOSING_SLOT
    assert state["slot_ids"] == [FIRST_SLOT_ID, FIRST_SLOT_ID + 1]


def test_counter_question_at_vacancy_step_is_answered(bot):
    """Роутер работает и на выборе вакансии (I1)."""
    _, state = app.handle("привет", {})
    _, state = app.handle("AB", state)
    _, state = app.handle("container", state)

    reply, state = app.handle("а какая там зарплата?", state)

    assert "Ответ по фактам." in reply
    assert state["step"] == funnel.CHOOSING_VACANCY
    assert state["vacancy_id"] is None


def test_counter_question_at_rank_step_is_answered(bot):
    """Роутер работает и на самом первом шаге — выборе должности."""
    _, state = app.handle("привет", {})

    reply, state = app.handle("а какая зарплата вообще?", state)

    assert "Ответ по фактам." in reply
    assert state["step"] == funnel.CHOOSING_RANK
    assert state["wanted_rank"] == ""


def test_counter_question_at_vessel_step_is_answered(bot):
    """И на выборе типа флота тоже."""
    _, state = app.handle("привет", {})
    _, state = app.handle("AB", state)

    reply, state = app.handle("а что за суда?", state)

    assert "Ответ по фактам." in reply
    assert state["step"] == funnel.CHOOSING_VESSEL_TYPE
    assert state["wanted_vessel_type"] == ""


def test_message_with_digits_is_not_a_vacancy_number(bot):
    """Номер 2 в списке существует — значит проверяется именно роутер."""
    assert len(bot.db.list_active_vacancies(None)) >= 2

    _, state = app.handle("привет", {})
    _, state = app.handle("AB", state)
    _, state = app.handle("container", state)

    reply, state = app.handle("а можно 2 контракта подряд?", state)

    assert state["step"] == funnel.CHOOSING_VACANCY
    assert state["vacancy_id"] is None


@pytest.mark.parametrize("message", ["а можно 2 марта?", "после 15 числа", "нет", "10:00", ""])
def test_parse_number_ignores_text_with_digits(message):
    assert app._parse_number(message) is None


@pytest.mark.parametrize("message,expected", [("2", 2), (" 3 ", 3), ("1.", 1), ("2)", 2)])
def test_parse_number_accepts_bare_number(message, expected):
    assert app._parse_number(message) == expected


# --- сценарий 5: у кандидата уже есть интервью -----------------------------

def test_candidate_with_active_interview_is_stopped(bot):
    bot.db.active_application = {"id": 5, "starts_at": datetime(2026, 10, 1, 10, 0)}
    state = _to_profile()

    _, state = app.handle("Ivanov Ivan", state)
    reply, state = app.handle("ivan@example.com", state)

    assert state["step"] == funnel.BLOCKED
    assert "01.10" in reply and "10:00" in reply
    assert bot.db.booked == []


# --- сценарий 6: модель недоступна (I2) ------------------------------------

def test_model_failure_does_not_fill_profile(bot):
    state = _to_profile()
    before = dict(state)
    bot.brain.down = True

    reply, state = app.handle("Ivanov Ivan", state)

    assert state == before
    assert state["profile"] == {}
    assert state["step"] == funnel.COLLECTING_PROFILE
    assert "Как вас зовут" in reply


def test_model_failure_in_extract_does_not_fill_profile(bot):
    """Роутер ответил, а extract сорвался — сырой текст в анкету не пишем."""
    state = _to_profile()
    before = dict(state)
    bot.brain.down = True

    reply, state = app.handle("Ivanov Ivan", state)

    assert state == before
    assert state["profile"] == {}
    assert state["notes"] == ""
    assert "Как вас зовут" in reply


def test_model_failure_does_not_reach_slot_booking(bot):
    """За время сбоя воронка не доходит до конца и не занимает слот."""
    state = _to_profile()
    bot.brain.down = True
    for message in PROFILE_ANSWERS + ["да", "1", "1"]:
        _, state = app.handle(message, state)

    assert state["step"] == funnel.COLLECTING_PROFILE
    assert state["profile"] == {}
    assert bot.db.booked == []
    assert bot.db.applications == []


# --- пароль рекрутера (I8) --------------------------------------------------

@pytest.fixture
def fresh_attempts(monkeypatch):
    monkeypatch.setenv("RECRUITER_PASSWORD", "s3cret")
    monkeypatch.setattr(app, "_password_attempts", {"failures": 0, "locked_until": 0.0})


def test_wrong_password_is_refused(fresh_attempts):
    assert app.check_password("не тот") is False
    assert app.check_password("s3cret") is True


def test_password_attempts_are_rate_limited(fresh_attempts):
    for _ in range(app.MAX_PASSWORD_ATTEMPTS - 1):
        assert app._guard("не тот") == "⚠️ Неверный пароль."

    assert "Подождите" in app._guard("не тот")
    # во время паузы пароль не проверяется вовсе — даже верный
    assert "Подождите" in app._guard("s3cret")


def test_correct_password_resets_counter(fresh_attempts):
    assert app._guard("не тот") is not None
    assert app._guard("s3cret") is None
    assert app._password_attempts["failures"] == 0


# --- инициализация схемы не должна ронять интерфейс -------------------------

def _break_schema_init(fake_db):
    def boom(conn):
        raise RuntimeError("миграция не прошла")
    fake_db.init_schema = boom


def test_broken_schema_init_answers_candidate_instead_of_crashing(bot):
    """Упавшая инициализация — понятный текст, а не ошибка интерфейса."""
    _break_schema_init(bot.db)

    reply, state = app.handle("привет", {})

    assert reply == app.DB_SETUP_FAILED_TEXT
    assert state["step"] == funnel.GREETING


def test_broken_schema_init_does_not_crash_recruiter_tab(bot, fresh_attempts):
    """У рекрутера обёртки раньше не было вовсе — падало прямо в Gradio."""
    _break_schema_init(bot.db)

    assert app.recruiter_browse("s3cret", "все")[1] == app.DB_SETUP_FAILED_TEXT
    assert app.recruiter_applications("s3cret") == app.DB_SETUP_FAILED_TEXT
    assert app.recruiter_save_vacancy(
        "s3cret", 0, "AB", "container", 6, 1800, "", ""
    ) == app.DB_SETUP_FAILED_TEXT
    assert app.recruiter_toggle_vacancy("s3cret", 5, False) == app.DB_SETUP_FAILED_TEXT

    title, message = app.recruiter_month("s3cret", 2026, 10)[:2]
    assert message == app.DB_SETUP_FAILED_TEXT


# --- вебхук Telegram ---------------------------------------------------------

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
    # Карточку (ФИО, контакт) чужому не показываем — но по приоритету 4
    # из ТЗ ему всё равно причитается вежливый отказ, а не полное молчание.
    assert all("Petrov" not in text for _, text in sent)


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


class _RaceLosingFakeDB(_TelegramFakeDB):
    """Имитирует гонку: владельца формально ещё нет, но UPDATE в базе
    его уже назначил кто-то другой — bind_telegram_chat возвращает False."""

    def bind_telegram_chat(self, conn, application_id, chat_id):
        return False


def test_lost_bind_race_gets_no_card(monkeypatch):
    # Два одновременных Start с одним кодом: проверка «владельца ещё нет»
    # проходит у обоих, но UPDATE с условием telegram_chat_id IS NULL
    # выигрывает только один. Проигравший не должен получить карточку.
    fake_db, sent = _RaceLosingFakeDB(_telegram_application()), []
    _patch_telegram(monkeypatch, fake_db, sent)

    update = {"message": {"chat": {"id": 555}, "text": "/start GOODTOKEN"}}
    assert app.handle_telegram_update(update, conn=None) == "foreign"
    assert all("Petrov" not in text for _, text in sent)


def test_login_rejects_wrong_password(fresh_attempts):
    stored, unlocked, message = app.recruiter_login("не тот")
    assert stored == ""
    assert unlocked is False
    assert "парол" in message.lower()


def test_login_accepts_right_password(fresh_attempts):
    """Вместо пароля наружу уходит ключ сессии, а не сам пароль."""
    stored, unlocked, message = app.recruiter_login("s3cret")

    assert unlocked is True
    assert message == ""
    assert stored != "s3cret"
    assert app.session_alive(stored) is True


def test_logout_closes_the_session(fresh_attempts):
    """После «Выйти» ключ перестаёт открывать панель."""
    stored, _, _ = app.recruiter_login("s3cret")

    app.recruiter_logout(stored)

    assert app.session_alive(stored) is False
    assert app._guard(stored) is not None


def test_session_key_opens_the_panel_without_password(fresh_attempts):
    """Пароль вводят один раз: дальше работает ключ."""
    stored, _, _ = app.recruiter_login("s3cret")

    assert app._guard(stored) is None


def test_page_reopens_the_panel_for_a_live_key(fresh_attempts):
    stored, _, _ = app.recruiter_login("s3cret")

    token, login_box, workspace, message, field = app.recruiter_restore(stored)

    assert token == stored
    assert login_box["visible"] is False
    assert workspace["visible"] is True


def test_page_asks_for_password_without_a_key(fresh_attempts):
    token, login_box, workspace, message, field = app.recruiter_restore("")

    assert token == ""
    assert login_box["visible"] is True
    assert workspace["visible"] is False


def test_stale_key_does_not_open_the_panel(fresh_attempts, monkeypatch):
    """Просроченный ключ равносилен отсутствию входа."""
    stored, _, _ = app.recruiter_login("s3cret")
    app._sessions[stored] = 0.0

    assert app.session_alive(stored) is False
    assert stored not in app._sessions


def test_login_without_configured_password_stays_closed(fresh_attempts, monkeypatch):
    monkeypatch.delenv("RECRUITER_PASSWORD", raising=False)
    stored, unlocked, message = app.recruiter_login("что угодно")
    assert stored == ""
    assert unlocked is False
    assert message


def test_login_counts_attempts_once_per_try(fresh_attempts):
    # Каждая попытка должна увеличивать счётчик ровно на единицу:
    # иначе пауза наступит вдвое раньше, чем обещано человеку.
    app.recruiter_login("не тот")
    assert app._password_attempts["failures"] == 1


# --- приветствие при открытии страницы ---

def test_opening_message_greets_and_asks_for_rank(bot):
    text, state = app.opening_message()

    assert "Здравствуйте" in text
    assert "должность" in text.lower()
    assert "2nd Engineer" in text and "AB" in text
    assert state["step"] == funnel.CHOOSING_RANK


def test_opening_message_without_vacancies_stays_at_greeting(bot):
    bot.db.vacancies = []
    text, state = app.opening_message()

    assert "ваканс" in text.lower()
    assert state["step"] == funnel.GREETING


def test_opening_message_survives_broken_database(bot):
    # База недоступна при открытии страницы — человек должен увидеть
    # внятный текст, а не пустой экран или ошибку интерфейса.
    def boom():
        raise RuntimeError("Нет DATABASE_URL")
    bot.db.connect = boom

    text, state = app.opening_message()

    assert text
    assert state["step"] == funnel.GREETING


def test_first_real_message_is_not_wasted_after_opening(bot):
    # Раньше первое сообщение уходило впустую: кандидат писал «привет»,
    # а в ответ получал список вакансий. Теперь список уже показан, и
    # первое сообщение — это выбор номера.
    _, state = app.opening_message()

    reply, state = app.handle("1", state)

    # Первое сообщение — уже выбор должности, а не потраченное впустую «привет».
    assert "Отлично" in reply
    assert state["wanted_rank"] == "2nd Engineer"
    assert state["step"] == funnel.CHOOSING_VESSEL_TYPE


# --- значки должностей и типов судов ---

@pytest.mark.parametrize("rank, expected", [
    ("Master", "🧭"),
    ("Chief Officer", "🧭"),
    ("2nd Officer", "🧭"),
    ("Chief Engineer", "⚙️"),
    ("2nd Engineer", "⚙️"),
    ("ETO", "💡"),
    ("Electrician", "💡"),
    ("AB", "⚓"),
    ("OS", "⚓"),
    ("Cook", "🧑\u200d🍳"),
])
def test_rank_gets_its_icon(rank, expected):
    assert app.rank_icon(rank) == expected


@pytest.mark.parametrize("vessel, expected", [
    ("bulk carrier", "🚢"),
    ("container", "📦"),
    ("tanker", "🛢️"),
    ("chemical tanker", "🧪"),
    ("LNG tanker", "🔥"),
    ("reefer", "❄️"),
])
def test_vessel_gets_its_icon(vessel, expected):
    assert app.vessel_icon(vessel) == expected


def test_unknown_rank_and_vessel_fall_back_to_anchor_and_ship():
    # Рекрутер волен завести любую должность — значок должен найтись всегда.
    assert app.rank_icon("Rigger") == "⚓"
    assert app.vessel_icon("FPSO") == "🚢"
    assert app.rank_icon("") == "⚓"
    assert app.vessel_icon(None) == "🚢"


def test_vacancy_line_shows_both_icons(bot):
    line = app.render_vacancies([bot.db.vacancy])

    assert "⚓" in line          # AB
    assert "📦" in line          # container
    assert "AB" in line and "container" in line
    assert "1." in line


# --- Панель вакансий рекрутера --------------------------------------------


def _row(vacancy_id, rank="2nd Engineer", vessel="bulk carrier", active=True):
    return {
        "id": vacancy_id,
        "rank": rank,
        "vessel_type": vessel,
        "contract_months": 6,
        "salary_usd": 6500,
        "requirements": "опыт",
        "screening_questions": ["STCW?", "Виза?"],
        "is_active": active,
    }


def test_card_shows_the_main_things():
    """Должность, флот, ставка и срок — всё, что решает на первом взгляде."""
    html = app.card_html(_row(1))

    assert "2nd Engineer" in html
    assert "bulk carrier" in html
    assert "$6500 / мес" in html
    assert "контракт 6 мес" in html


def test_closed_vacancy_is_marked_for_the_recruiter():
    assert "закрыта" in app.card_html(_row(2, active=False))
    assert "закрыта" not in app.card_html(_row(1))


def test_card_escapes_text_from_the_base():
    """Название заводит человек — разметку страницы оно ломать не должно."""
    html = app.card_html(_row(1, rank="<script>alert(1)</script>"))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_card_has_no_icons():
    """Значки в карточках мешают читать: ищут глазами должность."""
    html = app.card_html(_row(1))

    assert all(ord(char) < 0x2000 or char in "—·…" for char in html)


def test_rank_list_shows_how_many_places():
    """Кандидат не набирает должность руками — он видит её в списке."""
    choices = app.rank_choices([{"rank": "2nd Engineer", "count": 2},
                                {"rank": "AB", "count": 1}])

    assert choices[0] == ("Все должности (3)", "")
    assert choices[1] == ("2nd Engineer (2)", "2nd Engineer")
    assert choices[2] == ("AB (1)", "AB")


def test_rank_list_without_vacancies_is_just_the_total():
    assert app.rank_choices([]) == [("Все должности (0)", "")]


def test_grid_hides_unused_cards():
    """Карточек в сетке всегда MAX_CARDS — лишние прячутся, а не рисуются."""
    updates = app.fill_cards([_row(1), _row(2)])

    assert len(updates) == app.MAX_CARDS * 2
    assert updates[0]["visible"] is True
    assert updates[2]["visible"] is True
    assert updates[4]["visible"] is False
    assert updates[5]["value"] == ""


def test_browse_shows_open_vacancies(monkeypatch):
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "list_all_vacancies",
                        lambda conn, search=None, only=None: [_row(1), _row(2)])

    answer = app.candidate_browse("")

    assert answer[0] == [1, 2]
    assert "Открытых вакансий: 2" in answer[1]


def test_browse_without_matches_says_so(monkeypatch):
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "list_all_vacancies",
                        lambda conn, search=None, only=None: [])

    answer = app.candidate_browse("Master")

    assert answer[0] == []
    assert "вакансий нет" in answer[1]


def test_browse_asks_the_base_only_for_open_ones(monkeypatch):
    """Кандидату закрытая вакансия не должна попасться никогда."""
    asked = {}

    def list_all(conn, search=None, only=None):
        asked["search"], asked["only"] = search, only
        return []

    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "list_all_vacancies", list_all)

    app.candidate_browse("AB")

    assert asked == {"search": "AB", "only": "open"}


def test_grid_shows_no_more_than_it_has_places(monkeypatch):
    many = [_row(number) for number in range(1, app.MAX_CARDS + 6)]
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "list_all_vacancies",
                        lambda conn, search=None, only=None: many)

    answer = app.candidate_browse("")

    assert len(answer[0]) == app.MAX_CARDS
    assert str(len(many)) in answer[1]


def test_signup_click_opens_the_assistant(monkeypatch):
    """Нажатие на карточке открывает помощника и начинает анкету."""
    _candidate_db(monkeypatch, vacancy=_row(7),
                  days=[{"day": date(2026, 9, 15), "free": 12}])

    history, state, assistant = app._signup_click(0)([7], {})

    assert assistant["visible"] is True
    assert state["vacancy_id"] == 7
    assert "2nd Engineer" in history[0]["content"]


def test_signup_click_on_empty_place_does_nothing(monkeypatch):
    monkeypatch.setattr(app, "_open_conn",
                        lambda: (_ for _ in ()).throw(AssertionError))

    history, state, assistant = app._signup_click(5)([7], {})

    assert state == {}


def test_assistant_keeps_the_conversation(monkeypatch):
    """Свой маленький чат должен вести разговор не хуже готового."""
    monkeypatch.setattr(app, "handle",
                        lambda message, state: ("Понял вас.", {"step": "x"}))

    field, history, state = app.candidate_reply(
        "Ivanov Ivan", [{"role": "assistant", "content": "Как вас зовут?"}], {})

    assert field == ""
    assert history[-2] == {"role": "user", "content": "Ivanov Ivan"}
    assert history[-1] == {"role": "assistant", "content": "Понял вас."}
    assert state == {"step": "x"}


def test_assistant_ignores_an_empty_answer(monkeypatch):
    monkeypatch.setattr(app, "handle",
                        lambda message, state: (_ for _ in ()).throw(AssertionError))

    field, history, state = app.candidate_reply("   ", [], {})

    assert history == []


def _candidate_db(monkeypatch, vacancy=None, days=None):
    """База для карточек кандидата: что открыли и какие даты свободны."""
    days = [] if days is None else days
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "get_vacancy", lambda conn, vacancy_id: vacancy)
    monkeypatch.setattr(app.db, "list_open_days",
                        lambda conn, limit=14: list(days))


def test_sign_up_starts_the_funnel_on_the_chosen_vacancy(monkeypatch):
    vacancy = dict(_row(7), screening_questions=["Опыт на танкерах?"])
    _candidate_db(monkeypatch, vacancy=vacancy,
                  days=[{"day": date(2026, 9, 15), "free": 12}])

    history, state = app.candidate_sign_up(7, {})

    assert state["vacancy_id"] == 7
    assert state["step"] == funnel.COLLECTING_PROFILE
    assert state["wanted_rank"] == "2nd Engineer"
    assert funnel.PROFILE_FIELDS[0][1] in history[0]["content"]


def test_sign_up_refuses_when_no_day_is_green(monkeypatch):
    """Без открытых дней анкету не начинаем: отказ в конце обиднее."""
    _candidate_db(monkeypatch, vacancy=_row(7), days=[])

    history, state = app.candidate_sign_up(7, {})

    assert "Свободных дат" in history[0]["content"]
    assert state == {}


def test_sign_up_refuses_a_closed_vacancy(monkeypatch):
    _candidate_db(monkeypatch, vacancy=_row(7, active=False),
                  days=[{"day": date(2026, 9, 15), "free": 12}])

    history, state = app.candidate_sign_up(7, {})

    assert "уже закрыта" in history[0]["content"]
    assert state == {}


def test_recruiter_grid_refuses_wrong_password(monkeypatch):
    _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn",
                        lambda: (_ for _ in ()).throw(AssertionError))

    answer = app.recruiter_browse("не тот", "все")

    assert answer[0] == []
    assert "Неверный пароль" in answer[1]


def test_recruiter_grid_shows_closed_vacancies_too(monkeypatch):
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "list_all_vacancies",
                        lambda conn, search=None, only=None: [_row(1),
                                                              _row(2, active=False)])

    answer = app.recruiter_browse(password, "все")

    assert answer[0] == [1, 2]
    assert "Вакансий: 2" in answer[1]


def test_new_vacancy_form_opens_empty(monkeypatch):
    password = _unlocked(monkeypatch)

    chosen, form, title, message, *fields = app.recruiter_new_vacancy(password)

    assert chosen == 0
    assert form["visible"] is True
    assert "Новая вакансия" in title
    assert tuple(fields) == app.BLANK_FORM


def test_new_vacancy_form_stays_shut_without_password(monkeypatch):
    _unlocked(monkeypatch)

    chosen, form, title, message, *fields = app.recruiter_new_vacancy("не тот")

    assert form["visible"] is False
    assert "Неверный пароль" in message


def test_edit_form_opens_with_the_vacancy(monkeypatch):
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "get_vacancy",
                        lambda conn, vacancy_id: dict(_row(vacancy_id),
                                                      requirements="опыт"))

    chosen, form, title, message, *fields = app._edit_click(0)(password, [4])

    assert chosen == 4
    assert form["visible"] is True
    assert "№4" in title
    assert fields[0] == "2nd Engineer"


def test_cancel_closes_the_form():
    chosen, form, message = app.recruiter_close_form()

    assert chosen == 0
    assert form["visible"] is False


def test_status_filter_translates_labels():
    assert app._status_filter("открытые") == "open"
    assert app._status_filter("закрытые") == "closed"
    assert app._status_filter("все") is None


def _unlocked(monkeypatch):
    monkeypatch.setenv("RECRUITER_PASSWORD", "secret")
    app._password_attempts["failures"] = 0
    app._password_attempts["locked_until"] = 0.0
    return "secret"


def test_save_refuses_wrong_password(monkeypatch):
    _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (_ for _ in ()).throw(AssertionError))

    assert "Неверный пароль" in app.recruiter_save_vacancy(
        "не тот", 0, "AB", "tanker", 6, 1800, "", "")


def test_toggle_refuses_wrong_password(monkeypatch):
    _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (_ for _ in ()).throw(AssertionError))

    assert "Неверный пароль" in app.recruiter_toggle_vacancy("не тот", 5, False)


def test_save_requires_rank_and_vessel(monkeypatch):
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (_ for _ in ()).throw(AssertionError))

    assert "обязательны" in app.recruiter_save_vacancy(
        password, 0, "", "tanker", 6, 1800, "", "")


def test_save_rejects_words_instead_of_numbers(monkeypatch):
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (_ for _ in ()).throw(AssertionError))

    answer = app.recruiter_save_vacancy(
        password, 0, "AB", "tanker", "полгода", 1800, "", "")

    assert "целые числа" in answer


def test_new_vacancy_without_questions_gets_default_set(monkeypatch):
    password = _unlocked(monkeypatch)
    saved = {}

    def create_vacancy(conn, rank, vessel, months, salary, requirements, questions):
        saved["questions"] = questions
        return 42

    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "create_vacancy", create_vacancy)
    monkeypatch.setattr(app.db, "update_vessel_details",
                        lambda *args, **kwargs: None)

    answer = app.recruiter_save_vacancy(
        password, 0, "AB", "tanker", 6, 1800, "опыт", "   ")

    assert saved["questions"] == app.DEFAULT_SCREENING_QUESTIONS
    assert "#42" in answer


def test_chosen_vacancy_is_updated_not_duplicated(monkeypatch):
    """С выбранной вакансией кнопка правит её, а не заводит вторую такую же."""
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "create_vacancy",
                        lambda *args: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(app.db, "update_vacancy", lambda *args: True)
    monkeypatch.setattr(app.db, "update_vessel_details",
                        lambda *args, **kwargs: None)

    assert "#5 обновлена" in app.recruiter_save_vacancy(
        password, 5, "AB", "tanker", 6, 1800, "опыт", "Вопрос?")


def test_saving_a_vanished_vacancy_says_so(monkeypatch):
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "update_vacancy", lambda *args: False)

    assert "больше нет" in app.recruiter_save_vacancy(
        password, 5, "AB", "tanker", 6, 1800, "", "Вопрос?")


def test_toggle_needs_a_chosen_vacancy(monkeypatch):
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (_ for _ in ()).throw(AssertionError))

    assert "выберите вакансию" in app.recruiter_toggle_vacancy(password, 0, False).lower()


def test_closing_and_reopening_report_what_happened(monkeypatch):
    password = _unlocked(monkeypatch)
    asked = []
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "set_vacancy_active",
                        lambda conn, vacancy_id, active: asked.append(active) or True)

    closed = app.recruiter_toggle_vacancy(password, 5, False)
    opened = app.recruiter_toggle_vacancy(password, 5, True)

    assert asked == [False, True]
    assert "закрыта" in closed
    assert "открыта" in opened


def test_panel_has_no_way_to_delete_a_vacancy():
    """Удаления нет нигде: на вакансию ссылаются заявки кандидатов."""
    assert not [name for name in dir(app) if "delete" in name.lower()]
    assert not hasattr(app.db, "delete_vacancy")


def test_form_is_cleared_for_a_new_vacancy(monkeypatch):
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (_ for _ in ()).throw(AssertionError))

    assert app.recruiter_load_vacancy(password, 0) == app.BLANK_FORM


def test_chosen_vacancy_fills_the_form(monkeypatch):
    password = _unlocked(monkeypatch)
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(
        app.db, "get_vacancy",
        lambda conn, vacancy_id: dict(_row(vacancy_id), built_year="1996",
                                      dwt="3274", engine="Wartsila 8R32E",
                                      embarkation="ASAP"))

    (rank, vessel, months, salary, requirements, questions,
     built_year, dwt, engine, embarkation) = app.recruiter_load_vacancy(
        password, 3)

    assert rank == "2nd Engineer"
    assert vessel == "bulk carrier"
    assert (months, salary) == (6, 6500)
    assert questions == "STCW?\nВиза?"
    assert (built_year, dwt, engine, embarkation) == (
        "1996", "3274", "Wartsila 8R32E", "ASAP")


def test_saving_writes_vessel_details(monkeypatch):
    """Данные судна нужны объявлению в канале — они должны сохраняться."""
    password = _unlocked(monkeypatch)
    saved = {}
    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "update_vacancy", lambda *args: True)
    monkeypatch.setattr(
        app.db, "update_vessel_details",
        lambda conn, vacancy_id, built_year, dwt, engine, embarkation:
        saved.update(id=vacancy_id, built_year=built_year, dwt=dwt,
                     engine=engine, embarkation=embarkation))

    app.recruiter_save_vacancy(password, 5, "AB", "tanker", 6, 1800, "опыт",
                               "Вопрос?", "1996", "3274", "Wartsila", "ASAP")

    assert saved == {"id": 5, "built_year": "1996", "dwt": "3274",
                     "engine": "Wartsila", "embarkation": "ASAP"}


# --- Календарь интервью -----------------------------------------------------


def test_month_grid_starts_on_monday():
    """1 сентября 2026 — вторник, значит первая клетка пустая."""
    cells = app.month_cells(2026, 9)

    assert cells[0] is None
    assert cells[1] == 1
    assert cells[30] == 30
    assert len(cells) == app.CALENDAR_CELLS


def test_month_grid_holds_any_month():
    for year, month in ((2026, 2), (2026, 8), (2024, 2), (2026, 11)):
        cells = app.month_cells(year, month)
        numbers = [cell for cell in cells if cell is not None]
        assert numbers == list(range(1, len(numbers) + 1))
        assert len(cells) == app.CALENDAR_CELLS


def test_month_shift_crosses_the_year():
    assert app.shift_month(2026, 12, 1) == (2027, 1)
    assert app.shift_month(2026, 1, -1) == (2025, 12)
    assert app.shift_month(2026, 9, 2) == (2026, 11)


def test_month_title_is_readable():
    assert app.month_title(2026, 9) == "Сентябрь 2026"


# --- Окно вакансий у кандидата ----------------------------------------------


def test_sign_up_asks_the_first_profile_question(monkeypatch):
    _candidate_db(monkeypatch, vacancy=_row(7),
                  days=[{"day": date(2026, 9, 15), "free": 12}])

    history, state = app.candidate_sign_up(7, {})

    assert funnel.PROFILE_FIELDS[0][1] in history[0]["content"]


# --- Ежедневная выкладка вакансий в канал -----------------------------------


def _digest_db(monkeypatch, mark=None, vacancies=None, posted=None,
               edited=None, saved=None):
    """База и Telegram для ежедневной выкладки."""
    vacancies = [] if vacancies is None else vacancies
    posted = [] if posted is None else posted
    edited = [] if edited is None else edited
    saved = {} if saved is None else saved
    marks = {"value": mark}

    monkeypatch.setattr(app, "_open_conn", lambda: (FakeConn(), None))
    monkeypatch.setattr(app.db, "connect", lambda: FakeConn())
    monkeypatch.setattr(app.db, "init_schema", lambda conn, force=False: None)
    monkeypatch.setattr(app.db, "list_all_vacancies",
                        lambda conn, search=None, only=None: list(vacancies))
    monkeypatch.setattr(app.db, "get_setting",
                        lambda conn, key: marks["value"])
    monkeypatch.setattr(app.db, "set_setting",
                        lambda conn, key, value: marks.update(value=value))
    monkeypatch.setattr(app.db, "set_channel_message",
                        lambda conn, vacancy_id, message_id:
                        saved.update({vacancy_id: message_id}))
    monkeypatch.setattr(app.telegram, "channel_configured", lambda: True)
    monkeypatch.setattr(app.telegram, "publish_vacancy",
                        lambda vacancy, photo="", site="":
                        posted.append(vacancy["id"]) or 100 + vacancy["id"])
    monkeypatch.setattr(app.telegram, "update_vacancy_post",
                        lambda message_id, vacancy, site="":
                        edited.append(message_id) or True)
    return marks


def test_digest_waits_for_the_appointed_hour(monkeypatch):
    """До назначенного часа канал не трогаем."""
    _digest_db(monkeypatch)

    early = datetime(2026, 9, 13, app.PUBLISH_HOUR - 1, 30)

    assert app.daily_publication_due(FakeConn(), early) is False


def test_digest_runs_after_the_appointed_hour(monkeypatch):
    _digest_db(monkeypatch)

    late = datetime(2026, 9, 13, app.PUBLISH_HOUR, 5)

    assert app.daily_publication_due(FakeConn(), late) is True


def test_digest_runs_once_a_day(monkeypatch):
    """Отметка в базе: сервис засыпает и просыпается, повтор не нужен."""
    _digest_db(monkeypatch, mark="2026-09-13")

    same_day = datetime(2026, 9, 13, app.PUBLISH_HOUR + 3, 0)

    assert app.daily_publication_due(FakeConn(), same_day) is False


def test_digest_runs_again_next_day(monkeypatch):
    _digest_db(monkeypatch, mark="2026-09-12")

    next_day = datetime(2026, 9, 13, app.PUBLISH_HOUR, 1)

    assert app.daily_publication_due(FakeConn(), next_day) is True


def test_digest_publishes_every_open_vacancy(monkeypatch):
    posted, saved = [], {}
    _digest_db(monkeypatch, vacancies=[_row(1), _row(2)], posted=posted,
               saved=saved)

    count = app.publish_open_vacancies(FakeConn(), datetime(2026, 9, 13, 8, 0))

    assert count == 2
    assert posted == [1, 2]
    assert saved == {1: 101, 2: 102}


def test_digest_edits_already_published(monkeypatch):
    """Вчерашнее объявление правится, а не публикуется заново."""
    posted, edited = [], []
    _digest_db(monkeypatch,
               vacancies=[dict(_row(1), channel_message_id=55)],
               posted=posted, edited=edited)

    count = app.publish_open_vacancies(FakeConn(), datetime(2026, 9, 13, 8, 0))

    assert count == 1
    assert edited == [55]
    assert posted == []


def test_digest_marks_the_day_as_done(monkeypatch):
    marks = _digest_db(monkeypatch, vacancies=[_row(1)])

    app.publish_open_vacancies(FakeConn(), datetime(2026, 9, 13, 8, 0))

    assert marks["value"] == "2026-09-13"


def test_digest_stays_quiet_without_a_channel(monkeypatch):
    monkeypatch.setattr(app.telegram, "channel_configured", lambda: False)
    monkeypatch.setattr(app.db, "connect",
                        lambda: (_ for _ in ()).throw(AssertionError))

    assert app.run_daily_publication() == 0


def test_publish_hour_follows_the_chosen_timezone(monkeypatch):
    """«Восемь утра» рекрутер понимает по своим часам, а не по UTC."""
    monkeypatch.setenv("PUBLISH_TIMEZONE", "Europe/Kyiv")

    local = app.publish_now()
    utc = datetime.utcnow()

    assert abs((local - utc).total_seconds()) > 3000


def test_unknown_timezone_falls_back_to_utc(monkeypatch):
    """Опечатка в названии пояса не должна ронять публикацию."""
    monkeypatch.setenv("PUBLISH_TIMEZONE", "Europe/Несуществующий")

    assert abs((app.publish_now() - datetime.utcnow()).total_seconds()) < 5


def test_without_timezone_we_stay_on_utc(monkeypatch):
    monkeypatch.delenv("PUBLISH_TIMEZONE", raising=False)

    assert abs((app.publish_now() - datetime.utcnow()).total_seconds()) < 5
