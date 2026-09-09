"""Крюинг-бот: вкладка кандидата и вкладка рекрутера.

Запуск локально:
  python crewing_bot/app.py
"""

import hmac
import os
import re
import sys
import time
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crewing_bot import brain, db, funnel

load_dotenv()

KNOWLEDGE = (Path(__file__).parent / "knowledge.md").read_text(encoding="utf-8")


def to_plain_text(content) -> str:
    """Gradio 6 отдаёт content списком блоков, а модель ждёт строку."""
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        )
    return content


def render_vacancies(vacancies: list) -> str:
    return "\n".join(
        f"{number}. {v['rank']} — {v['vessel_type']}, "
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
    vacancies = db.list_active_vacancies(conn)

    if state.step == funnel.BLOCKED:
        return state.blocked_reason, vars(state)

    if state.step == funnel.CONFIRMED:
        return "Вы уже записаны. Менеджер свяжется с вами перед интервью.", vars(state)

    # Приветствие: показываем вакансии и ждём номер
    if state.step == funnel.GREETING:
        if not vacancies:
            return "Сейчас открытых вакансий нет. Загляните позже.", vars(state)
        state = funnel.State(step=funnel.CHOOSING_VACANCY)
        return (
            "Здравствуйте! Я помощник крюингового агентства. "
            "Вот открытые вакансии — ответьте номером:\n\n"
            + render_vacancies(vacancies)
        ), vars(state)

    vacancy = db.get_vacancy(conn, state.vacancy_id) if state.vacancy_id else None
    question, resume, resumed = _current_question(conn, state, vacancies)

    # Router: встречный вопрос не сбивает воронку — и на шагах выбора
    # вакансии и слота тоже. Сообщение, которое целиком является номером,
    # модели не показываем: это заведомо выбор из списка.
    if question and _parse_number(message) is None:
        decision = brain.classify(message, question)
        if brain.is_unavailable(decision):
            # Модель молчит — воронку не двигаем, вопрос повторяем.
            return _model_down(resume), vars(resumed)
        if decision == "вопрос":
            reply = brain.answer(message, KNOWLEDGE, _vacancy_text(vacancy))
            if brain.is_unavailable(reply):
                return _model_down(resume), vars(resumed)
            return f"{reply}\n\n{resume}", vars(resumed)

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
    chosen = funnel.confirm(chosen)
    return (f"✅ Записал вас на интервью {when_text}. "
            "Менеджер свяжется с вами по указанному контакту."), vars(chosen)


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


def recruiter_vacancies(password: str) -> str:
    error = _guard(password)
    if error:
        return error
    conn, error = _open_conn()
    if error:
        return error
    try:
        vacancies = db.list_active_vacancies(conn)
    finally:
        conn.close()
    if not vacancies:
        return "Вакансий пока нет."
    return "\n".join(
        f"#{v['id']} {v['rank']} — {v['vessel_type']}, {v['contract_months']} мес, "
        f"${v['salary_usd']}, вопросов скрининга: {len(v['screening_questions'])}"
        for v in vacancies
    )


def recruiter_add_vacancy(password, rank, vessel_type, contract_months,
                          salary_usd, requirements, questions_text) -> str:
    error = _guard(password)
    if error:
        return error
    if not rank or not vessel_type:
        return "⚠️ Должность и тип судна обязательны."
    questions = [line.strip() for line in (questions_text or "").splitlines() if line.strip()]
    conn, error = _open_conn()
    if error:
        return error
    try:
        vacancy_id = db.create_vacancy(
            conn, rank, vessel_type, int(contract_months), int(salary_usd),
            requirements, questions or DEFAULT_SCREENING_QUESTIONS,
        )
    finally:
        conn.close()
    return f"✅ Вакансия #{vacancy_id} создана."


def recruiter_open_slots(password, day_text, start_hhmm, end_hhmm, step_min) -> str:
    error = _guard(password)
    if error:
        return error
    from datetime import date as date_type
    try:
        day = date_type.fromisoformat((day_text or "").strip())
    except ValueError:
        return "⚠️ Дата в формате ГГГГ-ММ-ДД, например 2026-09-15."
    try:
        step = int(step_min)
    except (TypeError, ValueError):
        return "⚠️ Шаг — целое число минут больше нуля."
    if step <= 0:
        return "⚠️ Шаг должен быть больше нуля минут."

    conn, conn_error = _open_conn()
    if conn_error:
        return conn_error
    try:
        created = db.open_slots(conn, day, start_hhmm.strip(), end_hhmm.strip(), step)
    except ValueError as error:
        # Некорректный шаг или время — понятный текст рекрутеру, а не сбой.
        return f"⚠️ {error}"
    finally:
        conn.close()
    return f"✅ Открыто слотов: {created} (время UTC)."


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


def build_ui():
    with gr.Blocks(title="Крюинг-агентство «Меридиан»") as demo:
        with gr.Tab("Кандидат"):
            state = gr.State({})
            gr.ChatInterface(
                fn=candidate_chat,
                additional_inputs=[state],
                additional_outputs=[state],
                title="Запись на интервью",
                description="Выберите вакансию, ответьте на вопросы и заберите время интервью.",
            )

        with gr.Tab("Рекрутер"):
            password = gr.Textbox(label="Пароль", type="password")

            gr.Markdown("### Вакансии")
            vacancies_out = gr.Textbox(label="Открытые вакансии", lines=6)
            gr.Button("Показать вакансии").click(
                recruiter_vacancies, inputs=password, outputs=vacancies_out
            )

            rank = gr.Textbox(label="Должность", placeholder="2nd Engineer")
            vessel_type = gr.Textbox(label="Тип судна", placeholder="bulk carrier")
            contract_months = gr.Number(label="Контракт, мес", value=6)
            salary_usd = gr.Number(label="Ставка, $", value=6500)
            requirements = gr.Textbox(label="Требования", lines=3)
            questions_text = gr.Textbox(
                label="Вопросы скрининга — по одному в строке. Пусто = набор по умолчанию",
                lines=6,
            )
            add_out = gr.Textbox(label="Результат")
            gr.Button("Добавить вакансию").click(
                recruiter_add_vacancy,
                inputs=[password, rank, vessel_type, contract_months,
                        salary_usd, requirements, questions_text],
                outputs=add_out,
            )

            gr.Markdown("### Слоты интервью")
            gr.Markdown("Время слотов задаётся и хранится в **UTC** — "
                        "кандидат видит его с той же пометкой.")
            day_text = gr.Textbox(label="Дата (ГГГГ-ММ-ДД, UTC)")
            start_hhmm = gr.Textbox(label="С (UTC)", value="10:00")
            end_hhmm = gr.Textbox(label="До (UTC)", value="17:00")
            step_min = gr.Number(label="Шаг, мин (больше нуля)", value=30)
            slots_out = gr.Textbox(label="Результат")
            gr.Button("Открыть слоты").click(
                recruiter_open_slots,
                inputs=[password, day_text, start_hhmm, end_hhmm, step_min],
                outputs=slots_out,
            )

            gr.Markdown("### Заявки")
            applications_out = gr.Textbox(label="Заявки кандидатов", lines=20)
            gr.Button("Показать заявки").click(
                recruiter_applications, inputs=password, outputs=applications_out
            )
    return demo


if __name__ == "__main__":
    build_ui().launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7861)),
    )
