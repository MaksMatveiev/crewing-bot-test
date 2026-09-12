"""Вся работа с Postgres: схема, вакансии, слоты, бронь, заявки.

Модуль не знает про модель — здесь только данные.
Все запросы параметризованы через %s: подстановка f-строкой в SQL
открыла бы инъекцию.
"""

import json
import os
import secrets
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

# Предохранители «Открыть слоты»: шаг должен быть положительным, иначе цикл
# в open_slots никогда не закончится, а размер пачки — конечным, чтобы
# опечатка в поле формы не забила базу.
MAX_SLOTS_PER_CALL = 200


# Миграция под уникальный индекс по lower(contact). Выполняется до создания
# самого индекса: на базе, которая жила со старым багом, в candidates уже
# могут лежать строки, отличающиеся только регистром контакта, и CREATE
# UNIQUE INDEX на таких данных падает, унося за собой всю инициализацию.
#
# Порядок ровно такой, иначе каждый шаг ломает следующий:
#   1) в группе строк с одинаковым нормализованным контактом лишние активные
#      заявки переводятся в статус 'duplicate' — иначе перенос заявок на одну
#      строку кандидата нарушит частичный индекс one_active_application;
#   2) все заявки дубликатов переносятся на самую раннюю строку группы (min id),
#      поэтому ни одна заявка и её история не теряются;
#   3) опустевшие строки-дубликаты удаляются — только после переноса заявок,
#      внешний ключ applications.candidate_id остаётся целым;
#   4) контакты приводятся к нижнему регистру без краевых пробелов. После
#      шага 3 конфликтов больше нет, поэтому UPDATE не нарушит UNIQUE(contact).
_GROUPS_CTE = """
WITH grp AS (
    SELECT id, lower(trim(contact)) AS key FROM candidates
),
keepers AS (
    SELECT key, min(id) AS keep_id FROM grp GROUP BY key
)
"""

MIGRATIONS = (
    # 1. лишние активные заявки дубликатов уходят в 'duplicate'
    """
    WITH grp AS (
        SELECT id, lower(trim(contact)) AS key FROM candidates
    ),
    ranked AS (
        SELECT a.id, row_number() OVER (PARTITION BY g.key ORDER BY a.id) AS rn
        FROM applications a
        JOIN grp g ON g.id = a.candidate_id
        WHERE a.status = 'new'
    )
    UPDATE applications a SET status = 'duplicate'
    FROM ranked WHERE a.id = ranked.id AND ranked.rn > 1
    """,
    # 2. заявки дубликатов переезжают на самую раннюю строку кандидата
    _GROUPS_CTE + """
    UPDATE applications a SET candidate_id = k.keep_id
    FROM grp g JOIN keepers k ON k.key = g.key
    WHERE a.candidate_id = g.id AND g.id <> k.keep_id
    """,
    # 3. опустевшие дубликаты удаляются
    _GROUPS_CTE + """
    DELETE FROM candidates c
    USING grp g JOIN keepers k ON k.key = g.key
    WHERE c.id = g.id AND g.id <> k.keep_id
    """,
    # 4. контакты в каноническом виде
    """
    UPDATE candidates SET contact = lower(trim(contact))
    WHERE contact <> lower(trim(contact))
    """,
)

# Контакт узнаётся независимо от регистра. Колонка contact объявлена UNIQUE
# и остаётся такой: ON CONFLICT (contact) в upsert_candidate опирается
# именно на неё. Индекс ниже не конфликтует с ней, а страхует от строк,
# заведённых мимо upsert_candidate: код всегда пишет контакт в нижнем
# регистре, поэтому для его строк оба ограничения совпадают.
CONTACT_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS candidates_contact_lower
    ON candidates (lower(contact))
"""

# Колонки для Telegram добавляются отдельно от SCHEMA: таблица applications
# у работающих проектов уже создана, а CREATE TABLE IF NOT EXISTS её не
# меняет. UNIQUE по nullable-колонке в Postgres допускает сколько угодно
# NULL — заявки, созданные до появления фичи, останутся без кода.
COLUMN_MIGRATIONS = (
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS telegram_token TEXT",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT",
    "CREATE UNIQUE INDEX IF NOT EXISTS applications_telegram_token"
    " ON applications (telegram_token)",
    # Номер объявления в канале: по нему пост правится, а не публикуется
    # заново при каждом сохранении вакансии.
    "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS channel_message_id BIGINT",
)

# Схема одна на процесс: init_schema вызывается на каждый ход диалога и на
# каждое действие рекрутера, а создавать таблицы и гонять миграцию на каждое
# сообщение — лишняя нагрузка на базу и лишний повод упасть на горячем пути.
# Флаг взводится только после успеха, поэтому неудачная первая попытка
# не запоминается и следующий вызов попробует снова.
_schema_ready = False


def connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Нет DATABASE_URL. Supabase → Settings → Database → Connection string → URI."
        )
    # Часовой пояс сессии задаём явно: время слотов пишется наивным datetime
    # в TIMESTAMPTZ и читается обратно в поясе сессии. Без этого подпись «UTC»
    # в интерфейсе была бы верна только на базе, у которой пояс и так UTC.
    return psycopg2.connect(url, options="-c timezone=UTC")


def init_schema(conn, force: bool = False) -> None:
    """Создать таблицы, домигрировать контакты и поставить индексы.

    За процесс выполняется один раз: force=True нужен тестам, которые
    заводят легаси-данные уже после первой инициализации.
    """
    global _schema_ready
    if _schema_ready and not force:
        return
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        for statement in COLUMN_MIGRATIONS:
            cur.execute(statement)
        for statement in MIGRATIONS:
            cur.execute(statement)
        cur.execute(CONTACT_INDEX)
    _schema_ready = True


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


def list_active_vacancies(conn, rank: str = None, vessel_type: str = None) -> list:
    """Открытые вакансии, при желании суженные должностью и типом судна.

    Фильтры необязательные: без них отдаём всё, как и раньше.
    """
    where = ["is_active"]
    params = []
    if rank:
        where.append("rank = %s")
        params.append(rank)
    if vessel_type:
        where.append("vessel_type = %s")
        params.append(vessel_type)

    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, rank, vessel_type, contract_months, salary_usd,"
            " requirements, screening_questions FROM vacancies"
            " WHERE " + " AND ".join(where) + " ORDER BY id",
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def list_all_vacancies(conn, search: str = None, only: str = None) -> list:
    """Вакансии для рекрутера — включая закрытые.

    search — поиск по должности, типу судна и требованиям.
    only — "open" или "closed"; всё остальное означает «показать все».

    Отличается от list_active_vacancies тем, что кандидату закрытые
    вакансии не видны никогда, а рекрутеру они нужны: по ним есть
    заявки и история.
    """
    where = []
    params = []
    if only == "open":
        where.append("is_active")
    elif only == "closed":
        where.append("NOT is_active")
    if search and search.strip():
        where.append("(rank ILIKE %s OR vessel_type ILIKE %s OR requirements ILIKE %s)")
        pattern = f"%{search.strip()}%"
        params += [pattern, pattern, pattern]

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, rank, vessel_type, contract_months, salary_usd,"
            " requirements, screening_questions, is_active, created_at"
            " FROM vacancies" + clause + " ORDER BY is_active DESC, id",
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def update_vacancy(conn, vacancy_id: int, rank, vessel_type, contract_months,
                   salary_usd, requirements, screening_questions) -> bool:
    """Изменить вакансию. False, если такой нет.

    Удаления в проекте нет намеренно: вакансию закрывают, а не стирают —
    на неё ссылаются заявки, и терять их вместе с вакансией нельзя.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE vacancies SET rank = %s, vessel_type = %s,"
            " contract_months = %s, salary_usd = %s, requirements = %s,"
            " screening_questions = %s WHERE id = %s RETURNING id",
            (rank, vessel_type, contract_months, salary_usd, requirements,
             json.dumps(list(screening_questions), ensure_ascii=False),
             vacancy_id),
        )
        return cur.fetchone() is not None


def set_vacancy_active(conn, vacancy_id: int, active: bool) -> bool:
    """Закрыть или снова открыть вакансию. False, если такой нет."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE vacancies SET is_active = %s WHERE id = %s RETURNING id",
            (bool(active), vacancy_id),
        )
        return cur.fetchone() is not None


def list_open_ranks(conn) -> list:
    """Должности, которые встречаются в открытых вакансиях."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT rank FROM vacancies WHERE is_active ORDER BY rank"
        )
        return [row[0] for row in cur.fetchall()]


def set_channel_message(conn, vacancy_id: int, message_id) -> None:
    """Запомнить номер объявления в канале."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE vacancies SET channel_message_id = %s WHERE id = %s",
            (int(message_id) if message_id else None, vacancy_id),
        )


def list_rank_counts(conn) -> list:
    """Должности открытых вакансий и сколько их по каждой.

    Кандидат не набирает должность руками: он открывает список и видит
    сразу, где сколько мест.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rank, COUNT(*) FROM vacancies WHERE is_active"
            " GROUP BY rank ORDER BY rank"
        )
        return [{"rank": row[0], "count": row[1]} for row in cur.fetchall()]


def list_open_vessel_types(conn, rank: str) -> list:
    """Типы судов открытых вакансий — только для выбранной должности."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT vessel_type FROM vacancies"
            " WHERE is_active AND rank = %s ORDER BY vessel_type",
            (rank,),
        )
        return [row[0] for row in cur.fetchall()]

def get_vacancy(conn, vacancy_id: int):
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, rank, vessel_type, contract_months, salary_usd,"
            " requirements, screening_questions, is_active,"
            " channel_message_id FROM vacancies WHERE id = %s",
            (vacancy_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def open_slots(conn, day: date, start_hhmm: str, end_hhmm: str, step_min: int = 30) -> int:
    """Открыть слоты интервалом. Возвращает число созданных слотов.

    Время наивное и трактуется Postgres как UTC — рекрутер задаёт слоты
    в UTC, кандидат видит их с явной пометкой UTC.

    Некорректный шаг — ValueError с текстом для рекрутера, а не зависание:
    при шаге 0 цикл ниже никогда бы не закончился.
    """
    step_min = int(step_min)
    if step_min <= 0:
        raise ValueError("Шаг должен быть больше нуля минут.")
    try:
        start = datetime.combine(day, datetime.strptime(start_hhmm, "%H:%M").time())
        end = datetime.combine(day, datetime.strptime(end_hhmm, "%H:%M").time())
    except ValueError:
        raise ValueError("Время в формате ЧЧ:ММ, например 10:00.") from None

    moments = []
    current = start
    while current < end:
        moments.append(current)
        current += timedelta(minutes=step_min)
        if len(moments) > MAX_SLOTS_PER_CALL:
            raise ValueError(
                f"За один раз можно открыть не больше {MAX_SLOTS_PER_CALL} слотов — "
                "увеличьте шаг или сократите интервал."
            )

    with conn, conn.cursor() as cur:
        created = 0
        for moment in moments:
            # Слот на это время мог быть открыт раньше: в календаре по дню
            # кликают повторно, и дубли слотов там появлялись бы сами собой.
            cur.execute(
                "INSERT INTO slots (starts_at, duration_min)"
                " SELECT %s, %s WHERE NOT EXISTS ("
                "   SELECT 1 FROM slots WHERE starts_at = %s)",
                (moment, step_min, moment),
            )
            created += cur.rowcount
    return created


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


def list_slot_days(conn, first_day: date, last_day: date) -> dict:
    """Сколько слотов открыто и занято в каждый день промежутка.

    Календарю нужно знать про день целиком, а не про каждый слот:
    зелёным он красит день, у которого слоты есть.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT starts_at::date AS day,"
            " COUNT(*) FILTER (WHERE status = 'open') AS free,"
            " COUNT(*) FILTER (WHERE status = 'booked') AS booked"
            " FROM slots WHERE starts_at::date BETWEEN %s AND %s"
            " GROUP BY day",
            (first_day, last_day),
        )
        return {row[0]: {"free": row[1], "booked": row[2]} for row in cur.fetchall()}


def close_free_day(conn, day: date) -> tuple:
    """Убрать свободные слоты дня. Возвращает (убрано, осталось занятых).

    Занятые слоты не трогаем никогда: за каждым стоит заявка живого
    человека, которому уже назвали время. Поэтому день с бронями
    закрывается только частично, и рекрутер об этом узнаёт из ответа.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM slots WHERE starts_at::date = %s AND status = 'open'",
            (day,),
        )
        removed = cur.rowcount
        cur.execute(
            "SELECT COUNT(*) FROM slots"
            " WHERE starts_at::date = %s AND status <> 'open'",
            (day,),
        )
        return removed, cur.fetchone()[0]


def list_open_days(conn, limit: int = 14) -> list:
    """Ближайшие дни, где есть свободное время для интервью.

    Кандидату показывают дни, а не полсотни получасовых слотов: сначала
    он смотрит, попадает ли вообще в даты, и только потом выбирает час.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT starts_at::date AS day, COUNT(*) AS free FROM slots"
            " WHERE status = 'open' AND starts_at > NOW()"
            " GROUP BY day ORDER BY day LIMIT %s",
            (limit,),
        )
        return [{"day": row[0], "free": row[1]} for row in cur.fetchall()]


def list_day_slots(conn, day: date) -> dict:
    """Что открыто в этот день: время приёма → состояние слота.

    Рекрутер отмечает часы по одному, поэтому календарю нужно знать не
    число слотов, а какие именно часы заняты и какие свободны.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_char(starts_at, 'HH24:MI') AS at, status FROM slots"
            " WHERE starts_at::date = %s ORDER BY starts_at",
            (day,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def open_slot(conn, day: date, hhmm: str, step_min: int = 30) -> bool:
    """Открыть один приём. False, если такой слот уже есть.

    Повторное нажатие по времени не должно плодить одинаковые слоты:
    по часам в календаре кликают туда-сюда.
    """
    try:
        moment = datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())
    except ValueError:
        raise ValueError("Время в формате ЧЧ:ММ, например 08:00.") from None

    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slots (starts_at, duration_min)"
            " SELECT %s, %s WHERE NOT EXISTS ("
            "   SELECT 1 FROM slots WHERE starts_at = %s)"
            " RETURNING id",
            (moment, int(step_min), moment),
        )
        return cur.fetchone() is not None


def close_slot(conn, day: date, hhmm: str) -> bool:
    """Снять свободный приём. False, если он занят или его нет.

    Занятый слот не убираем никогда: за ним заявка живого человека,
    которому уже назвали время.
    """
    try:
        moment = datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())
    except ValueError:
        raise ValueError("Время в формате ЧЧ:ММ, например 08:00.") from None

    with conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM slots WHERE starts_at = %s AND status = 'open'"
            " RETURNING id",
            (moment,),
        )
        return cur.fetchone() is not None


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


def release_slot(conn, slot_id: int) -> bool:
    """Вернуть слот в свободные. Нужна, если бронь прошла, а заявка не сохранилась.

    Возвращает False, если слот уже был свободен — значит освобождать нечего.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE slots SET status = 'open' WHERE id = %s AND status = 'booked' RETURNING id",
            (slot_id,),
        )
        return cur.fetchone() is not None


def normalize_contact(contact: str) -> str:
    """Привести контакт к каноническому виду: без краёв, в нижнем регистре.

    Нормализация живёт в db, а не в вызывающем коде: правило «одно
    активное интервью на моряка» держится на том, что Ivan@Example.com
    и ivan@example.com — это одна строка в candidates, и забыть про это
    в одном из вызовов быть не должно.
    """
    return str(contact or "").strip().lower()


def upsert_candidate(conn, full_name: str, contact: str, citizenship: str) -> int:
    """Найти моряка по контакту или завести. Один человек — одна строка."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO candidates (full_name, contact, citizenship) VALUES (%s, %s, %s)"
            " ON CONFLICT (contact) DO UPDATE SET full_name = EXCLUDED.full_name,"
            " citizenship = EXCLUDED.citizenship RETURNING id",
            (full_name, normalize_contact(contact), citizenship),
        )
        return cur.fetchone()[0]


def find_active_application(conn, contact: str):
    """Активная заявка моряка, если есть.

    Активная — статус 'new' и слот ещё в будущем. Заявки с прошедшим
    интервью переводятся в 'done' здесь же, чтобы рекрутеру не
    приходилось чистить статусы руками.

    Контакт нормализуется здесь же и сравнивается через lower(): моряк,
    написавший почту в другом регистре, узнаётся как тот же человек.
    """
    contact = normalize_contact(contact)
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE applications a SET status = 'done' FROM slots s, candidates c"
            " WHERE a.slot_id = s.id AND a.candidate_id = c.id"
            " AND lower(c.contact) = %s AND a.status = 'new' AND s.starts_at <= NOW()",
            (contact,),
        )
        cur.execute(
            "SELECT a.id, s.starts_at FROM applications a"
            " JOIN slots s ON s.id = a.slot_id"
            " JOIN candidates c ON c.id = a.candidate_id"
            " WHERE lower(c.contact) = %s AND a.status = 'new' AND s.starts_at > NOW()",
            (contact,),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def create_application(conn, candidate_id: int, vacancy_id: int, slot_id: int,
                       profile: dict, screening: dict, notes: str = "") -> int:
    """Сохранить заявку. Вердикт дописывается позже отдельным запросом:
    отказ модели на последнем шаге не должен стоить кандидату брони.

    Код для ссылки в Telegram рождается здесь же, одной операцией с
    заявкой: если заявка есть — код у неё есть всегда.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applications (candidate_id, vacancy_id, slot_id,"
            " rank_experience_months, total_experience_months, vessel_types,"
            " readiness_date, screening, notes, telegram_token)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " RETURNING id",
            (
                candidate_id, vacancy_id, slot_id,
                _as_int(profile.get("rank_experience_months")),
                _as_int(profile.get("total_experience_months")),
                profile.get("vessel_types"),
                profile.get("readiness_date"),
                json.dumps(screening, ensure_ascii=False),
                notes,
                secrets.token_urlsafe(16),
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


def application_token(conn, application_id: int):
    """Код заявки для ссылки в Telegram. None, если заявки нет."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_token FROM applications WHERE id = %s",
            (application_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def find_application_by_token(conn, token: str):
    """Заявка по коду из ссылки. None, если код неизвестен.

    Отдаёт ровно то, что нужно для карточки кандидату. Вердикта модели
    здесь намеренно нет: это внутренняя заметка рекрутера.
    """
    if not token:
        return None
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT a.id, a.telegram_chat_id, a.readiness_date,"
            " c.full_name, c.contact, v.rank, v.vessel_type, s.starts_at"
            " FROM applications a"
            " JOIN candidates c ON c.id = a.candidate_id"
            " JOIN vacancies v ON v.id = a.vacancy_id"
            " JOIN slots s ON s.id = a.slot_id"
            " WHERE a.telegram_token = %s",
            (token,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def bind_telegram_chat(conn, application_id: int, chat_id: int) -> bool:
    """Привязать чат к заявке. False означает, что чат уже был привязан.

    Условие telegram_chat_id IS NULL прямо в UPDATE: первый, кто открыл
    ссылку, становится её владельцем, и подменить его нельзя.
    """
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE applications SET telegram_chat_id = %s"
            " WHERE id = %s AND telegram_chat_id IS NULL RETURNING id",
            (chat_id, application_id),
        )
        return cur.fetchone() is not None


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
