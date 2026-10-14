"""The pending-question (“flow”) layer — the fix for repeated, wrong saves.

The owner kept typing in the bot and the value kept being overwritten. The cause
was a question that stayed armed for days: whatever arrived next became its
answer. These tests pin down the three rules that stop it:

1. a question expires after ``FLOW_TIMEOUT_SECONDS``;
2. a wrong answer is tolerated ``MAX_TRIES`` times, then the question is dropped;
3. tapping any button (or “✖️ বাতিল”) drops the question immediately.
"""
from __future__ import annotations

import time

import pytest

from app.handlers import state
from app.services import settings as settings_service


@pytest.fixture(autouse=True)
def clean_state():
    for holder in (state.pending_input, state.search_pending, state.search_at,
                   state.flow_ctx, state.link_gen, state.batch_mode,
                   state.wrong_answers):
        holder.clear()
    yield
    for holder in (state.pending_input, state.search_pending, state.search_at,
                   state.flow_ctx, state.link_gen, state.batch_mode,
                   state.wrong_answers):
        holder.clear()


def test_ask_stamps_the_time_and_keeps_the_context():
    state.ask(1, "channel_set", store_id=7)
    row = state.peek(1)
    assert row["action"] == "channel_set"
    assert row["ctx"]["store_id"] == 7
    assert state.pending_age(1) < 1


def test_flow_timeout_comes_from_the_settings_registry():
    seen = settings_service.get_int("FLOW_TIMEOUT_SECONDS")
    assert seen == state.flow_timeout()          # the registry value wins
    assert settings_service.set("FLOW_TIMEOUT_SECONDS", "5")[0] is True
    try:
        assert state.flow_timeout() == 30        # never below the hard floor
    finally:
        settings_service.set("FLOW_TIMEOUT_SECONDS", str(seen))


def test_a_fresh_question_survives_peek_but_a_stale_one_dies():
    state.ask(2, "create_store")
    assert state.peek(2) is not None
    state.pending_input[2]["at"] = time.time() - state.flow_timeout() - 1
    assert state.peek(2) is None                 # peek(fresh=True) is the default
    assert state.pending_input == {}


def test_drop_if_stale_reports_whether_something_was_dropped():
    state.ask(3, "create_store")
    assert state.drop_if_stale(3) is False       # still fresh
    state.pending_input[3]["at"] = 0.0           # the epoch → definitely stale
    assert state.drop_if_stale(3) is True
    assert state.peek(3) is None
    assert state.drop_if_stale(3) is False       # nothing left to drop


def test_wrong_answers_are_counted_per_question_and_capped():
    assert state.tries_left(4, "drip_count") == state.MAX_TRIES
    assert [state.bump_tries(4, "drip_count") for _ in range(3)] == [1, 2, 3]
    assert state.tries_left(4, "drip_count") == 0
    assert state.tries_left(4, "other_question") == state.MAX_TRIES   # per question
    state.ask(4, "drip_count")                   # re-asking starts a clean count
    assert state.tries_left(4, "drip_count") == state.MAX_TRIES


def test_clear_forgets_everything_about_a_user():
    state.ask(5, "create_store")
    state.start_search(5, 9)
    state.flow_ctx[5] = {"store_id": 9}
    state.link_gen[5] = {"ids": [1]}
    state.bump_tries(5, "create_store")
    state.clear(5)
    assert state.peek(5) is None
    assert 5 not in state.search_pending
    assert state.flow_ctx == {} and state.link_gen == {}
    assert (5, "create_store") not in state.wrong_answers


def test_search_prompts_expire_like_any_other_question():
    state.start_search(6, 11)
    assert state.is_searching(6) is True
    assert state.peek(6) is None                 # search is not a pending_input row
    state.search_at[6] = time.time() - state.flow_timeout() - 5
    assert state.is_searching(6) is False
    assert state.take_search(6) is None
    assert state.search_pending == {}


def test_take_search_pops_once():
    state.start_search(7, 12)
    assert state.take_search(7) == 12
    assert state.take_search(7) is None          # consumed — never reused


def test_cancel_buttons_offer_a_way_out():
    data = state.cancel_buttons()
    assert data == [[state.cancel_buttons()[0][0]]]          # one button, one row
    assert "বাতিল" in data[0][0].text


def test_end_flow_clears_context_but_keeps_the_question():
    state.ask(8, "studio_text", store_id=3)
    state.flow(8, store_id=3, chat_id=-100)
    state.end_flow(8)
    assert state.ctx(8) == {}
    assert state.peek(8)["action"] == "studio_text"           # still waiting
