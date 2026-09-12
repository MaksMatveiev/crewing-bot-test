"""Крюинг-бот: вкладка кандидата и вкладка рекрутера.

Запуск локально:
  python crewing_bot/app.py
"""

import hmac
import logging
import os
import re
import sys
import time
from calendar import monthrange
from datetime import date
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crewing_bot import brain, db, funnel, telegram

load_dotenv()

logger = logging.getLogger(__name__)

# Вывод логов включаем явно: uvicorn настраивает только свои логгеры, а без
# обработчика на корневом наши записи о сбоях никуда не попадут — и разбирать
# жалобу «бот не отвечает» будет нечем.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

KNOWLEDGE = (Path(__file__).parent / "knowledge.md").read_text(encoding="utf-8")
STATIC_DIR = Path(__file__).parent / "static"


def to_plain_text(content) -> str:
    """Gradio 6 отдаёт content списком блоков, а модель ждёт строку."""
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        )
    return content


# Значок подбирается по части названия: рекрутер пишет «2nd Engineer»,
# «Chief Engineer», «Single Engineer» — все они про машинную команду.
RANK_ICONS = (
    ("engineer", "⚙️"),
    ("motorman", "⚙️"),
    ("fitter", "⚙️"),
    ("oiler", "⚙️"),
    ("eto", "💡"),
    ("electric", "💡"),
    ("master", "🧭"),
    ("captain", "🧭"),
    ("officer", "🧭"),
    ("mate", "🧭"),
    ("cook", "🧑‍🍳"),
    ("steward", "🍽️"),
    ("messman", "🍽️"),
    ("bosun", "⚓"),
    ("ab", "⚓"),
    ("os", "⚓"),
    ("seaman", "⚓"),
)

VESSEL_ICONS = (
    ("chemical", "🧪"),
    ("lng", "🔥"),
    ("lpg", "🔥"),
    ("gas", "🔥"),
    ("tanker", "🛢️"),
    ("container", "📦"),
    ("reefer", "❄️"),
    ("cruise", "🛳️"),
    ("passenger", "🛳️"),
    ("yacht", "⛵"),
    ("offshore", "🛠️"),
    ("bulk", "🚢"),
    ("cargo", "🚢"),
)


def _pick_icon(value, table, default):
    """Найти значок по части названия. Порядок в таблице важен.

    «chemical tanker» должен получить колбу, а не бочку, поэтому более
    частные слова стоят в таблице раньше общих.
    """
    if not isinstance(value, str):
        return default
    text = value.casefold().strip()
    if not text:
        return default
    words = set(text.replace("-", " ").split())
    for needle, icon in table:
        if needle in words or needle in text:
            return icon
    return default


def rank_icon(rank) -> str:
    """Значок должности. Незнакомая должность получает якорь."""
    return _pick_icon(rank, RANK_ICONS, "⚓")


def vessel_icon(vessel_type) -> str:
    """Значок типа судна. Незнакомый тип получает судно."""
    return _pick_icon(vessel_type, VESSEL_ICONS, "🚢")


def render_vacancies(vacancies: list) -> str:
    return "\n".join(
        f"{number}. {rank_icon(v['rank'])} {v['rank']} — "
        f"{vessel_icon(v['vessel_type'])} {v['vessel_type']}, "
        f"{v['contract_months']} мес, ${v['salary_usd']}/мес"
        for number, v in enumerate(vacancies, start=1)
    )


def render_slots(slots: list) -> str:
    """Список слотов для кандидата. Время подписано UTC: офис и моряк
    сидят в разных поясах, а в базе лежит UTC."""
    return "\n".join(
        f"{number}. {slot['starts_at'].strftime('%d.%m %H:%M')} UTC"
        for number, slot in enumerate(slots, start=1)
    )


def _vacancy_text(vacancy) -> str:
    if not vacancy:
        return ""
    return (
        f"{vacancy['rank']} на {vacancy['vessel_type']}, контракт "
        f"{vacancy['contract_months']} мес, ставка ${vacancy['salary_usd']}. "
        f"Требования: {vacancy['requirements']}"
    )


# Номером считается только сообщение, которое целиком состоит из номера:
# «2», «2.», «2)». Всё остальное — не номер. Иначе «а можно 2 марта?»
# молча бронирует второй слот в списке.
_ONLY_NUMBER = re.compile(r"^\s*(\d{1,3})\s*[.)]?\s*$")

MODEL_DOWN_TEXT = (
    "⚠️ Не могу сейчас ответить — помощник временно недоступен. "
    "Повторите, пожалуйста, сообщение через минуту."
)


def _parse_number(message: str):
    """Номер из сообщения или None, если сообщение — не только номер."""
    match = _ONLY_NUMBER.match(str(message))
    return int(match.group(1)) if match else None


def _model_down(resume: str) -> str:
    """Извинение и повтор текущего вопроса: воронка стоит на месте."""
    return f"{MODEL_DOWN_TEXT}\n\n{resume}" if resume else MODEL_DOWN_TEXT


DB_SETUP_FAILED_TEXT = (
    "⚠️ База данных сейчас недоступна — попробуйте, пожалуйста, позже."
)


def _ensure_schema(conn) -> bool:
    """Подготовить схему. False — не получилось, звать базу дальше нельзя.

    Инициализация не должна ронять интерфейс: упавшая миграция или
    недоступная база — повод показать кандидату понятный текст, а не
    ошибку Gradio. Сама db.init_schema за процесс отрабатывает один раз.
    """
    try:
        db.init_schema(conn)
        return True
    except Exception:
        return False


def _open_conn():
    """Соединение с готовой схемой для вкладки рекрутера.

    Возвращает пару (соединение, текст ошибки): ровно одно из двух заполнено.
    """
    try:
        conn = db.connect()
    except RuntimeError as error:
        return None, f"⚠️ {error}"
    if not _ensure_schema(conn):
        conn.close()
        return None, DB_SETUP_FAILED_TEXT
    return conn, None


GREETING_TEXT = (
    "Здравствуйте! Я помощник крюингового агентства. "
    "Подберу вакансию и запишу на интервью.\n\n"
    "На какую должность смотрите? Ответьте номером или названием:"
)


def render_options(options, icon=None) -> str:
    """Нумерованный список вариантов — должностей или типов судов.

    icon — функция, подбирающая значок по названию. Без неё список
    остаётся текстовым: так удобнее в тестах и в местах, где значок
    не нужен.
    """
    return "\n".join(
        f"{number}. {icon(value) + chr(32) if icon else str()}{value}"
        for number, value in enumerate(options, start=1))


def opening_message():
    """Первое сообщение бота — показывается при открытии страницы.

    Раньше кандидат писал «привет» впустую: бот отвечал списком вакансий,
    а само сообщение пропадало. Теперь разговор начинает бот и сразу
    спрашивает должность.

    Отказ базы здесь не должен оставлять человека перед пустым экраном,
    поэтому любая ошибка превращается в понятный текст.
    """
    empty = vars(funnel.State())
    try:
        conn = db.connect()
    except RuntimeError as error:
        return f"⚠️ {error}", empty

    try:
        if not _ensure_schema(conn):
            return DB_SETUP_FAILED_TEXT, empty
        ranks = db.list_open_ranks(conn)
    except Exception:
        logger.exception("Не удалось собрать приветствие")
        return DB_SETUP_FAILED_TEXT, empty
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not ranks:
        return "Сейчас открытых вакансий нет. Загляните позже.", empty

    state = funnel.offer_ranks(funnel.State(), ranks)
    return GREETING_TEXT + "\n\n" + render_options(ranks, rank_icon), vars(state)


def handle(message: str, state_dict: dict):
    """Один ход разговора: возвращает ответ бота и новое состояние."""
    state = funnel.State(**state_dict) if state_dict else funnel.State()
    try:
        conn = db.connect()
    except RuntimeError as error:
        return f"⚠️ {error}", vars(state)
    try:
        if not _ensure_schema(conn):
            return DB_SETUP_FAILED_TEXT, vars(state)
        return _handle_with_db(conn, message, state)
    except Exception:
        # Обрыв соединения, нарушение уникального индекса и т.п. — отказ
        # инфраструктуры не должен ронять чат. Состояние воронки не меняем,
        # чтобы кандидат мог повторить попытку с того же места.
        return (
            "⚠️ Сейчас не получилось обработать сообщение — попробуйте, "
            "пожалуйста, ещё раз."
        ), vars(state)
    finally:
        conn.close()


def _handle_with_db(conn, message: str, state):
    # Список сужается выбранными должностью и типом флота, если они уже названы.
    vacancies = db.list_active_vacancies(conn, state.wanted_rank or None,
                                         state.wanted_vessel_type or None)

    if state.step == funnel.BLOCKED:
        return state.blocked_reason, vars(state)

    if state.step == funnel.CONFIRMED:
        return "Вы уже записаны. Менеджер свяжется с вами перед интервью.", vars(state)

    # Приветствие обычно показывает opening_message при открытии страницы.
    # Эта ветка — на случай, когда состояние пустое: перезагрузка вкладки,
    # старая ссылка, вызов через API.
    if state.step == funnel.GREETING:
        text, fresh = opening_message()
        return text, fresh

    vacancy = db.get_vacancy(conn, state.vacancy_id) if state.vacancy_id else None
    question, resume, resumed = _current_question(conn, state, vacancies)

    # Router без модели: встречный вопрос виден по знаку вопроса и
    # вопросительному слову. Раньше это решала модель — отдельный запрос
    # на каждое сообщение кандидата, то есть половина времени ответа и
    # половина суточной квоты впустую. Сообщение, целиком являющееся
    # номером, — заведомо выбор из списка, его не разбираем вовсе.
    if question and _parse_number(message) is None and brain.looks_like_question(message):
        reply = brain.answer(message, KNOWLEDGE, _vacancy_text(vacancy))
        if brain.is_unavailable(reply):
            # Модель молчит — воронку не двигаем, вопрос повторяем.
            return _model_down(resume), vars(resumed)
        return f"{reply}\n\n{resume}", vars(resumed)

    if state.step == funnel.CHOOSING_RANK:
        ranks = db.list_open_ranks(conn)
        if not ranks:
            return "Сейчас открытых вакансий нет. Загляните позже.", vars(state)
        state = funnel.offer_ranks(state, ranks)
        index = funnel.match_option(message, ranks)
        if index is None:
            return ("Не понял должность. Ответьте номером или названием:\n\n"
                    + render_options(ranks, rank_icon)), vars(state)

        state = funnel.select_rank(state, index)
        types = db.list_open_vessel_types(conn, state.wanted_rank)
        state = funnel.offer_vessel_types(state, types)
        return (f"Отлично, {state.wanted_rank}.\n\n"
                "На каком флоте хотите работать? Ответьте номером или названием:\n\n"
                + render_options(types, vessel_icon)), vars(state)

    if state.step == funnel.CHOOSING_VESSEL_TYPE:
        types = db.list_open_vessel_types(conn, state.wanted_rank)
        if not types:
            # Вакансии этой должности закрыли, пока человек думал.
            ranks = db.list_open_ranks(conn)
            state = funnel.offer_ranks(funnel.State(), ranks)
            return ("По этой должности вакансий не осталось. "
                    "Выберите другую:\n\n" + render_options(ranks, rank_icon)), vars(state)

        state = funnel.offer_vessel_types(state, types)
        index = funnel.match_option(message, types)
        if index is None:
            return ("Не понял тип флота. Ответьте номером или названием:\n\n"
                    + render_options(types, vessel_icon)), vars(state)

        state = funnel.select_vessel_type(state, index)
        matching = db.list_active_vacancies(conn, state.wanted_rank,
                                            state.wanted_vessel_type)
        if not matching:
            # Сюда почти не попасть: типы взяты из тех же вакансий. Но если
            # вакансию закрыли между двумя запросами — не заводим в тупик.
            state = funnel.offer_vessel_types(state, types)
            return ("По такому сочетанию вакансий нет. "
                    "Выберите другой тип флота:\n\n" + render_options(types, vessel_icon)), vars(state)

        return (f"Вот что есть: {state.wanted_rank} на "
                f"{state.wanted_vessel_type}. Ответьте номером:\n\n"
                + render_vacancies(matching)), vars(state)

    if state.step == funnel.CHOOSING_VACANCY:
        number = _parse_number(message)
        if number is None or not 1 <= number <= len(vacancies):
            return ("Не понял номер. Ответьте номером из списка:\n\n"
                    + render_vacancies(vacancies)), vars(state)
        chosen = vacancies[number - 1]
        state = funnel.select_vacancy(state, chosen["id"], chosen["screening_questions"])
        return (f"Отлично, {chosen['rank']} на {chosen['vessel_type']}.\n\n"
                + funnel.next_question(state)), vars(state)

    if state.step == funnel.COLLECTING_PROFILE:
        return _handle_profile(conn, message, state)

    if state.step == funnel.SCREENING:
        state = funnel.record_screening_answer(state, message)
        following = funnel.next_question(state)
        if following:
            return following, vars(state)
        # Скрининг закончен — воронка уже на шаге выбора слота (см.
        # record_screening_answer). Слоты запрашиваем той же функцией,
        # что и на любом следующем ходу: список слотов ещё не показан
        # (state.slot_ids пуст), поэтому текст последнего ответа на
        # скрининг здесь не используется как номер слота.
        return _handle_slot_choice(conn, message, state, vacancy)

    if state.step == funnel.CHOOSING_SLOT:
        return _handle_slot_choice(conn, message, state, vacancy)

    return "Не понял. Напишите ещё раз, пожалуйста.", vars(state)


def _current_question(conn, state, vacancies):
    """Что бот спрашивает прямо сейчас — для роутера и для возврата к воронке.

    funnel.next_question намеренно молчит про выбор вакансии и слота:
    эти списки рисует app.py из данных базы. Поэтому текст «текущего
    вопроса» для этих двух шагов собирается здесь, иначе встречный
    вопрос на них вообще не распознавался бы.

    Третьим значением возвращается состояние, соответствующее показанному
    списку: свободные слоты берутся из базы заново, и запомненные id
    должны совпадать с номерами, которые увидит кандидат.
    """
    if state.step == funnel.CHOOSING_RANK and state.rank_options:
        listing = ("Вернёмся к выбору должности — ответьте номером:\n\n"
                   + render_options(state.rank_options, rank_icon))
        return listing, listing, state

    if state.step == funnel.CHOOSING_VESSEL_TYPE and state.vessel_options:
        listing = ("Вернёмся к выбору типа флота — ответьте номером:\n\n"
                   + render_options(state.vessel_options, vessel_icon))
        return listing, listing, state

    if state.step == funnel.CHOOSING_VACANCY:
        listing = ("Вернёмся к выбору вакансии — ответьте номером:\n\n"
                   + render_vacancies(vacancies))
        return listing, listing, state

    if state.step == funnel.CHOOSING_SLOT and state.slot_ids:
        slots = db.list_open_slots(conn)
        if not slots:
            return None, "", state
        listing = ("Вернёмся к выбору времени — ответьте номером:\n\n"
                   + render_slots(slots))
        return listing, listing, funnel.offer_slots(state, [s["id"] for s in slots])

    question = funnel.next_question(state)
    resume = f"Вернёмся к анкете. {question}" if question else ""
    return question, resume, state


def _handle_profile(conn, message: str, state):
    pending = [name for name, _ in funnel.PROFILE_FIELDS if name not in state.profile]
    parsed = brain.extract(message, pending)
    if brain.is_unavailable(parsed):
        # Модель недоступна — сырой текст в поле анкеты не пишем и шаг не
        # двигаем, иначе за время сбоя воронка соберёт мусор и займёт слот.
        return _model_down(funnel.next_question(state) or ""), vars(state)
    before = dict(state.profile)
    state = funnel.record_profile(state, parsed)

    # Модель ничего не распознала — записываем ответ в текущее поле как есть
    # и сохраняем сырой текст рекрутеру, чтобы разговор не зациклился
    # на одном вопросе, а исходная формулировка не потерялась.
    if state.profile == before and pending:
        state = funnel.record_profile(state, {pending[0]: message.strip()})
        state = funnel.add_note(state, f"{pending[0]}: {message.strip()}")

    # Контакт стал известен — проверяем, не записан ли моряк уже
    contact = state.profile.get("contact")
    if contact and "contact" not in before:
        active = db.find_active_application(conn, contact)
        if active:
            when = active["starts_at"].strftime("%d.%m в %H:%M UTC")
            state = funnel.block(
                state,
                f"Вы уже записаны на интервью {when}. Второе интервью не нужно — "
                "если требуется перенос, напишите менеджеру.",
            )
            return state.blocked_reason, vars(state)

    if funnel.profile_complete(state):
        state = funnel.start_screening(state)
        return ("Спасибо! Теперь несколько вопросов по вакансии.\n\n"
                + funnel.next_question(state)), vars(state)
    return funnel.next_question(state), vars(state)


def _handle_slot_choice(conn, message: str, state, vacancy):
    """Показ и выбор слота интервью.

    Свободные слоты запрашиваются у базы заново на каждый ход: рекрутер
    открывает время на своей вкладке в любой момент, поэтому отсутствие
    слотов — состояние временное, а не терминальное. Воронка остаётся на
    шаге CHOOSING_SLOT (funnel.block здесь не используется — блокировка
    зарезервирована для случая, когда моряк уже записан на интервью), и
    следующее сообщение кандидата снова опросит базу.
    """
    slots = db.list_open_slots(conn)
    if not slots:
        state = funnel.offer_slots(state, [])
        return ("Свободных слотов сейчас нет — менеджер свяжется с вами "
                "и предложит время."), vars(state)

    if not state.slot_ids:
        # Список ещё не показывали (только что закончился скрининг или
        # в прошлый раз слотов не было) — не пытаемся угадать номер в
        # этом сообщении, а показываем актуальный список.
        state = funnel.offer_slots(state, [slot["id"] for slot in slots])
        return ("Выберите время интервью — ответьте номером (время UTC):\n\n"
                + render_slots(slots)), vars(state)

    number = _parse_number(message)
    try:
        if number is None:
            raise ValueError("нет номера")
        chosen = funnel.select_slot(state, number)
    except ValueError:
        state = funnel.offer_slots(state, [slot["id"] for slot in slots])
        return ("Такого номера нет. Вот свободное время:\n\n"
                + render_slots(slots)), vars(state)

    if not db.book_slot(conn, chosen.slot_id):
        state = funnel.offer_slots(state, [slot["id"] for slot in slots])
        return ("Этот слот только что заняли. Выберите другой:\n\n"
                + render_slots(slots)), vars(state)

    # Зона 1 — заявка. Пока её нет, бронь ничем не подкреплена: если здесь
    # сорвалось, слот надо вернуть в свободные, иначе рекрутер видит
    # занятое время без брони, а кандидат остаётся ни с чем.
    try:
        candidate_id = db.upsert_candidate(
            conn,
            chosen.profile.get("full_name", ""),
            chosen.profile.get("contact", ""),
            chosen.profile.get("citizenship", ""),
        )
        application_id = db.create_application(
            conn, candidate_id, chosen.vacancy_id, chosen.slot_id,
            chosen.profile, chosen.screening, chosen.notes,
        )
    except Exception:
        db.release_slot(conn, chosen.slot_id)
        state = funnel.offer_slots(state, [slot["id"] for slot in slots])
        return ("Не удалось записать вас на интервью — попробуйте, пожалуйста, "
                "ещё раз. Вот свободное время:\n\n" + render_slots(slots)), vars(state)

    # Зона 2 — вердикт. Заявка уже закоммичена, бронь состоялась. Вердикт
    # это всего лишь заметка рекрутеру, поэтому сбой здесь НЕ освобождает
    # слот и не отменяет запись: иначе кандидату сказали бы «не получилось»
    # при живой заявке, а повторную попытку отбил бы индекс
    # one_active_application — человек остался бы заперт навсегда.
    try:
        verdict_text = brain.verdict(vacancy or {}, chosen.profile, chosen.screening)
        if not brain.is_unavailable(verdict_text):
            db.set_verdict(conn, application_id, verdict_text)
    except Exception:
        # Молчим намеренно: рекрутер увидит заявку с пустым вердиктом,
        # это лучше, чем потерянная бронь.
        pass

    when = next((slot["starts_at"] for slot in slots if slot["id"] == chosen.slot_id), None)
    when_text = when.strftime("%d.%m в %H:%M UTC") if when else "выбранное время"
    confirmation = (f"✅ Записал вас на интервью {when_text}. "
                    "Менеджер свяжется с вами по указанному контакту.")

    # Ссылка на Telegram — необязательное дополнение. Не настроен бот или
    # код почему-то не достался — просто подтверждаем бронь без ссылки.
    try:
        link = telegram.deep_link(db.application_token(conn, application_id))
    except Exception:
        link = None
    if link:
        confirmation += (
            "\n\nХотите получить эту заявку в Telegram? "
            f"Откройте ссылку и нажмите «Начать»:\n{link}"
        )

    chosen = funnel.confirm(chosen)
    return confirmation, vars(chosen)


def candidate_chat(message, history, state_dict):
    return handle(to_plain_text(message), state_dict)


DEFAULT_SCREENING_QUESTIONS = [
    "Сколько месяцев опыта в этой должности на таком типе судна?",
    "Какие STCW-сертификаты действующие и до какой даты?",
    "Есть ли действующий паспорт моряка (SID)?",
    "Есть ли виза US C1/D и шенген?",
    "Уровень английского — Marlins или CES, сколько процентов?",
    "До какой даты действует медкомиссия?",
]


# Простое ограничение подбора пароля в памяти процесса: адрес приложения
# публичный, а вкладка рекрутера отдаёт ФИО, контакты и гражданство всех
# кандидатов. Ни пароль, ни попытки не логируются.
MAX_PASSWORD_ATTEMPTS = 5
LOCKOUT_SECONDS = 60
_password_attempts = {"failures": 0, "locked_until": 0.0}


def check_password(entered: str) -> bool:
    """Сравнение пароля постоянным по времени способом.

    Сравниваем байты, а не строки: compare_digest отказывается работать
    со строками с не-ASCII символами, а пароль может быть каким угодно.
    """
    expected = os.getenv("RECRUITER_PASSWORD")
    if not expected:
        return False
    return hmac.compare_digest(
        str(entered or "").encode("utf-8"), str(expected).encode("utf-8")
    )


def _lockout_left() -> int:
    """Сколько секунд осталось ждать после серии неудач. 0 — можно пробовать."""
    left = _password_attempts["locked_until"] - time.monotonic()
    return int(left) + 1 if left > 0 else 0


def _guard(password: str):
    """Текст ошибки, если доступа нет, иначе None."""
    if not os.getenv("RECRUITER_PASSWORD"):
        return "⚠️ Не задан RECRUITER_PASSWORD в .env — вкладка закрыта."

    waiting = _lockout_left()
    if waiting:
        # Пароль даже не проверяем, пока идёт пауза.
        return f"⚠️ Слишком много неудачных попыток. Подождите {waiting} с."

    if not check_password(password):
        _password_attempts["failures"] += 1
        if _password_attempts["failures"] >= MAX_PASSWORD_ATTEMPTS:
            _password_attempts["failures"] = 0
            _password_attempts["locked_until"] = time.monotonic() + LOCKOUT_SECONDS
            return (f"⚠️ Слишком много неудачных попыток. "
                    f"Подождите {LOCKOUT_SECONDS} с.")
        return "⚠️ Неверный пароль."

    _password_attempts["failures"] = 0
    return None


def recruiter_login(entered: str):
    """Проверить пароль на входе во вкладку рекрутера.

    Возвращает (пароль_для_сессии, открыт_ли_доступ, сообщение_об_ошибке).

    Пароль запоминается в состоянии вкладки и дальше подставляется в
    каждый вызов: скрытие блоков — только внешний вид, и проверку на
    сервере обходить нельзя.
    """
    error = _guard(entered)
    if error:
        return "", False, error
    return entered, True, ""


NEW_VACANCY_LABEL = "— новая вакансия —"

STATUS_FILTERS = ("все", "открытые", "закрытые")


# Фотографии судов лежат рядом с приложением и отдаются как /static.
# Снимки с Pexels: лицензия разрешает свободное использование.
VESSEL_PHOTOS = (
    ("lng", "lng-tanker.jpg"),
    ("chemical", "chemical-tanker.jpg"),
    ("tanker", "tanker.jpg"),
    ("bulk", "bulk-carrier.jpg"),
    ("container", "container.jpg"),
    ("general", "general-cargo.jpg"),
    ("reefer", "reefer.jpg"),
)


def vessel_photo(vessel_type) -> str:
    """Адрес фотографии для типа судна.

    Порядок в таблице важен: «LNG tanker» и «chemical tanker» должны
    найтись раньше простого «tanker», иначе газовоз получил бы снимок
    обычного танкера.
    """
    text = str(vessel_type or "").lower()
    for key, filename in VESSEL_PHOTOS:
        if key in text:
            return f"/static/vessels/{filename}"
    return "/static/vessels/default.jpg"


def _escape(value) -> str:
    """Текст из базы попадает в разметку — экранируем его.

    Название вакансии заводит человек, и знак < в требованиях не должен
    ломать вёрстку страницы.
    """
    return (str(value if value is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def vacancy_cards_html(vacancies, *, show_status: bool = True) -> str:
    """Вакансии карточками: фотография судна, должность, ставка, срок.

    show_status=False убирает пометку «открыта/закрыта» — кандидату
    показывают только открытые, и подпись была бы лишней.
    """
    if not vacancies:
        return "<p class='cards-empty'>Ничего не найдено.</p>"

    cards = []
    for v in vacancies:
        status = ""
        if show_status:
            mark = "открыта" if v["is_active"] else "закрыта"
            state = "open" if v["is_active"] else "closed"
            status = f"<span class='card-status {state}'>{mark}</span>"
        cards.append(
            "<article class='vacancy-card'>"
            f"<img src='{vessel_photo(v['vessel_type'])}'"
            f" alt='{_escape(v['vessel_type'])}' loading='lazy'>"
            "<div class='card-body'>"
            f"<h4>{_escape(v['rank'])}</h4>"
            f"<p class='card-vessel'>{_escape(v['vessel_type'])}</p>"
            f"<p class='card-salary'>${v['salary_usd']} / мес</p>"
            f"<p class='card-meta'>контракт {v['contract_months']} мес"
            f" · №{v['id']}</p>"
            f"{status}"
            "</div></article>"
        )
    return "<div class='vacancy-cards'>" + "".join(cards) + "</div>"


def vacancy_choices(vacancies) -> list:
    """Варианты выпадающего списка: подпись и номер вакансии.

    Первый вариант — пустой: он означает «создаю новую», и тогда форма
    ни к какой вакансии не привязана.
    """
    options = [(NEW_VACANCY_LABEL, 0)]
    for v in vacancies:
        closed = "" if v["is_active"] else " · закрыта"
        options.append(
            (f"#{v['id']} {v['rank']} — {v['vessel_type']}{closed}", v["id"]))
    return options


def _status_filter(status: str):
    """Подпись фильтра → значение для db.list_all_vacancies."""
    return {"открытые": "open", "закрытые": "closed"}.get(status)


def recruiter_vacancies(password: str, search: str = "", status: str = "все"):
    """Список вакансий с поиском и фильтром по статусу.

    Возвращает пару: текст списка и обновление выпадающего списка —
    после любой правки оба должны показывать одно и то же.
    """
    error = _guard(password)
    if error:
        return error, gr.update()

    conn, error = _open_conn()
    if error:
        return error, gr.update()
    try:
        found = db.list_all_vacancies(conn, search, _status_filter(status))
    finally:
        conn.close()

    return vacancy_cards_html(found), gr.update(choices=vacancy_choices(found))


def recruiter_load_vacancy(password: str, vacancy_id):
    """Подставить выбранную вакансию в поля формы.

    Пустой выбор очищает форму — так рекрутер заводит новую вакансию,
    не рискуя случайно переписать чужую.
    """
    blank = ("", "", 6, 6500, "", "")
    error = _guard(password)
    if error or not vacancy_id:
        return blank

    conn, error = _open_conn()
    if error:
        return blank
    try:
        vacancy = db.get_vacancy(conn, int(vacancy_id))
    finally:
        conn.close()
    if not vacancy:
        return blank

    return (
        vacancy["rank"],
        vacancy["vessel_type"],
        vacancy["contract_months"],
        vacancy["salary_usd"],
        vacancy["requirements"] or "",
        "\n".join(vacancy["screening_questions"] or []),
    )


def recruiter_save_vacancy(password, vacancy_id, rank, vessel_type,
                           contract_months, salary_usd, requirements,
                           questions_text) -> str:
    """Сохранить форму: создать новую вакансию или изменить выбранную."""
    error = _guard(password)
    if error:
        return error
    if not rank or not vessel_type:
        return "⚠️ Должность и тип судна обязательны."

    try:
        months = int(contract_months)
        salary = int(salary_usd)
    except (TypeError, ValueError):
        return "⚠️ Контракт и ставка — целые числа."

    questions = [line.strip() for line in (questions_text or "").splitlines()
                 if line.strip()] or DEFAULT_SCREENING_QUESTIONS

    conn, error = _open_conn()
    if error:
        return error
    try:
        if vacancy_id:
            saved = db.update_vacancy(conn, int(vacancy_id), rank, vessel_type,
                                      months, salary, requirements, questions)
            return (f"✅ Вакансия #{int(vacancy_id)} обновлена."
                    if saved else "⚠️ Такой вакансии больше нет — обновите список.")
        new_id = db.create_vacancy(conn, rank, vessel_type, months, salary,
                                   requirements, questions)
        return f"✅ Вакансия #{new_id} создана."
    finally:
        conn.close()


def recruiter_toggle_vacancy(password, vacancy_id, open_it: bool) -> str:
    """Закрыть вакансию или открыть её снова.

    Удаления в панели нет намеренно: на вакансию ссылаются заявки, и
    стереть её — значит потерять историю кандидатов. Закрытая вакансия
    просто перестаёт показываться в чате.
    """
    error = _guard(password)
    if error:
        return error
    if not vacancy_id:
        return "⚠️ Сначала выберите вакансию в списке."

    conn, error = _open_conn()
    if error:
        return error
    try:
        changed = db.set_vacancy_active(conn, int(vacancy_id), open_it)
    finally:
        conn.close()
    if not changed:
        return "⚠️ Такой вакансии больше нет — обновите список."
    return (f"✅ Вакансия #{int(vacancy_id)} снова открыта."
            if open_it else f"✅ Вакансия #{int(vacancy_id)} закрыта — "
                            "в чате её больше не предложат.")


def recruiter_applications(password: str) -> str:
    error = _guard(password)
    if error:
        return error
    conn, error = _open_conn()
    if error:
        return error
    try:
        rows = db.list_applications(conn)
    finally:
        conn.close()
    if not rows:
        return "Заявок пока нет."
    return "\n\n".join(
        f"#{row['id']} {row['full_name']} ({row['contact']}, {row['citizenship']})\n"
        f"  {row['rank']} — {row['vessel_type']}, интервью "
        f"{row['starts_at'].strftime('%d.%m %H:%M')} UTC, статус {row['status']}\n"
        f"  опыт в должности: {row['rank_experience_months']} мес, "
        f"общий: {row['total_experience_months']} мес, "
        f"готов с {row['readiness_date']}\n"
        f"  вердикт: {row['verdict'] or '—'}"
        for row in rows
    )


def _startup_greeting():
    """Приветствие для первой отрисовки страницы.

    Собирается один раз при старте приложения. База может быть
    недоступна — тогда показываем текст без списка должностей, а список
    добавит событие загрузки.
    """
    try:
        text, _ = opening_message()
    except Exception:
        logger.exception("Не удалось собрать приветствие при старте")
        text = GREETING_TEXT
    return [{"role": "assistant", "content": text}]


# Рабочий день интервью: с 08:00 до 14:00 UTC, приёмы по полчаса.
# Рекрутер не задаёт время руками — он открывает день целиком.
INTERVIEW_START = "08:00"
INTERVIEW_END = "14:00"
INTERVIEW_STEP_MIN = 30
# Шесть недель по семь дней — в такую сетку помещается любой месяц.
CALENDAR_CELLS = 42

MONTH_NAMES = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)

WEEKDAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def month_title(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1].capitalize()} {year}"


def shift_month(year: int, month: int, step: int):
    """Соседний месяц. Декабрь и январь переносят год сами."""
    index = (year * 12 + (month - 1)) + step
    return index // 12, index % 12 + 1


def month_cells(year: int, month: int) -> list:
    """Сетка месяца: числа по местам, None — пустая клетка.

    Неделя начинается с понедельника — так календарь читают моряки и
    рекрутеры, а не с воскресенья.
    """
    first = date(year, month, 1)
    days_in_month = monthrange(year, month)[1]
    lead = first.weekday()
    cells = [None] * lead + list(range(1, days_in_month + 1))
    return (cells + [None] * CALENDAR_CELLS)[:CALENDAR_CELLS]


def _day_buttons(year, month, days_with_slots):
    """Обновления для 42 кнопок-клеток календаря.

    Открытый день — зелёный (variant primary, календарь красит его своим
    цветом). Прошедшие дни нажимать нельзя: открывать интервью задним
    числом бессмысленно.
    """
    today = date.today()
    updates = []
    for number in month_cells(year, month):
        if number is None:
            updates.append(gr.update(value="", interactive=False,
                                     variant="secondary"))
            continue
        day = date(year, month, number)
        counts = days_with_slots.get(day)
        is_open = bool(counts and (counts["free"] or counts["booked"]))
        updates.append(gr.update(
            value=str(number),
            interactive=day >= today,
            variant="primary" if is_open else "secondary",
        ))
    return updates


def _calendar_answer(year, month, days_with_slots, message=""):
    open_days = sum(1 for counts in days_with_slots.values()
                    if counts["free"] or counts["booked"])
    booked = sum(counts["booked"] for counts in days_with_slots.values())
    summary = (f"Открытых дней в этом месяце: {open_days}"
               f" · записей на интервью: {booked}")
    return [month_title(year, month), message or summary] + \
        _day_buttons(year, month, days_with_slots)


def _month_slots(conn, year, month):
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    return db.list_slot_days(conn, first, last)


def recruiter_calendar(password: str, year: int, month: int, message: str = ""):
    """Нарисовать месяц: заголовок, подпись и 42 клетки."""
    error = _guard(password)
    if error:
        return [month_title(year, month), error] + \
            _day_buttons(year, month, {})

    conn, error = _open_conn()
    if error:
        return [month_title(year, month), error] + \
            _day_buttons(year, month, {})
    try:
        days = _month_slots(conn, year, month)
    finally:
        conn.close()
    return _calendar_answer(year, month, days, message)


def recruiter_shift_month(password: str, year: int, month: int, step: int):
    """Листнуть календарь на месяц назад или вперёд."""
    year, month = shift_month(year, month, step)
    return [year, month] + recruiter_calendar(password, year, month)


def recruiter_toggle_day(password: str, year: int, month: int, number: int):
    """Открыть день для интервью или снять его.

    Открытый день — это слоты с 08:00 до 14:00 UTC по полчаса. Повторное
    нажатие убирает свободные слоты, но занятые не трогает никогда: за
    каждым стоит человек, которому уже назвали время.
    """
    error = _guard(password)
    if error:
        return recruiter_calendar(password, year, month, error)

    try:
        day = date(year, month, int(number))
    except ValueError:
        return recruiter_calendar(password, year, month)
    if day < date.today():
        return recruiter_calendar(
            password, year, month,
            "⚠️ Прошедший день открыть нельзя.")

    conn, error = _open_conn()
    if error:
        return recruiter_calendar(password, year, month, error)
    try:
        known = db.list_slot_days(conn, day, day).get(day)
        if known and (known["free"] or known["booked"]):
            removed, left = db.close_free_day(conn, day)
            if left:
                message = (f"⚠️ {day.strftime('%d.%m')}: снято свободных "
                           f"слотов {removed}, но {left} уже заняты — "
                           "день остаётся открытым для них.")
            else:
                message = f"✅ {day.strftime('%d.%m')} закрыт, слотов снято: {removed}."
        else:
            created = db.open_slots(conn, day, INTERVIEW_START, INTERVIEW_END,
                                    INTERVIEW_STEP_MIN)
            message = (f"✅ {day.strftime('%d.%m')} открыт: "
                       f"{INTERVIEW_START}–{INTERVIEW_END} UTC, слотов {created}.")
        days = _month_slots(conn, year, month)
    except ValueError as failure:
        return recruiter_calendar(password, year, month, f"⚠️ {failure}")
    finally:
        conn.close()
    return _calendar_answer(year, month, days, message)


def _day_click(position: int):
    """Обработчик одной клетки календаря.

    Клетка знает только своё место в сетке — число месяца выясняется в
    момент нажатия. Иначе при листании месяцев пришлось бы пересобирать
    все сорок две кнопки заново.
    """
    def click(password, year, month):
        number = month_cells(year, month)[position]
        if number is None:
            return recruiter_calendar(password, year, month)
        return recruiter_toggle_day(password, year, month, number)

    return click


def style_version() -> str:
    """Отпечаток файла стилей для адреса ссылки.

    Без него браузер держит старый style.css после выкладки, и рекрутер
    видит вчерашнее оформление, пока не почистит кеш вручную. Отпечаток
    меняется вместе с файлом — и ссылка вместе с ним.
    """
    try:
        stamp = (STATIC_DIR / "style.css").stat().st_mtime_ns
    except OSError:
        # Файла нет — страница просто останется без оформления.
        return "0"
    return format(stamp & 0xFFFFFFFF, "x")


def build_ui():
    with gr.Blocks(title="Крюинг-агентство «Меридиан»") as demo:
        # В Gradio 6 у Blocks нет параметра css, поэтому подключаем стили
        # ссылкой на файл, который отдаёт само приложение. Заодно правка
        # внешнего вида не требует перезапуска сборки страницы.
        gr.HTML(f'<link rel="stylesheet" href="/static/style.css?v={style_version()}">')
        with gr.Tab("Кандидат"):
            state = gr.State({})
            # Приветствие попадает в разметку сразу, при сборке страницы:
            # событие загрузки отрабатывает уже после первой отрисовки, и на
            # спящем сервисе человек успевал увидеть пустой чат.
            chatbot = gr.Chatbot(label="Chatbot", value=_startup_greeting())
            gr.ChatInterface(
                chatbot=chatbot,
                fn=candidate_chat,
                additional_inputs=[state],
                additional_outputs=[state],
                title="Запись на интервью",
                description="Подберём вакансию по должности и типу флота и запишем на интервью.",
            )

        with gr.Tab("Рекрутер"):
            # Пароль живёт в состоянии вкладки и подставляется в каждый вызов.
            # Скрытие блоков ниже — только внешний вид: обработчики на сервере
            # проверяют пароль сами, иначе защиту обошли бы запросом мимо
            # интерфейса.
            session_password = gr.State("")

            with gr.Group() as login_box:
                gr.Markdown("### Вход для рекрутера")
                password = gr.Textbox(label="Пароль", type="password")
                login_message = gr.Markdown("")
                login_button = gr.Button("Войти")

            with gr.Group(visible=False) as workspace:
                with gr.Tabs():
                    with gr.Tab("Вакансии"):
                        gr.Markdown("### Вакансии")
                        with gr.Row():
                            search = gr.Textbox(
                                label="Поиск", scale=3,
                                placeholder="должность, тип судна или слово из требований")
                            status = gr.Radio(
                                list(STATUS_FILTERS), value="все", label="Показывать",
                                scale=2)
                        refresh_button = gr.Button("Обновить список")
                        vacancies_out = gr.HTML("<p class='cards-empty'>Нажмите «Обновить список».</p>")

                        gr.Markdown("### Добавить или изменить")
                        # Выпадающий список привязывает форму к вакансии. Пустой
                        # выбор — режим «новая»: так правка и создание живут в одной
                        # форме и не расходятся.
                        picker = gr.Dropdown(
                            choices=[(NEW_VACANCY_LABEL, 0)], value=0,
                            label="Вакансия", interactive=True)
                        rank = gr.Textbox(label="Должность", placeholder="2nd Engineer")
                        vessel_type = gr.Textbox(label="Тип судна", placeholder="bulk carrier")
                        with gr.Row():
                            contract_months = gr.Number(label="Контракт, мес", value=6)
                            salary_usd = gr.Number(label="Ставка, $", value=6500)
                        requirements = gr.Textbox(label="Требования", lines=3)
                        questions_text = gr.Textbox(
                            label="Вопросы скрининга — по одному в строке. Пусто = набор по умолчанию",
                            lines=6,
                        )
                        save_button = gr.Button("Сохранить", variant="primary")
                        with gr.Row():
                            close_button = gr.Button("Закрыть вакансию")
                            reopen_button = gr.Button("Открыть снова")
                        gr.Markdown(
                            "_Удаления нет: на вакансию ссылаются заявки кандидатов. "
                            "Закрытая вакансия просто не предлагается в чате._")
                        vacancy_message = gr.Markdown("")

                        form_fields = [rank, vessel_type, contract_months, salary_usd,
                                       requirements, questions_text]
                        list_inputs = [session_password, search, status]
                        list_outputs = [vacancies_out, picker]

                        refresh_button.click(recruiter_vacancies,
                                             inputs=list_inputs, outputs=list_outputs)
                        search.submit(recruiter_vacancies,
                                      inputs=list_inputs, outputs=list_outputs)
                        status.change(recruiter_vacancies,
                                      inputs=list_inputs, outputs=list_outputs)

                        picker.change(recruiter_load_vacancy,
                                      inputs=[session_password, picker],
                                      outputs=form_fields)

                        # После сохранения и после закрытия список перечитывается:
                        # иначе рекрутер видит старое состояние и правит вслепую.
                        save_button.click(
                            recruiter_save_vacancy,
                            inputs=[session_password, picker] + form_fields,
                            outputs=vacancy_message,
                        ).then(recruiter_vacancies, inputs=list_inputs, outputs=list_outputs)

                        close_button.click(
                            lambda password, chosen: recruiter_toggle_vacancy(
                                password, chosen, False),
                            inputs=[session_password, picker], outputs=vacancy_message,
                        ).then(recruiter_vacancies, inputs=list_inputs, outputs=list_outputs)

                        reopen_button.click(
                            lambda password, chosen: recruiter_toggle_vacancy(
                                password, chosen, True),
                            inputs=[session_password, picker], outputs=vacancy_message,
                        ).then(recruiter_vacancies, inputs=list_inputs, outputs=list_outputs)

                    with gr.Tab("Календарь"):
                        gr.Markdown("### Календарь интервью")
                        gr.Markdown(
                            f"Нажмите на день — он откроется для интервью "
                            f"с **{INTERVIEW_START} до {INTERVIEW_END} UTC** "
                            f"(приёмы по {INTERVIEW_STEP_MIN} минут). Зелёный день "
                            "уже открыт; нажатие на него снимает свободные слоты, "
                            "а занятые оставляет.")

                        today = date.today()
                        calendar_year = gr.State(today.year)
                        calendar_month = gr.State(today.month)

                        with gr.Row():
                            previous_month = gr.Button("◀", scale=1)
                            month_label = gr.Markdown(
                                f"### {month_title(today.year, today.month)}")
                            next_month = gr.Button("▶", scale=1)

                        with gr.Column(elem_id="calendar-grid"):
                            with gr.Row():
                                for name in WEEKDAY_NAMES:
                                    gr.Markdown(f"**{name}**")
                            day_buttons = []
                            for week in range(CALENDAR_CELLS // 7):
                                with gr.Row():
                                    for cell in range(7):
                                        day_buttons.append(
                                            gr.Button("", interactive=False, scale=1))

                        calendar_message = gr.Markdown("")
                        calendar_outputs = [month_label, calendar_message] + day_buttons

                        # Каждая клетка знает только своё место в сетке; какое это
                        # число — решает обработчик по текущему месяцу. Иначе сетку
                        # пришлось бы пересобирать при каждом листании.
                        for position, button in enumerate(day_buttons):
                            button.click(
                                _day_click(position),
                                inputs=[session_password, calendar_year, calendar_month],
                                outputs=calendar_outputs,
                            )

                        previous_month.click(
                            lambda password, year, month: recruiter_shift_month(
                                password, year, month, -1),
                            inputs=[session_password, calendar_year, calendar_month],
                            outputs=[calendar_year, calendar_month] + calendar_outputs,
                        )
                        next_month.click(
                            lambda password, year, month: recruiter_shift_month(
                                password, year, month, 1),
                            inputs=[session_password, calendar_year, calendar_month],
                            outputs=[calendar_year, calendar_month] + calendar_outputs,
                        )
                        show_calendar = gr.Button("Обновить календарь")
                        show_calendar.click(
                            recruiter_calendar,
                            inputs=[session_password, calendar_year, calendar_month],
                            outputs=calendar_outputs,
                        )

                    with gr.Tab("Заявки"):
                        gr.Markdown("### Заявки")
                        applications_out = gr.Textbox(label="Заявки кандидатов", lines=20)
                        gr.Button("Показать заявки").click(
                            recruiter_applications, inputs=session_password, outputs=applications_out
                        )

            def _login(entered):
                stored, unlocked, message = recruiter_login(entered)
                # Поле пароля очищаем в любом случае: на общем экране
                # введённому паролю оставаться незачем.
                return (stored,
                        gr.update(visible=not unlocked),
                        gr.update(visible=unlocked),
                        message,
                        "")

            # После входа обе вкладки заполняются сами: иначе рекрутер
            # видит пустые поля и должен нажимать «Обновить» руками.
            login_button.click(
                _login,
                inputs=password,
                outputs=[session_password, login_box, workspace,
                         login_message, password],
            ).then(
                recruiter_vacancies, inputs=list_inputs, outputs=list_outputs
            ).then(
                recruiter_calendar,
                inputs=[session_password, calendar_year, calendar_month],
                outputs=calendar_outputs,
            )

        def _open_chat():
            """Приветствие и первый вопрос — сразу при открытии страницы."""
            text, fresh = opening_message()
            return [{"role": "assistant", "content": text}], fresh

        demo.load(_open_chat, outputs=[chatbot, state])

    return demo


def handle_telegram_update(update: dict, *, conn) -> str:
    """Обработать апдейт от Telegram. Возвращает слово-решение.

    Разделено с транспортом нарочно: вся логика проверяется тестами без
    сети и без веб-сервера.
    """
    parsed = telegram.parse_start(update)
    if not parsed:
        return "ignored"
    chat_id, token = parsed

    application = db.find_application_by_token(conn, token)
    if not application:
        telegram.send_message(
            chat_id,
            "Ссылка не найдена или устарела. Запишитесь на интервью заново.",
        )
        return "unknown"

    owner = application.get("telegram_chat_id")
    if owner is None:
        # bind_telegram_chat вернёт False, если между чтением owner и этим
        # UPDATE чат уже успел привязать кто-то другой (гонка при двух
        # одновременных Start с одним кодом). Тогда владелец — не мы, и
        # дальше действуем так же, как с изначально чужим чатом.
        is_foreign = not db.bind_telegram_chat(conn, application["id"], chat_id)
    else:
        is_foreign = owner != chat_id

    if is_foreign:
        # Ссылку могли переслать или заскринить. Чужому её содержимое
        # не показываем: там ФИО и контакт живого человека.
        telegram.send_message(
            chat_id,
            "Эта ссылка выдана другому кандидату. Запишитесь на интервью сами — "
            "и получите свою заявку.",
        )
        return "foreign"

    telegram.send_message(chat_id, telegram.build_card(application))
    return "sent"


def build_app():
    """Веб-приложение: чат Gradio на / и приём сообщений Telegram.

    Telegram умеет только вебхук на публичный адрес. Опрос (polling) на
    бесплатном хостинге не годится: пока сервис спит, опрашивать некому,
    и нажатие Start потерялось бы навсегда. Запрос вебхука сервис будит.
    """
    import gradio as gr
    from fastapi import FastAPI, Request, Response

    # /docs, /redoc и /openapi.json отключены: адрес публичный, и
    # незачем публиковать инвентарь маршрутов бота.
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # Фон и стили отдаём сами: Gradio монтируется на корень, и обычного
    # места для статики у него нет.
    from fastapi.staticfiles import StaticFiles
    api.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @api.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
        header = request.headers.get("x-telegram-bot-api-secret-token")
        # Сравниваем байты постоянным по времени способом — как и пароль
        # рекрутера в check_password (compare_digest не работает со
        # строками с не-ASCII символами, поэтому сначала кодируем в utf-8).
        if not secret or not hmac.compare_digest(
            str(header or "").encode("utf-8"), str(secret).encode("utf-8")
        ):
            # Адрес публичный. Без этой проверки любой мог бы слать
            # поддельные апдейты и подбирать чужие коды заявок.
            return Response(status_code=403)

        try:
            update = await request.json()
        except Exception:
            return {"ok": True}

        conn = None
        try:
            conn = db.connect()
            db.init_schema(conn)
            handle_telegram_update(update, conn=conn)
        except Exception:
            # Отвечаем 200 в любом случае: иначе Telegram будет
            # повторять доставку часами. Но след в логе оставляем: без
            # этого расследовать «карточки не приходят» будет нечем.
            # Апдейт и текст сообщений кандидата в лог не попадают —
            # только факт сбоя и трассировка исключения.
            logger.exception("Ошибка обработки апдейта Telegram-вебхука")
        finally:
            if conn is not None:
                conn.close()
        return {"ok": True}

    return gr.mount_gradio_app(api, build_ui(), path="/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        build_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 7861)),
    )
