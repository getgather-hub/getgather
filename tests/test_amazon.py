from dataclasses import fields

import pytest

from getgather.mcp.amazon import AMAZON_CA, AMAZON_US, us_config

ALT = "https://amazon-test.example.com"


def test_every_absolute_url_is_rebased() -> None:
    """Guards `with_base_url` against a URL field being added and forgotten."""
    rebased = AMAZON_US.with_base_url(ALT)

    rebased_urls = 0
    for field in fields(rebased):
        value = getattr(rebased, field.name)
        if isinstance(value, str) and value.startswith("http"):
            assert value.startswith(ALT), f"{field.name} was not rebased: {value}"
            rebased_urls += 1

    assert rebased_urls == 7


def test_rebasing_preserves_path_and_query() -> None:
    rebased = AMAZON_US.with_base_url(ALT)

    assert rebased.signin_url == f"{ALT}/ax/account/manage"
    assert rebased.browsing_history_url == (
        f"{ALT}/gp/history?ref_=nav_AccountFlyout_browsinghistory"
    )
    assert rebased.watch_history_url == f"{ALT}/gp/video/settings/watch-history"
    assert rebased.watchlist_url == f"{ALT}/gp/video/mystuff/watchlist"
    assert rebased.prime_library_url == f"{ALT}/gp/video/mystuff/library"
    assert rebased.watchlist_pagination_api_url == f"{ALT}/gp/video/api/paginateCollection"
    assert rebased.watch_history_pagination_api_url == (
        f"{ALT}/gp/video/api/getWatchHistorySettingsPage"
    )


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        (ALT, ALT),
        (f"{ALT}/", ALT),
        (f"  {ALT}  ", ALT),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://localhost:3000/", "http://localhost:3000"),
    ],
)
def test_origin_is_normalized(supplied: str, expected: str) -> None:
    assert AMAZON_US.with_base_url(supplied).base_url == expected


@pytest.mark.parametrize(
    "supplied",
    [
        "",
        "   ",
        "amazon-test.example.com",
        "ftp://amazon-test.example.com",
        "javascript:alert(1)",
        "https://",
        "https://amazon-test.example.com/prefix",
        "https://amazon-test.example.com?a=1",
        "https://amazon-test.example.com#frag",
    ],
)
def test_invalid_origin_is_rejected(supplied: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        AMAZON_US.with_base_url(supplied)


def test_override_does_not_leak_into_the_shared_config() -> None:
    AMAZON_US.with_base_url(ALT)

    assert AMAZON_US.base_url == "https://www.amazon.com"
    assert AMAZON_US.watch_history_url == "https://www.amazon.com/gp/video/settings/watch-history"


def test_rebasing_ca_collapses_both_of_its_origins() -> None:
    rebased = AMAZON_CA.with_base_url(ALT)

    assert rebased.browsing_history_url.startswith(ALT)  # was amazon.ca
    assert rebased.watchlist_url == f"{ALT}/region/na/mystuff/watchlist"  # was primevideo.com


def test_omitting_the_override_reuses_the_shared_config() -> None:
    assert us_config(None) is AMAZON_US
