"""Проверка защиты, которая не пускает тесты в рабочую базу.

Базы здесь нет и не нужно: сравниваются строки подключения. Поэтому
тесты идут всегда, даже когда тесты базы пропущены.
"""

from crewing_bot.tests.dbguard import same_database


def test_guard_spots_same_database_behind_different_text():
    """Защита обязана видеть одну базу за разными строками.

    Параметр в конце строки её не меняет — именно на этом рабочая база
    однажды и была вычищена.
    """
    base = "postgresql://user:pass@db.example.com:5432/postgres"
    assert same_database(base, base + "?application_name=tests") is True
    assert same_database(base, base + "?sslmode=require") is True


def test_guard_lets_through_a_different_database():
    first = "postgresql://user:pass@db.example.com:5432/postgres"
    second = "postgresql://user:pass@test.example.com:5432/postgres"
    third = "postgresql://user:pass@db.example.com:5432/other"

    assert same_database(first, second) is False
    assert same_database(first, third) is False


def test_guard_treats_missing_value_as_different():
    assert same_database(None, "postgresql://u:p@h:5432/d") is False
    assert same_database("", "") is False


def test_guard_separates_users_on_one_host():
    """Пулер Supabase отличает базы пользователем в строке подключения."""
    first = "postgresql://postgres.aaa:pass@pooler.example.com:5432/postgres"
    second = "postgresql://postgres.bbb:pass@pooler.example.com:5432/postgres"

    assert same_database(first, second) is False
