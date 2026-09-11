"""Воронка разговора с моряком: чистые переходы состояния.

Модуль намеренно ничего не знает ни про базу, ни про модель — поэтому
проверяется тестами целиком и мгновенно.
"""

from dataclasses import dataclass, field, replace

GREETING = "greeting"
CHOOSING_VACANCY = "choosing_vacancy"
COLLECTING_PROFILE = "collecting_profile"
SCREENING = "screening"
CHOOSING_SLOT = "choosing_slot"
CONFIRMED = "confirmed"
BLOCKED = "blocked"

# Порядок важен: вопросы задаются сверху вниз, первым — незаполненный.
PROFILE_FIELDS = [
    ("full_name", "Как вас зовут? Фамилия и имя, как в паспорте моряка."),
    ("contact", "Как с вами связаться — email или WhatsApp?"),
    ("citizenship", "Ваше гражданство?"),
    ("rank_experience_months", "Сколько месяцев опыта именно в этой должности?"),
    ("total_experience_months", "Какой общий стаж в море, в месяцах?"),
    ("vessel_types", "На каких типах судов вы работали?"),
    ("readiness_date", "С какой даты готовы заступить на контракт?"),
]


@dataclass(frozen=True)
class State:
    step: str = GREETING
    vacancy_id: int | None = None
    profile: dict = field(default_factory=dict)
    screening_questions: list = field(default_factory=list)
    screening: dict = field(default_factory=dict)
    slot_ids: list = field(default_factory=list)
    slot_id: int | None = None
    blocked_reason: str = ""
    # сырые ответы, которые модель не смогла разобрать — уйдут рекрутеру
    notes: str = ""


def select_vacancy(state: State, vacancy_id: int, screening_questions: list) -> State:
    return replace(
        state,
        step=COLLECTING_PROFILE,
        vacancy_id=vacancy_id,
        screening_questions=list(screening_questions),
    )


def _readable(value):
    """Привести значение от модели к виду, пригодному для показа человеку.

    Модель на «танкера» отвечает списком, а колонки в базе текстовые.
    Без склейки рекрутер увидел бы в заявке {танкера,контейнеровозы} —
    Postgres так записывает список в текстовое поле. Числа не трогаем:
    они уходят в числовые колонки.
    """
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return value


def record_profile(state: State, parsed: dict) -> State:
    """Записать распознанные поля анкеты. Пустые значения игнорируются."""
    known = {name for name, _ in PROFILE_FIELDS}
    filled = dict(state.profile)
    for name, value in parsed.items():
        value = _readable(value)
        if name in known and value not in (None, "", []):
            filled[name] = value
    return replace(state, profile=filled)


def profile_complete(state: State) -> bool:
    return all(name in state.profile for name, _ in PROFILE_FIELDS)


def start_screening(state: State) -> State:
    return replace(state, step=SCREENING)


def record_screening_answer(state: State, answer: str) -> State:
    """Записать ответ на текущий вопрос скрининга и перейти к следующему.

    Когда вопросы кончились — воронка переходит к выбору слота.
    """
    pending = [q for q in state.screening_questions if q not in state.screening]
    if not pending:
        return replace(state, step=CHOOSING_SLOT)
    answers = dict(state.screening)
    answers[pending[0]] = answer
    step = SCREENING if len(pending) > 1 else CHOOSING_SLOT
    return replace(state, screening=answers, step=step)


def offer_slots(state: State, slot_ids: list) -> State:
    """Запомнить слоты, показанные кандидату. Номер в чате — индекс в этом списке."""
    return replace(state, step=CHOOSING_SLOT, slot_ids=list(slot_ids))


def select_slot(state: State, number: int) -> State:
    """Выбрать слот по номеру из показанного списка.

    Номер — индекс в списке из базы, поэтому несуществующее время
    выбрать невозможно: за пределами списка будет ValueError.
    """
    if not 1 <= number <= len(state.slot_ids):
        raise ValueError(f"нет слота с номером {number}")
    return replace(state, slot_id=state.slot_ids[number - 1])


def confirm(state: State) -> State:
    return replace(state, step=CONFIRMED)


def block(state: State, reason: str) -> State:
    return replace(state, step=BLOCKED, blocked_reason=reason)


def add_note(state: State, text: str) -> State:
    """Копить сырые ответы, которые не удалось разобрать.

    Рекрутер увидит их в заявке — лучше показать человеку исходный
    текст, чем молча потерять его.
    """
    joined = f"{state.notes}\n{text}".strip() if state.notes else text.strip()
    return replace(state, notes=joined)


def next_question(state: State) -> str | None:
    """Вопрос, который бот задаёт прямо сейчас.

    Список вакансий и список слотов рисует app.py из данных базы —
    здесь только вопросы анкеты и скрининга.
    """
    if state.step == COLLECTING_PROFILE:
        for name, question in PROFILE_FIELDS:
            if name not in state.profile:
                return question
    if state.step == SCREENING:
        for question in state.screening_questions:
            if question not in state.screening:
                return question
    return None
