
from crewing_bot import brain


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


def test_verdict_prompt_has_no_personal_data():
    """ФИО и контакт для оценки под вакансию не нужны — в промпт не уходят."""
    seen = {}

    def fake_ask(prompt, **kwargs):
        seen["prompt"] = prompt
        return "Подходит"

    brain.verdict(
        {"rank": "AB", "vessel_type": "container", "requirements": "опыт 12 мес"},
        {
            "full_name": "Ivanov Ivan",
            "contact": "ivanov@example.com",
            "rank_experience_months": 24,
            "vessel_types": "container",
            "readiness_date": "2026-10-01",
        },
        {"STCW?": "да"},
        ask=fake_ask,
    )
    assert "Ivanov Ivan" not in seen["prompt"]
    assert "ivanov@example.com" not in seen["prompt"]
    # опыт, типы судов и дата готовности остаются
    assert "24" in seen["prompt"]
    assert "container" in seen["prompt"]
    assert "2026-10-01" in seen["prompt"]


def _unavailable(prompt, **kwargs):
    return brain.ModelUnavailable("⚠️ Модель не отвечает: timeout")


def test_classify_reports_model_failure_instead_of_guessing():
    result = brain.classify("Украина", "Гражданство?", ask=_unavailable)
    assert brain.is_unavailable(result) is True


def test_extract_reports_model_failure_instead_of_empty_dict():
    result = brain.extract("Украина", ["citizenship"], ask=_unavailable)
    assert brain.is_unavailable(result) is True
    assert result == {}


def test_ordinary_empty_extract_is_not_a_failure():
    """Пустой словарь от живой модели — не сбой, воронка идёт дальше."""
    result = brain.extract("что-то", ["full_name"], ask=lambda p, **k: "не понял")
    assert brain.is_unavailable(result) is False


def test_answer_passes_model_failure_through():
    assert brain.is_unavailable(brain.answer("а сколько платят?", "факты", "вакансия",
                                             ask=_unavailable)) is True


def test_ask_model_marks_missing_key_as_unavailable(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert brain.is_unavailable(brain.ask_model("привет")) is True


def test_ask_model_handles_none_response(monkeypatch):
    """Если модель вернула None (фильтр контента), ask_model возвращает предупреждение."""
    from unittest.mock import MagicMock

    # Подменяем GOOGLE_API_KEY
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-12345")

    # Создаём mock OpenAI клиента
    mock_openai_class = MagicMock()
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = None  # Модель вернула пустой ответ

    mock_client.chat.completions.create.return_value = mock_response
    mock_openai_class.return_value = mock_client

    # Подменяем OpenAI в модуле brain
    monkeypatch.setattr(brain, "OpenAI", mock_openai_class)

    result = brain.ask_model("test prompt")

    # ask_model должна вернуть строку с предупреждением, а не None
    assert result is not None
    assert isinstance(result, str)
    assert result.startswith("⚠️")
