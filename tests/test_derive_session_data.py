"""Tests for webapp_server.py's _derive_session_data - the pure per-request
version of the festival-session derivation that used to only ever mutate
shared module globals (_set_festival_data). The whole reason it was split
out is so two different groups' concurrently-resolved festival datasets
never leak into each other - see group_festivals.py / _festival_data_for."""

import webapp_server

_BEERS_A = [{"id": "1", "name": "A", "brewery": "X"}]
_SESSIONS_A = {"yellow": [{"id": "1", "name": "A", "brewery": "X"}]}

_BEERS_B = [{"id": "2", "name": "B", "brewery": "Y"}]
_SESSIONS_B = {"blue": [{"id": "2", "name": "B", "brewery": "Y"}]}


def test_derives_session_order_and_beer_sessions():
    beer_sessions, session_beer_ids, session_order, session_colors = webapp_server._derive_session_data(
        _BEERS_A, _SESSIONS_A
    )
    assert session_order == ["yellow"]
    assert beer_sessions["1"] == ["yellow"]
    assert session_beer_ids["yellow"] == {1}
    assert session_colors["yellow"] == "yellow"


def test_no_session_grouping_falls_back_to_single_synthetic_session():
    beer_sessions, session_beer_ids, session_order, session_colors = webapp_server._derive_session_data(
        _BEERS_A, {}
    )
    assert len(session_order) == 1
    synthetic = session_order[0]
    assert beer_sessions["1"] == [synthetic]
    assert session_beer_ids[synthetic] == {1}


def test_two_calls_with_different_datasets_do_not_leak_into_each_other():
    result_a = webapp_server._derive_session_data(_BEERS_A, _SESSIONS_A)
    result_b = webapp_server._derive_session_data(_BEERS_B, _SESSIONS_B)
    beer_sessions_a, session_beer_ids_a, session_order_a, _ = result_a
    beer_sessions_b, session_beer_ids_b, session_order_b, _ = result_b

    assert session_order_a == ["yellow"]
    assert session_order_b == ["blue"]
    assert "2" not in beer_sessions_a
    assert "1" not in beer_sessions_b
    assert session_beer_ids_a == {"yellow": {1}}
    assert session_beer_ids_b == {"blue": {2}}


def test_interleaved_calls_stay_independent():
    """The exact race this refactor exists to prevent: call A, then call B
    before touching A's result - A's result must still be A's, not
    overwritten by B (the old global-mutating _set_festival_data would have
    failed this if two requests interleaved)."""
    result_a = webapp_server._derive_session_data(_BEERS_A, _SESSIONS_A)
    result_b = webapp_server._derive_session_data(_BEERS_B, _SESSIONS_B)
    # Re-read result_a's fields now that result_b has been computed.
    beer_sessions_a, _, session_order_a, _ = result_a
    assert session_order_a == ["yellow"]
    assert beer_sessions_a == {"1": ["yellow"]}
