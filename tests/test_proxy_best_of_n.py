# tests/test_proxy_best_of_n.py
import pytest

from getgather.browsers.backend import ProxyVerificationError
from getgather.browsers.proxy_best_of_n import (
    DEFAULT_PROXY_BEST_OF_N,
    FALLBACK_PROBE_URL,
    candidate_session_ids,
    effective_proxy_best_of_n,
    parse_ttfb,
    pick_fastest_session,
    proxy_probe_url,
)


def test_effective_n_default_is_three() -> None:
    assert effective_proxy_best_of_n(None) == DEFAULT_PROXY_BEST_OF_N == 3


def test_effective_n_explicit() -> None:
    assert effective_proxy_best_of_n(1) == 1
    assert effective_proxy_best_of_n(5) == 5
    assert effective_proxy_best_of_n(0) == 1  # floor at 1


def test_probe_url_from_target_domain() -> None:
    assert proxy_probe_url("amazon.com") == "https://amazon.com/"


def test_probe_url_uses_first_comma_separated_token() -> None:
    assert proxy_probe_url(" amazon.com , cvs.com ") == "https://amazon.com/"


def test_probe_url_fallback_when_missing() -> None:
    assert proxy_probe_url(None) == FALLBACK_PROBE_URL
    assert proxy_probe_url("") == FALLBACK_PROBE_URL
    assert proxy_probe_url("   ") == FALLBACK_PROBE_URL


def test_candidate_session_ids() -> None:
    assert candidate_session_ids("b0", 3) == ["b0-p0", "b0-p1", "b0-p2"]


def test_parse_ttfb_ok() -> None:
    assert parse_ttfb("0.321") == pytest.approx(0.321)
    assert parse_ttfb(" 1.5\n") == pytest.approx(1.5)


def test_parse_ttfb_invalid() -> None:
    assert parse_ttfb("") is None
    assert parse_ttfb("curl: (28) timed out") is None


def test_pick_fastest_session_picks_lowest_ttfb() -> None:
    winner = pick_fastest_session(
        [("b0-p0", 0.40), ("b0-p1", 0.12), ("b0-p2", None)]
    )
    assert winner == "b0-p1"


def test_pick_fastest_session_all_fail() -> None:
    with pytest.raises(ProxyVerificationError, match="no proxy candidate"):
        pick_fastest_session([("b0-p0", None), ("b0-p1", None)])
