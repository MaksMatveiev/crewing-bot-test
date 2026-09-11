import pytest

from crewing_bot import funnel


def test_new_state_starts_at_greeting():
    state = funnel.State()
    assert state.step == funnel.GREETING


def test_select_vacancy_moves_to_profile_and_asks_name():
    state = funnel.select_vacancy(funnel.State(), vacancy_id=7, screening_questions=["STCW?"])
    assert state.step == funnel.COLLECTING_PROFILE
    assert state.vacancy_id == 7
    assert state.screening_questions == ["STCW?"]
    assert "зовут" in funnel.next_question(state)


def test_record_profile_fills_several_fields_at_once():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    state = funnel.record_profile(state, {"full_name": "Ivanov Ivan", "citizenship": "Ukraine"})
    assert state.profile["full_name"] == "Ivanov Ivan"
    assert state.profile["citizenship"] == "Ukraine"
    # контакт ещё не заполнен — его и спрашиваем
    assert "email" in funnel.next_question(state).lower()


def test_record_profile_ignores_empty_values():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    state = funnel.record_profile(state, {"full_name": None, "citizenship": ""})
    assert state.profile == {}


def test_state_is_not_mutated_in_place():
    before = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    funnel.record_profile(before, {"full_name": "Ivanov Ivan"})
    assert before.profile == {}


def _filled_profile_state():
    state = funnel.select_vacancy(funnel.State(), 7, ["Есть ли действующий STCW?", "Виза US C1/D?"])
    return funnel.record_profile(state, {name: "x" for name, _ in funnel.PROFILE_FIELDS})


def test_profile_complete_only_when_all_fields_filled():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    assert funnel.profile_complete(state) is False
    assert funnel.profile_complete(_filled_profile_state()) is True


def test_screening_asks_questions_one_by_one():
    state = funnel.start_screening(_filled_profile_state())
    assert state.step == funnel.SCREENING
    assert funnel.next_question(state) == "Есть ли действующий STCW?"

    state = funnel.record_screening_answer(state, "Да, до 2028 года")
    assert state.screening["Есть ли действующий STCW?"] == "Да, до 2028 года"
    assert funnel.next_question(state) == "Виза US C1/D?"


def test_screening_finished_moves_to_slots():
    state = funnel.start_screening(_filled_profile_state())
    state = funnel.record_screening_answer(state, "Да")
    state = funnel.record_screening_answer(state, "Нет")
    assert state.step == funnel.CHOOSING_SLOT
    assert funnel.next_question(state) is None


def test_select_slot_by_number_picks_id_from_database_list():
    state = funnel.offer_slots(funnel.State(step=funnel.CHOOSING_SLOT), [101, 102, 103])
    state = funnel.select_slot(state, 2)
    assert state.slot_id == 102


def test_select_slot_out_of_range_is_rejected():
    state = funnel.offer_slots(funnel.State(step=funnel.CHOOSING_SLOT), [101, 102])
    with pytest.raises(ValueError):
        funnel.select_slot(state, 5)
    with pytest.raises(ValueError):
        funnel.select_slot(state, 0)
    # состояние не испорчено
    assert state.slot_id is None


def test_add_note_accumulates_raw_answers():
    state = funnel.add_note(funnel.State(), "24 мес наверное")
    state = funnel.add_note(state, "виза в процессе")
    assert "24 мес наверное" in state.notes
    assert "виза в процессе" in state.notes


def test_block_stops_the_funnel():
    state = funnel.block(funnel.State(), "уже записан на 12 марта, 10:00")
    assert state.step == funnel.BLOCKED
    assert "12 марта" in state.blocked_reason


def test_list_from_model_becomes_readable_text():
    # Модель на «танкера» отвечает списком, а колонка в базе текстовая:
    # без склейки рекрутер видит в заявке {танкера,контейнеровозы}.
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    state = funnel.record_profile(state, {"vessel_types": ["танкера", "контейнеровозы"]})
    assert state.profile["vessel_types"] == "танкера, контейнеровозы"


def test_single_item_list_loses_brackets_too():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    state = funnel.record_profile(state, {"vessel_types": ["танкера"]})
    assert state.profile["vessel_types"] == "танкера"


def test_empty_list_is_ignored_like_empty_value():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    state = funnel.record_profile(state, {"vessel_types": []})
    assert "vessel_types" not in state.profile


def test_numbers_from_model_stay_usable():
    state = funnel.select_vacancy(funnel.State(), 7, ["STCW?"])
    state = funnel.record_profile(state, {"rank_experience_months": 24})
    assert state.profile["rank_experience_months"] == 24


# --- выбор должности и типа флота ---

RANKS = ["2nd Engineer", "AB", "Master"]


def test_bare_number_picks_option():
    assert funnel.match_option("2", RANKS) == 1
    assert funnel.match_option(" 3 ", RANKS) == 2


def test_number_out_of_range_is_no_match():
    assert funnel.match_option("9", RANKS) is None
    assert funnel.match_option("0", RANKS) is None


def test_text_matches_option_ignoring_case():
    assert funnel.match_option("ab", RANKS) == 1
    assert funnel.match_option("MASTER", RANKS) == 2


def test_partial_text_matches():
    # Моряк пишет сокращённо — «2nd eng» вместо «2nd Engineer».
    assert funnel.match_option("2nd eng", RANKS) == 0


def test_ambiguous_text_is_no_match():
    # Два совпадения — выбирать за кандидата нельзя, переспросим.
    assert funnel.match_option("e", ["2nd Engineer", "Chief Engineer"]) is None


def test_unknown_text_is_no_match():
    assert funnel.match_option("повар", RANKS) is None
    assert funnel.match_option("", RANKS) is None


def test_rank_then_vessel_then_vacancy():
    state = funnel.offer_ranks(funnel.State(), RANKS)
    assert state.step == funnel.CHOOSING_RANK

    state = funnel.select_rank(state, 0)
    assert state.wanted_rank == "2nd Engineer"
    assert state.step == funnel.CHOOSING_VESSEL_TYPE

    state = funnel.offer_vessel_types(state, ["bulk carrier", "tanker"])
    state = funnel.select_vessel_type(state, 1)
    assert state.wanted_vessel_type == "tanker"
    assert state.step == funnel.CHOOSING_VACANCY


def test_selecting_out_of_range_rank_is_rejected():
    state = funnel.offer_ranks(funnel.State(), RANKS)
    with pytest.raises(ValueError):
        funnel.select_rank(state, 5)
    assert state.wanted_rank == ""
