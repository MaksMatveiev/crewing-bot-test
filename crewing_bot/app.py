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
from dataclasses import replace
from datetime import date, datetime, timedelta
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


# Сетка карточек собрана из настоящих кнопок Gradio, а не из разметки:
# нажатие внутри HTML до сервера не доходит. Поэтому карточек ровно
# столько, сколько заготовлено, а лишние прячутся.
MAX_CARDS = 24

ALL_RANKS_LABEL = "Все должности"


def rank_choices(counts) -> list:
    """Варианты выпадающего списка должностей с числом вакансий."""
    total = sum(item["count"] for item in counts)
    options = [(f"{ALL_RANKS_LABEL} ({total})", "")]
    options += [(f"{item['rank']} ({item['count']})", item["rank"])
                for item in counts]
    return options


def card_html(vacancy) -> str:
    """Одна карточка: снимок судна и короткая выжимка.

    Порядок как на витрине: сверху мелко тип флота, затем должность,
    условия и ставка. Так глаз цепляется за должность, а не за цену.
    """
    closed = "" if vacancy.get("is_active", True) else (
        "<p class='vac-closed'>закрыта</p>")
    return (
        # Без loading="lazy": внутри разметки, которую Gradio вставляет
        # уже после отрисовки, браузер не начинал загрузку вовсе — на
        # сервере вместо снимков оставались пустые прямоугольники.
        f"<div class='vac-photo'><img src='{vessel_photo(vacancy['vessel_type'])}'"
        f" alt='{_escape(vacancy['vessel_type'])}'></div>"
        "<div class='vac-text'>"
        f"<p class='vac-vessel'>{_escape(vacancy['vessel_type'])}</p>"
        f"<h4>{_escape(vacancy['rank'])}</h4>"
        f"<p class='vac-meta'>контракт {vacancy['contract_months']} мес</p>"
        f"<p class='vac-salary'>${vacancy['salary_usd']} / мес</p>"
        f"{closed}"
        "</div>"
    )


def fill_cards(vacancies) -> list:
    """Обновления для всех заготовленных карточек.

    По две штуки на карточку: показать ли её и что в ней написано.
    """
    updates = []
    for position in range(MAX_CARDS):
        if position < len(vacancies):
            updates.append(gr.update(visible=True))
            updates.append(gr.update(value=card_html(vacancies[position])))
        else:
            updates.append(gr.update(visible=False))
            updates.append(gr.update(value=""))
    return updates


def _browse(vacancies, message):
    return [[v["id"] for v in vacancies], message] + fill_cards(vacancies)


def candidate_ranks():
    """Список должностей для выпадающего окна. Пустой, если база молчит."""
    conn, error = _open_conn()
    if error:
        return gr.update(choices=[(ALL_RANKS_LABEL, "")], value="")
    try:
        counts = db.list_rank_counts(conn)
    finally:
        conn.close()
    return gr.update(choices=rank_choices(counts), value="")


def candidate_browse(rank: str = ""):
    """Показать вакансии — все или по выбранной должности."""
    conn, error = _open_conn()
    if error:
        return _browse([], error)
    try:
        found = db.list_all_vacancies(conn, rank or None, "open")
    finally:
        conn.close()

    if not found:
        return _browse([], "Сейчас по этой должности вакансий нет.")
    return _browse(found[:MAX_CARDS], f"Открытых вакансий: {len(found)}")


def candidate_start(vacancy_id: int, state_dict: dict):
    """Нажатие «Записаться» на карточке: открыть помощника и начать анкету."""
    history, state = candidate_sign_up(vacancy_id, state_dict)
    return history, state, gr.update(visible=True)


def candidate_close_assistant():
    """Закрыть помощника и вернуться к вакансиям.

    Разговор при этом сбрасывается: человек ушёл выбирать другую
    вакансию, и прежняя анкета к ней уже не относится.
    """
    return gr.update(visible=False), {}, []


def candidate_reply(message: str, history, state_dict: dict):
    """Ответ кандидата помощнику.

    Свой маленький чат вместо готового ChatInterface: тот приносит с
    собой кнопки повтора, отмены и счётчики, а здесь нужен разговор и
    ничего кроме.
    """
    message = to_plain_text(message).strip()
    history = list(history or [])
    if not message:
        return "", history, state_dict or {}

    reply, state = handle(message, state_dict or {})
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    return "", history, state


def recruiter_browse(password: str, only: str = "все"):
    """Вакансии рекрутера карточками — теми же, что видит кандидат."""
    error = _guard(password)
    if error:
        return _browse([], error)

    conn, failure = _open_conn()
    if failure:
        return _browse([], failure)
    try:
        found = db.list_all_vacancies(conn, None, _status_filter(only))
    finally:
        conn.close()

    if not found:
        return _browse([], "Вакансий пока нет.")
    return _browse(found[:MAX_CARDS], f"Вакансий: {len(found)}")


BLANK_FORM = ("", "", 6, 6500, "", "")


def recruiter_new_vacancy(password: str):
    """Открыть пустую форму. Поля очищаются, чтобы не переписать чужую."""
    error = _guard(password)
    if error:
        return (0, gr.update(visible=False), "### Новая вакансия",
                error) + BLANK_FORM

    return (0, gr.update(visible=True), "### Новая вакансия", "") + BLANK_FORM


def recruiter_edit_vacancy(password: str, vacancy_id: int):
    """Открыть форму с данными выбранной вакансии."""
    error = _guard(password)
    if error:
        return (0, gr.update(visible=False), "### Вакансия", error) + BLANK_FORM

    fields = recruiter_load_vacancy(password, vacancy_id)
    if fields == BLANK_FORM:
        return (0, gr.update(visible=False), "### Вакансия",
                "⚠️ Вакансия не открылась — обновите список.") + BLANK_FORM

    return ((int(vacancy_id), gr.update(visible=True),
             f"### Вакансия №{int(vacancy_id)}", "") + tuple(fields))


def recruiter_close_form():
    """Свернуть форму, ничего не сохраняя."""
    return 0, gr.update(visible=False), ""


def candidate_sign_up(vacancy_id: int, state_dict: dict):
    """Начать запись на выбранную вакансию.

    Дальше работает та же воронка, что и в чате: анкета, скрининг, выбор
    времени. Окно с вакансиями только заменяет выбор должности и флота
    вслепую — правила записи от этого не меняются.
    """
    if not vacancy_id:
        return gr.update(), state_dict or {}

    conn, error = _open_conn()
    if error:
        return [{"role": "assistant", "content": error}], state_dict or {}
    try:
        vacancy = db.get_vacancy(conn, int(vacancy_id))
        if not vacancy or not vacancy["is_active"]:
            return ([{"role": "assistant",
                      "content": "Эта вакансия уже закрыта. Выберите другую, "
                                 "пожалуйста."}], state_dict or {})
        has_days = bool(db.list_open_days(conn))
    finally:
        conn.close()

    if not has_days:
        # Записывать некуда: слотов нет ни на один день. Честнее сказать
        # сразу, чем провести человека через анкету и отказать в конце.
        return ([{"role": "assistant",
                  "content": "Свободных дат для интервью пока нет. "
                             "Загляните, пожалуйста, позже — рекрутер "
                             "открывает дни каждую неделю."}],
                state_dict or {})

    state = funnel.State(**(state_dict or {}))
    state = replace(state,
                    wanted_rank=vacancy["rank"],
                    wanted_vessel_type=vacancy["vessel_type"])
    state = funnel.select_vacancy(state, vacancy["id"],
                                  vacancy["screening_questions"] or [])

    greeting = (f"Записываю вас на вакансию {vacancy['rank']} — "
                f"{vacancy['vessel_type']}. Задам несколько вопросов "
                f"для заявки.\n\n{funnel.next_question(state)}")
    return [{"role": "assistant", "content": greeting}], vars(state)


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


def _status_filter(status: str):
    """Подпись фильтра → значение для db.list_all_vacancies."""
    return {"открытые": "open", "закрытые": "closed"}.get(status)


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


def _month_slots(conn, year, month):
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    return db.list_slot_days(conn, first, last)


def interview_hours() -> list:
    """Время приёмов рабочего дня: с 08:00 до 14:00 по полчаса."""
    start = datetime.strptime(INTERVIEW_START, "%H:%M")
    finish = datetime.strptime(INTERVIEW_END, "%H:%M")
    hours = []
    while start < finish:
        hours.append(start.strftime("%H:%M"))
        start += timedelta(minutes=INTERVIEW_STEP_MIN)
    return hours


INTERVIEW_HOURS = interview_hours()


def _hour_buttons(open_at: dict):
    """Обновления кнопок времени: зелёная — приём открыт.

    Занятое время тоже зелёное, но нажать его нельзя: за ним стоит
    человек, которому уже назвали час.
    """
    updates = []
    for hhmm in INTERVIEW_HOURS:
        status = open_at.get(hhmm)
        updates.append(gr.update(
            variant="primary" if status else "secondary",
            interactive=status != "booked",
        ))
    return updates


def _day_answer(year, month, day_number, open_at, days_with_slots, message=""):
    """Полный ответ календаря: месяц, выбранный день, часы и подпись."""
    chosen = ""
    if day_number:
        day = date(year, month, day_number)
        chosen = f"**{day.strftime('%d.%m.%Y')}** — отметьте часы приёма"
    return ([month_title(year, month), message, chosen]
            + _day_buttons(year, month, days_with_slots)
            + _hour_buttons(open_at))


def _empty_day_answer(year, month, message=""):
    return _day_answer(year, month, None, {}, {}, message)


def recruiter_month(password: str, year: int, month: int, day_number=None,
                    message: str = ""):
    """Нарисовать месяц и, если день выбран, часы этого дня."""
    error = _guard(password)
    if error:
        return _empty_day_answer(year, month, error)

    conn, failure = _open_conn()
    if failure:
        return _empty_day_answer(year, month, failure)
    try:
        days = _month_slots(conn, year, month)
        open_at = (db.list_day_slots(conn, date(year, month, day_number))
                   if day_number else {})
    finally:
        conn.close()

    if not message:
        opened = sum(1 for counts in days.values()
                     if counts["free"] or counts["booked"])
        booked = sum(counts["booked"] for counts in days.values())
        message = f"Открытых дней: {opened} · записей: {booked}"
    return _day_answer(year, month, day_number, open_at, days, message)


def recruiter_pick_day(password: str, year: int, month: int, number: int):
    """Выбрать день — показать его часы. Ничего не меняет в базе."""
    try:
        day = date(year, month, int(number))
    except (TypeError, ValueError):
        return [0] + _empty_day_answer(year, month)

    if day < date.today():
        return [0] + recruiter_month(password, year, month, None,
                                     "⚠️ Прошедший день открыть нельзя.")
    return [int(number)] + recruiter_month(password, year, month, int(number))


def recruiter_toggle_time(password: str, year: int, month: int, number: int,
                          hhmm: str):
    """Открыть или снять один приём выбранного дня."""
    error = _guard(password)
    if error:
        return _empty_day_answer(year, month, error)
    if not number:
        return recruiter_month(password, year, month, None,
                               "⚠️ Сначала выберите день в календаре.")

    day = date(year, month, int(number))
    conn, failure = _open_conn()
    if failure:
        return _empty_day_answer(year, month, failure)
    try:
        status = db.list_day_slots(conn, day).get(hhmm)
        if status == "booked":
            message = f"⚠️ {hhmm} уже занят кандидатом — час остаётся."
        elif status == "open":
            db.close_slot(conn, day, hhmm)
            message = f"Снят приём {day.strftime('%d.%m')} в {hhmm}."
        else:
            db.open_slot(conn, day, hhmm, INTERVIEW_STEP_MIN)
            message = f"Открыт приём {day.strftime('%d.%m')} в {hhmm}."
    except ValueError as failure:
        return recruiter_month(password, year, month, int(number),
                               f"⚠️ {failure}")
    finally:
        conn.close()

    return recruiter_month(password, year, month, int(number), message)


def recruiter_whole_day(password: str, year: int, month: int, number: int,
                        open_it: bool):
    """Открыть или снять весь рабочий день разом."""
    error = _guard(password)
    if error:
        return _empty_day_answer(year, month, error)
    if not number:
        return recruiter_month(password, year, month, None,
                               "⚠️ Сначала выберите день в календаре.")

    day = date(year, month, int(number))
    if open_it and day < date.today():
        return recruiter_month(password, year, month, None,
                               "⚠️ Прошедший день открыть нельзя.")

    conn, failure = _open_conn()
    if failure:
        return _empty_day_answer(year, month, failure)
    try:
        if open_it:
            created = db.open_slots(conn, day, INTERVIEW_START, INTERVIEW_END,
                                    INTERVIEW_STEP_MIN)
            message = (f"{day.strftime('%d.%m')}: открыт весь день, "
                       f"новых приёмов {created}.")
        else:
            removed, left = db.close_free_day(conn, day)
            message = (f"{day.strftime('%d.%m')}: снято {removed}"
                       + (f", занятых осталось {left}." if left else "."))
    except ValueError as failure:
        return recruiter_month(password, year, month, int(number),
                               f"⚠️ {failure}")
    finally:
        conn.close()

    return recruiter_month(password, year, month, int(number), message)


def recruiter_month_step(password: str, year: int, month: int, step: int):
    """Листнуть календарь. Выбранный день сбрасывается: он был в том месяце."""
    year, month = shift_month(year, month, step)
    return [year, month, 0] + recruiter_month(password, year, month)


def _day_click(position: int):
    """Обработчик одной клетки календаря.

    Клетка знает только своё место в сетке — число месяца выясняется в
    момент нажатия. Иначе при листании месяцев пришлось бы пересобирать
    все сорок две кнопки заново.
    """
    def click(password, year, month):
        number = month_cells(year, month)[position]
        if number is None:
            return [0] + recruiter_month(password, year, month)
        return recruiter_pick_day(password, year, month, number)

    return click


def _hour_click(hhmm: str):
    """Кнопка одного часа приёма в выбранном дне."""
    def click(password, year, month, day_number):
        return recruiter_toggle_time(password, year, month, day_number, hhmm)

    return click


def _signup_click(position: int):
    """Кнопка «Записаться» на карточке под этим местом в сетке.

    Карточка знает только своё место: какая там вакансия, выясняется в
    момент нажатия по списку найденного.
    """
    def click(ids, state_dict):
        if not ids or position >= len(ids):
            return gr.update(), state_dict or {}, gr.update()
        return candidate_start(ids[position], state_dict)

    return click


def _edit_click(position: int):
    """Кнопка «Изменить» на карточке рекрутера."""
    def click(password, ids):
        if not ids or position >= len(ids):
            return recruiter_close_form() + ("", ) + BLANK_FORM
        return recruiter_edit_vacancy(password, ids[position])

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


AGENCY_NAME = "Меридиан"

HEADER_HTML = (
    '<link rel="icon" type="image/svg+xml"'
    ' href="/static/favicon.svg?v={version}">'
)


def header_html() -> str:
    """Значок вкладки.

    Название агентства рисует файл стилей: Gradio вырезает из разметки
    теги <style>, и заданное здесь правило до страницы не доходило.
    Отпечаток в адресе заставляет браузер перечитать значок после
    выкладки.
    """
    return HEADER_HTML.format(version=style_version())


CARDS_PER_ROW = 3


def _card_pool(button_label: str, variant: str = "primary"):
    """Заготовить сетку карточек.

    Карточки нельзя рисовать по числу вакансий: Gradio собирает страницу
    один раз при запуске. Поэтому их ровно MAX_CARDS, а лишние скрыты.
    """
    cards = []
    # Строки нужны только Gradio: раскладку задаёт сетка в стилях. Иначе
    # на узком экране строка из трёх ломалась как две и одна, и карточки
    # шли неровно.
    with gr.Column(elem_classes="cards-grid"):
        for start in range(0, MAX_CARDS, CARDS_PER_ROW):
            with gr.Row():
                for _ in range(CARDS_PER_ROW):
                    with gr.Column(visible=False, elem_classes="vac-card") as box:
                        body = gr.HTML("")
                        button = gr.Button(button_label, variant=variant)
                    cards.append((box, body, button))
    return cards


def _card_outputs(cards):
    """Выходы обновления сетки: по два на карточку — видимость и текст."""
    outputs = []
    for box, body, _ in cards:
        outputs += [box, body]
    return outputs


def build_ui():
    with gr.Blocks(title="Крюинг-агентство «Меридиан»") as demo:
        # В Gradio 6 у Blocks нет параметра css, поэтому подключаем стили
        # ссылкой на файл, который отдаёт само приложение.
        gr.HTML(f'<link rel="stylesheet" href="/static/style.css?v={style_version()}">'
                + header_html())

        with gr.Tab("Кандидат"):
            state = gr.State({})
            found_ids = gr.State([])

            with gr.Row():
                with gr.Column(scale=3, elem_id="vacancy-side"):
                    # Заголовка «Вакансии» нет: вкладка и так о них, а на
                    # телефоне он занимал строку впустую.
                    # Подпись поля не нужна: в самом списке написано
                    # «Все должности», и что это выбор должности, видно.
                    rank_picker = gr.Dropdown(
                        choices=[(ALL_RANKS_LABEL, "")], value="",
                        show_label=False, interactive=True,
                        elem_id="rank-picker")
                    browse_message = gr.Markdown("")
                    cards = _card_pool("Записаться")

                # Колонку помощника прячем целиком, а не только её
                # содержимое: скрытая группа всё равно занимала бы место,
                # и карточки оставались бы зажатыми.
                with gr.Column(scale=1, elem_id="assistant-side",
                               visible=False) as assistant:
                    back_button = gr.Button("← Ко всем вакансиям",
                                            elem_id="assistant-back")
                    gr.HTML(
                        "<div class='assist-head'>"
                        "<span class='assist-spark'>&#10022;</span>"
                        "<span class='assist-name'>Помощник</span></div>"
                        "<div class='assist-orb'></div>")
                    chat = gr.Chatbot(
                        height=420, show_label=False,
                        elem_id="assistant-chat",
                        avatar_images=(str(STATIC_DIR / "avatar-user.svg"),
                                       str(STATIC_DIR / "favicon.svg")))
                    answer = gr.Textbox(
                        placeholder="Напишите ответ…", show_label=False,
                        lines=1, submit_btn=True, elem_id="assistant-input")

            browse_outputs = [found_ids, browse_message] + _card_outputs(cards)
            rank_picker.change(candidate_browse, inputs=rank_picker,
                               outputs=browse_outputs)

            for position, (_, _, button) in enumerate(cards):
                button.click(
                    _signup_click(position),
                    inputs=[found_ids, state],
                    outputs=[chat, state, assistant],
                )

            answer.submit(candidate_reply, inputs=[answer, chat, state],
                          outputs=[answer, chat, state])
            back_button.click(candidate_close_assistant,
                              outputs=[assistant, state, chat])

        with gr.Tab("Рекрутер"):
            # Пароль живёт в состоянии вкладки и подставляется в каждый вызов.
            # Скрытие блоков — только внешний вид: обработчики на сервере
            # проверяют пароль сами, иначе защиту обошли бы запросом мимо
            # интерфейса.
            session_password = gr.State("")
            editing_id = gr.State(0)
            # Рекрутерская сетка держит свои номера вакансий отдельно от
            # кандидатской: обе вкладки открыты в одном браузере разом.
            recruiter_ids = gr.State([])

            # Карточка входа держит свою ширину и стоит по центру: поле
            # пароля во весь экран выглядит как ошибка вёрстки.
            with gr.Column(elem_id="login-card") as login_box:
                gr.Markdown("## Вход для рекрутера")
                gr.Markdown("Введите пароль, чтобы открыть вакансии "
                            "и календарь интервью.")
                password = gr.Textbox(label="Пароль", type="password",
                                      placeholder="••••••••")
                login_button = gr.Button("Войти", variant="primary")
                login_message = gr.Markdown("")

            with gr.Group(visible=False) as workspace:
                with gr.Row():
                    with gr.Column(scale=3):
                        with gr.Row():
                            new_button = gr.Button("+ Новая вакансия",
                                                   variant="primary", scale=1)
                            status = gr.Radio(
                                list(STATUS_FILTERS), value="все",
                                label="Показывать", scale=2)
                        recruiter_message = gr.Markdown("")

                        with gr.Group(visible=False) as vacancy_form:
                            form_title = gr.Markdown("### Новая вакансия")
                            rank = gr.Textbox(label="Должность",
                                              placeholder="2nd Engineer")
                            vessel_type = gr.Textbox(label="Тип судна",
                                                     placeholder="bulk carrier")
                            with gr.Row():
                                contract_months = gr.Number(label="Контракт, мес",
                                                            value=6)
                                salary_usd = gr.Number(label="Ставка, $",
                                                       value=6500)
                            requirements = gr.Textbox(label="Требования", lines=3)
                            questions_text = gr.Textbox(
                                label="Вопросы скрининга — по одному в строке. "
                                      "Пусто = набор по умолчанию",
                                lines=5)
                            with gr.Row():
                                save_button = gr.Button("Сохранить",
                                                        variant="primary")
                                cancel_button = gr.Button("Отмена")
                            with gr.Row():
                                close_button = gr.Button("Закрыть вакансию")
                                reopen_button = gr.Button("Открыть снова")
                            form_message = gr.Markdown("")
                            gr.HTML(
                                "<p class='form-note'>Удаления нет —"
                                " на вакансию ссылаются заявки кандидатов.<br>"
                                "Закрытая вакансия просто не показывается"
                                " кандидату.</p>")

                        recruiter_cards = _card_pool("Изменить", "secondary")

                    # Календарь живёт рядом с вакансиями: рекрутер держит
                    # перед глазами и места, и даты, не переключая вкладок.
                    with gr.Column(scale=1, elem_id="calendar-side"):
                        today = date.today()
                        calendar_year = gr.State(today.year)
                        calendar_month = gr.State(today.month)
                        chosen_day = gr.State(0)

                        with gr.Row(elem_id="calendar-head"):
                            previous_month = gr.Button("‹", scale=1)
                            month_label = gr.Markdown(
                                f"### {month_title(today.year, today.month)}")
                            next_month = gr.Button("›", scale=1)

                        with gr.Column(elem_id="calendar-grid"):
                            with gr.Row():
                                for name in WEEKDAY_NAMES:
                                    gr.Markdown(f"**{name}**")
                            day_buttons = []
                            for week in range(CALENDAR_CELLS // 7):
                                with gr.Row():
                                    for cell in range(7):
                                        day_buttons.append(
                                            gr.Button("", interactive=False,
                                                      scale=1))

                        calendar_message = gr.Markdown("")
                        day_label = gr.Markdown("Выберите день в календаре")

                        with gr.Column(elem_id="hours-grid"):
                            hour_buttons = []
                            for start in range(0, len(INTERVIEW_HOURS), 3):
                                with gr.Row():
                                    for hhmm in INTERVIEW_HOURS[start:start + 3]:
                                        hour_buttons.append(
                                            gr.Button(hhmm, scale=1))

                        with gr.Row():
                            open_all = gr.Button("Открыть весь день")
                            close_all = gr.Button("Снять день")

                        gr.Markdown("### Кто записан")
                        applications_out = gr.Textbox(
                            label="Заявки кандидатов", lines=8)
                        gr.Button("Показать заявки").click(
                            recruiter_applications, inputs=session_password,
                            outputs=applications_out)

                calendar_outputs = ([month_label, calendar_message, day_label]
                                    + day_buttons + hour_buttons)

                form_fields = [rank, vessel_type, contract_months, salary_usd,
                               requirements, questions_text]
                form_outputs = ([editing_id, vacancy_form, form_title,
                                 form_message] + form_fields)
                grid_inputs = [session_password, status]
                grid_outputs = ([recruiter_ids, recruiter_message]
                                + _card_outputs(recruiter_cards))

                status.change(recruiter_browse, inputs=grid_inputs,
                              outputs=grid_outputs)
                new_button.click(recruiter_new_vacancy, inputs=session_password,
                                 outputs=form_outputs)
                cancel_button.click(
                    recruiter_close_form,
                    outputs=[editing_id, vacancy_form, form_message])

                for position, (_, _, button) in enumerate(recruiter_cards):
                    button.click(
                        _edit_click(position),
                        inputs=[session_password, recruiter_ids],
                        outputs=form_outputs,
                    )

                # После любой правки сетка перечитывается: иначе рекрутер
                # видит старое состояние и правит вслепую.
                save_button.click(
                    recruiter_save_vacancy,
                    inputs=[session_password, editing_id] + form_fields,
                    outputs=form_message,
                ).then(recruiter_browse, inputs=grid_inputs, outputs=grid_outputs)

                close_button.click(
                    lambda secret, chosen: recruiter_toggle_vacancy(
                        secret, chosen, False),
                    inputs=[session_password, editing_id], outputs=form_message,
                ).then(recruiter_browse, inputs=grid_inputs, outputs=grid_outputs)

                reopen_button.click(
                    lambda secret, chosen: recruiter_toggle_vacancy(
                        secret, chosen, True),
                    inputs=[session_password, editing_id], outputs=form_message,
                ).then(recruiter_browse, inputs=grid_inputs, outputs=grid_outputs)

                for position, button in enumerate(day_buttons):
                    button.click(
                        _day_click(position),
                        inputs=[session_password, calendar_year, calendar_month],
                        outputs=[chosen_day] + calendar_outputs,
                    )

                for hhmm, button in zip(INTERVIEW_HOURS, hour_buttons):
                    button.click(
                        _hour_click(hhmm),
                        inputs=[session_password, calendar_year, calendar_month,
                                chosen_day],
                        outputs=calendar_outputs,
                    )

                open_all.click(
                    lambda secret, year, month, day: recruiter_whole_day(
                        secret, year, month, day, True),
                    inputs=[session_password, calendar_year, calendar_month,
                            chosen_day],
                    outputs=calendar_outputs,
                )
                close_all.click(
                    lambda secret, year, month, day: recruiter_whole_day(
                        secret, year, month, day, False),
                    inputs=[session_password, calendar_year, calendar_month,
                            chosen_day],
                    outputs=calendar_outputs,
                )

                previous_month.click(
                    lambda secret, year, month: recruiter_month_step(
                        secret, year, month, -1),
                    inputs=[session_password, calendar_year, calendar_month],
                    outputs=[calendar_year, calendar_month, chosen_day]
                            + calendar_outputs,
                )
                next_month.click(
                    lambda secret, year, month: recruiter_month_step(
                        secret, year, month, 1),
                    inputs=[session_password, calendar_year, calendar_month],
                    outputs=[calendar_year, calendar_month, chosen_day]
                            + calendar_outputs,
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

            # После входа вакансии и календарь заполняются сами: иначе
            # рекрутер видит пустоту и должен нажимать «Обновить» руками.
            login_button.click(
                _login,
                inputs=password,
                outputs=[session_password, login_box, workspace,
                         login_message, password],
            ).then(
                recruiter_browse, inputs=grid_inputs, outputs=grid_outputs
            ).then(
                recruiter_month,
                inputs=[session_password, calendar_year, calendar_month],
                outputs=calendar_outputs,
            )

        # Список должностей и вакансии кандидат видит сразу при открытии.
        demo.load(candidate_ranks, outputs=rank_picker).then(
            candidate_browse, inputs=rank_picker, outputs=browse_outputs)

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
