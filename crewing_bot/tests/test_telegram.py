import json
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


def test_parse_start_ignores_string_chat():
    # Мусорный апдейт: chat — строка вместо словаря. Раньше `chat.get("id")`
    # падал с AttributeError, теперь должен просто возвращать None.
    assert telegram.parse_start({"message": {"chat": "555", "text": "/start abc"}}) is None


def test_parse_start_ignores_numeric_chat():
    # Мусорный апдейт: chat — число вместо словаря.
    assert telegram.parse_start({"message": {"chat": 555, "text": "/start abc"}}) is None


def test_parse_start_ignores_list_chat():
    # Мусорный апдейт: chat — список вместо словаря.
    assert telegram.parse_start({"message": {"chat": [555], "text": "/start abc"}}) is None


def test_send_message_returns_false_on_unserializable_text(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    # text — объект, который json.dumps не умеет сериализовать: TypeError
    # должен быть перехвачен внутри send_message, а не улететь наружу.
    assert telegram.send_message(555, object()) is False


# --- Объявления в канале ----------------------------------------------------


def _vacancy(**changes):
    vacancy = {
        "id": 7,
        "rank": "2nd Engineer",
        "vessel_type": "bulk carrier",
        "contract_months": 6,
        "salary_usd": 6500,
        "requirements": "Опыт от 12 месяцев",
        "is_active": True,
        "channel_message_id": None,
    }
    vacancy.update(changes)
    return vacancy


class _Answer:
    """Ответ Telegram с нужным телом."""

    def __init__(self, body, status=200):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(body, calls=None, status=200):
    def send(request, timeout=None):
        if calls is not None:
            calls.append((request.full_url.rsplit("/", 1)[-1],
                          json.loads(request.data.decode("utf-8"))))
        return _Answer(body, status)

    return send


def test_channel_needs_both_token_and_address(monkeypatch):
    """Без адреса канала публикация просто выключена."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.delenv("TELEGRAM_CHANNEL", raising=False)
    assert telegram.channel_configured() is False

    monkeypatch.setenv("TELEGRAM_CHANNEL", "@jobs")
    assert telegram.channel_configured() is True


def test_post_shows_what_decides_at_a_glance(monkeypatch):
    """Должность со ставкой сверху, данные судна ниже, контакты внизу."""
    monkeypatch.delenv("SITE_URL", raising=False)
    monkeypatch.delenv("AGENCY_EMAIL", raising=False)
    monkeypatch.delenv("AGENCY_PHONES", raising=False)

    post = telegram.build_vacancy_post(
        dict(_vacancy(), built_year="1996", dwt="3274",
             engine="Wartsila 8R32E", embarkation="ASAP"),
        "https://example.com")

    assert post.startswith("2nd Engineer - 6500 USD")
    assert "Date of Embarkation: ASAP" in post
    assert "Built: 1996" in post
    assert "bulk carrier - 3274 DWT" in post
    assert "COE duration: 6 +/- 1 months" in post
    assert "Wartsila 8R32E" in post
    assert "https://example.com" in post


def test_post_skips_empty_vessel_details(monkeypatch):
    """Пустое поле лучше пропустить, чем писать «Built: —»."""
    monkeypatch.delenv("AGENCY_EMAIL", raising=False)
    monkeypatch.delenv("AGENCY_PHONES", raising=False)

    post = telegram.build_vacancy_post(_vacancy())

    assert "Built:" not in post
    assert "DWT" not in post
    assert "Date of Embarkation: ASAP" in post


def test_post_carries_agency_contacts(monkeypatch):
    monkeypatch.setenv("AGENCY_EMAIL", "crew@example.com")
    monkeypatch.setenv("AGENCY_PHONES", "+38 073 111; +38 073 222")
    monkeypatch.setenv("SITE_URL", "https://example.com")

    post = telegram.build_vacancy_post(_vacancy())

    assert "crew@example.com" in post
    assert "+38 073 111" in post
    assert "+38 073 222" in post
    assert "https://example.com" in post


def test_post_lists_interview_questions(monkeypatch):
    """В объявление идёт всё, что о вакансии известно, — включая вопросы."""
    monkeypatch.delenv("AGENCY_EMAIL", raising=False)
    monkeypatch.delenv("AGENCY_PHONES", raising=False)

    post = telegram.build_vacancy_post(
        dict(_vacancy(), screening_questions=["Сколько опыта?", "Есть виза?"]))

    assert "На интервью спросим:" in post
    assert "• Сколько опыта?" in post
    assert "• Есть виза?" in post


def test_post_carries_requirements(monkeypatch):
    monkeypatch.delenv("AGENCY_EMAIL", raising=False)
    monkeypatch.delenv("AGENCY_PHONES", raising=False)

    post = telegram.build_vacancy_post(
        dict(_vacancy(), requirements="Опыт от 12 месяцев"))

    assert "Требования: Опыт от 12 месяцев" in post


def test_post_currency_is_configurable(monkeypatch):
    monkeypatch.setenv("SALARY_CURRENCY", "EURO")

    post = telegram.build_vacancy_post(_vacancy())

    assert post.startswith("2nd Engineer - 6500 EURO")


def test_closed_vacancy_is_marked_in_the_post():
    post = telegram.build_vacancy_post(_vacancy(is_active=False))

    assert post.startswith("🚫 Вакансия закрыта")


def test_publishing_sends_photo_and_returns_message_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@jobs")
    calls = []

    message_id = telegram.publish_vacancy(
        _vacancy(), "https://example.com/ship.jpg", "https://example.com",
        opener=_opener({"ok": True, "result": {"message_id": 42}}, calls))

    assert message_id == 42
    assert calls[0][0] == "sendPhoto"
    assert calls[0][1]["chat_id"] == "@jobs"
    assert calls[0][1]["photo"] == "https://example.com/ship.jpg"


def test_publishing_without_photo_falls_back_to_text(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@jobs")
    calls = []

    message_id = telegram.publish_vacancy(
        _vacancy(), "", "", opener=_opener({"ok": True,
                                            "result": {"message_id": 7}}, calls))

    assert message_id == 7
    assert calls[0][0] == "sendMessage"


def test_publishing_survives_a_refusal(monkeypatch):
    """Телеграм отказал — сохранение вакансии от этого падать не должно."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@jobs")

    def boom(request, timeout=None):
        raise OSError("сеть недоступна")

    assert telegram.publish_vacancy(_vacancy(), "", "", opener=boom) is None


def test_publishing_without_channel_does_nothing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.delenv("TELEGRAM_CHANNEL", raising=False)

    def boom(request, timeout=None):
        raise AssertionError("в канал ходить не должны")

    assert telegram.publish_vacancy(_vacancy(), "", "", opener=boom) is None


def test_existing_post_is_edited_not_reposted(monkeypatch):
    """Закрытую вакансию помечаем в старом посте, а не публикуем заново."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@jobs")
    calls = []

    done = telegram.update_vacancy_post(
        42, _vacancy(is_active=False), "https://example.com",
        opener=_opener({"ok": True, "result": {"message_id": 42}}, calls))

    assert done is True
    assert calls[0][0] == "editMessageCaption"
    assert calls[0][1]["message_id"] == 42
    assert "закрыта" in calls[0][1]["caption"]


def test_editing_without_message_id_does_nothing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@jobs")

    def boom(request, timeout=None):
        raise AssertionError("править нечего")

    assert telegram.update_vacancy_post(None, _vacancy(), opener=boom) is False
