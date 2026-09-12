"""Защита рабочей базы от прогона тестов.

Живёт отдельным модулем, а не внутри test_db.py, нарочно: тесты базы
целиком пропускаются, когда нет TEST_DATABASE_URL, и вместе с ними
пропускалась бы проверка самой защиты — как раз в том случае, ради
которого она написана.
"""

from urllib.parse import urlsplit


def same_database(first, second) -> bool:
    """Ведут ли две строки подключения в одну и ту же базу.

    Сравнивать их как текст недостаточно: строки могут отличаться
    параметрами вроде ?application_name=... и при этом указывать на одно
    место. Именно так рабочая база однажды и была вычищена прогоном
    тестов — защита сравнивала текст и пропустила.
    """
    def key(url):
        parts = urlsplit(url or "")
        return (parts.hostname, parts.port, parts.path, parts.username)

    return bool(first) and bool(second) and key(first) == key(second)
