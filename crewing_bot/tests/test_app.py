"""Тесты склейки: handle() без Gradio, без базы и без сети.

Модули db и brain подменяются заглушками через monkeypatch, поэтому
проверяются именно решения app.py: что происходит с бронью, с состоянием
воронки и с ответом кандидату.
"""

import types
from datetime import datetime, timedelta

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

    def list_active_vacancies(self, conn):
        return list(self.vacancies)

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

    def classify(self, message, question):
        if self.down:
            return brain_module.ModelUnavailable("⚠️ модель недоступна")
        return "ответ"

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
    """Дойти до первого вопроса анкеты."""
    _, state = app.handle("привет", {})
    _, state = app.handle("1", state)
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
    bot.brain.classify = lambda message, question: "вопрос"

    reply, state = app.handle("а можно 2 марта?", state)

    assert "Ответ по фактам." in reply
    assert "1." in reply and "UTC" in reply
    assert bot.db.booked == []
    assert state["step"] == funnel.CHOOSING_SLOT
    assert state["slot_ids"] == [FIRST_SLOT_ID, FIRST_SLOT_ID + 1]


def test_counter_question_at_vacancy_step_is_answered(bot):
    """Роутер работает и на выборе вакансии (I1)."""
    bot.brain.classify = lambda message, question: "вопрос"
    _, state = app.handle("привет", {})

    reply, state = app.handle("а какая там зарплата?", state)

    assert "Ответ по фактам." in reply
    assert state["step"] == funnel.CHOOSING_VACANCY
    assert state["vacancy_id"] is None


def test_message_with_digits_is_not_a_vacancy_number(bot):
    """Номер 2 в списке существует — значит проверяется именно роутер."""
    assert len(bot.db.list_active_vacancies(None)) >= 2

    _, state = app.handle("привет", {})

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
    bot.brain.classify = lambda message, question: "ответ"

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


# --- слоты рекрутера (I10) --------------------------------------------------

def test_recruiter_open_slots_refuses_zero_step(bot, fresh_attempts):
    result = app.recruiter_open_slots("s3cret", "2026-10-01", "10:00", "17:00", 0)
    assert result.startswith("⚠️")


def test_recruiter_open_slots_refuses_non_number_step(bot, fresh_attempts):
    result = app.recruiter_open_slots("s3cret", "2026-10-01", "10:00", "17:00", None)
    assert result.startswith("⚠️")


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

    assert app.recruiter_vacancies("s3cret") == app.DB_SETUP_FAILED_TEXT
    assert app.recruiter_applications("s3cret") == app.DB_SETUP_FAILED_TEXT
    assert app.recruiter_add_vacancy(
        "s3cret", "AB", "container", 6, 1800, "", ""
    ) == app.DB_SETUP_FAILED_TEXT
    assert app.recruiter_open_slots(
        "s3cret", "2026-10-01", "10:00", "11:00", 30
    ) == app.DB_SETUP_FAILED_TEXT


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
