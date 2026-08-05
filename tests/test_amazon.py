from getgather.mcp.amazon import AMAZON_CA, create_amazon_us


def test_amazon_us_uses_production_origin_by_default() -> None:
    country = create_amazon_us()

    assert country.base_url == "https://www.amazon.com"
    assert country.signin_url == "https://www.amazon.com/ax/account/manage"
    assert country.watch_history_url == ("https://www.amazon.com/gp/video/settings/watch-history")
    assert country.watchlist_pagination_api_url == (
        "https://www.amazon.com/gp/video/api/paginateCollection"
    )
    assert country.watch_history_pagination_api_url == (
        "https://www.amazon.com/gp/video/api/getWatchHistorySettingsPage"
    )


def test_amazon_us_override_rewrites_every_absolute_origin() -> None:
    origin = "https://deployed-mock-amazon.example.com"
    country = create_amazon_us(f"{origin}/")

    assert country.base_url == origin
    assert country.signin_url == f"{origin}/ax/account/manage"
    assert country.watch_history_url == f"{origin}/gp/video/settings/watch-history"
    assert country.watchlist_url == f"{origin}/gp/video/mystuff/watchlist"
    assert country.prime_library_url == f"{origin}/gp/video/mystuff/library"
    assert country.browsing_history_url == (
        f"{origin}/gp/history?ref_=nav_AccountFlyout_browsinghistory"
    )
    assert country.watchlist_pagination_api_url == (f"{origin}/gp/video/api/paginateCollection")
    assert country.watch_history_pagination_api_url == (
        f"{origin}/gp/video/api/getWatchHistorySettingsPage"
    )


def test_amazon_canada_is_not_affected_by_us_override() -> None:
    assert AMAZON_CA.base_url == "https://www.amazon.ca"
    assert AMAZON_CA.watch_history_url.startswith("https://www.primevideo.com/")
