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
