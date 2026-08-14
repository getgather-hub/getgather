from unittest.mock import AsyncMock, patch

import pytest

from getgather.browsers.residential_proxy import (
    GeoLocation,
    MassiveProxyConfig,
    OxylabsProxyConfig,
    get_proxy_config,
    get_proxy_type_for_target_domain,
    should_skip_geolocation,
)
from getgather.browsers.settings import BrowserSettings

# --- should_skip_geolocation (Go net.IP equivalent) ---


@pytest.mark.parametrize(
    "ip",
    [
        "",
        "localhost",
        "unknown",
        "not-an-ip",
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.1.1",  # link-local
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "fdaa:40:8a24:a7b:2af:795b:588f:2",  # Fly 6PN / ULA
        "fe80::1",  # link-local unicast
        "::",  # unspecified
        "ff02::1",  # multicast
    ],
)
def test_should_skip_geolocation_non_global(ip: str) -> None:
    assert should_skip_geolocation(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"])
def test_should_skip_geolocation_public(ip: str) -> None:
    assert should_skip_geolocation(ip) is False


def _settings(**kwargs: object) -> BrowserSettings:
    return BrowserSettings(**kwargs)  # type: ignore[arg-type]


def _us_location() -> GeoLocation:
    return GeoLocation(country="US", subdivision="CA", city="San Francisco", postal_code="94102")


# --- GeoLocation validator ---


def test_geolocation_us_preserves_subdivision_and_postal() -> None:
    loc = GeoLocation(country="US", subdivision="CA", postal_code="94102")
    assert loc.country == "us"
    assert loc.subdivision == "ca"
    assert loc.postal_code == "94102"


def test_geolocation_non_us_drops_subdivision_and_postal() -> None:
    loc = GeoLocation(country="DE", subdivision="BY", postal_code="80331")
    assert loc.country == "de"
    assert loc.subdivision == ""
    assert loc.postal_code is None


def test_geolocation_non_us_does_not_raise() -> None:
    # Previously raised ValueError; now silently drops fields.
    loc = GeoLocation(country="GB", subdivision="ENG", postal_code="SW1A1AA")
    assert loc.country == "gb"


def test_geolocation_invalid_country_raises() -> None:
    with pytest.raises(ValueError):
        GeoLocation(country="XYZ")


def test_geolocation_city_compacted() -> None:
    loc = GeoLocation(country="US", city="New York")
    assert loc.city_compacted == "newyork"


# --- get_proxy_type_for_target_domain ---


def test_get_proxy_type_oxylabs() -> None:
    assert get_proxy_type_for_target_domain("amazon.com") == "oxylabs"


def test_get_proxy_type_massive() -> None:
    assert get_proxy_type_for_target_domain("google.com") == "massive"
    assert get_proxy_type_for_target_domain("youtube.com") == "massive"
    assert get_proxy_type_for_target_domain("doordash.com") == "massive"


def test_get_proxy_type_subdomain_matches() -> None:
    assert get_proxy_type_for_target_domain("www.amazon.com") == "oxylabs"
    assert get_proxy_type_for_target_domain("music.youtube.com") == "massive"


def test_get_proxy_type_no_match_returns_none() -> None:
    assert get_proxy_type_for_target_domain(None) is None
    assert get_proxy_type_for_target_domain("example.com") is None
    assert get_proxy_type_for_target_domain("www.example.com") is None


# --- OxylabsProxyConfig / MassiveProxyConfig URL format ---


def test_oxylabs_proxy_url_format() -> None:
    loc = GeoLocation(country="US")
    config = OxylabsProxyConfig(loc, "user", "pass")
    url = config.get_proxy_url("sess1")
    assert url.startswith("http://customer-user-cc-US-sessid-sess1-sesstime-1440:")
    assert "oxylabs.io" in url


def test_massive_proxy_url_country_only() -> None:
    loc = GeoLocation(country="DE", subdivision="BY", postal_code="80331")
    config = MassiveProxyConfig(loc, "user", "pass")
    url = config.get_proxy_url("sess1")
    assert "country-de" in url
    assert "subdivision" not in url
    assert "zipcode" not in url
    assert "joinmassive.com" in url


def test_massive_proxy_url_us_country_only() -> None:
    # Even for US, country-only since new format drops subdivision/zipcode.
    loc = GeoLocation(country="US", subdivision="CA", postal_code="94102")
    config = MassiveProxyConfig(loc, "user", "pass")
    url = config.get_proxy_url("sess1")
    assert "country-us" in url
    assert "subdivision" not in url
    assert "zipcode" not in url


# --- get_proxy_config ---


@pytest.mark.asyncio
async def test_get_proxy_config_no_ip_returns_none() -> None:
    s = _settings(
        OXYLABS_USERNAME="u",
        OXYLABS_PASSWORD="p",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
    )
    result = await get_proxy_config(None, None, s)
    assert result is None


@pytest.mark.asyncio
async def test_get_proxy_config_no_maxmind_returns_none() -> None:
    # Explicitly zero-out MaxMind fields so env credentials don't leak in.
    s = _settings(
        OXYLABS_USERNAME="u",
        OXYLABS_PASSWORD="p",
        MAXMIND_ACCOUNT_ID=0,
        MAXMIND_LICENSE_KEY="",
    )
    result = await get_proxy_config("1.2.3.4", None, s)
    assert result is None


@pytest.mark.asyncio
async def test_get_proxy_config_no_location_returns_none() -> None:
    s = _settings(
        OXYLABS_USERNAME="u",
        OXYLABS_PASSWORD="p",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
    )
    with patch(
        "getgather.browsers.residential_proxy.get_location", new=AsyncMock(return_value=None)
    ):
        result = await get_proxy_config("1.2.3.4", None, s)
    assert result is None


@pytest.mark.asyncio
async def test_get_proxy_config_domain_route_oxylabs() -> None:
    s = _settings(
        OXYLABS_USERNAME="oxu",
        OXYLABS_PASSWORD="oxp",
        MASSIVE_PROXY_USERNAME="mu",
        MASSIVE_PROXY_PASSWORD="mp",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
    )
    loc = _us_location()
    with patch(
        "getgather.browsers.residential_proxy.get_location", new=AsyncMock(return_value=loc)
    ):
        result = await get_proxy_config("1.2.3.4", "amazon.com", s)
    assert result is not None
    assert result.type_ == "oxylabs"
    assert "oxylabs.io" in result.get_proxy_url("sess")


@pytest.mark.asyncio
async def test_get_proxy_config_domain_route_massive() -> None:
    s = _settings(
        OXYLABS_USERNAME="oxu",
        OXYLABS_PASSWORD="oxp",
        MASSIVE_PROXY_USERNAME="mu",
        MASSIVE_PROXY_PASSWORD="mp",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
    )
    loc = _us_location()
    with patch(
        "getgather.browsers.residential_proxy.get_location", new=AsyncMock(return_value=loc)
    ):
        result = await get_proxy_config("1.2.3.4", "youtube.com", s)
    assert result is not None
    assert result.type_ == "massive"
    assert "joinmassive.com" in result.get_proxy_url("sess")


@pytest.mark.asyncio
async def test_get_proxy_config_default_fallback_when_no_domain_match() -> None:
    s = _settings(
        OXYLABS_USERNAME="oxu",
        OXYLABS_PASSWORD="oxp",
        MASSIVE_PROXY_USERNAME="mu",
        MASSIVE_PROXY_PASSWORD="mp",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
        DEFAULT_PROXY_TYPE="massive",
    )
    loc = _us_location()
    with patch(
        "getgather.browsers.residential_proxy.get_location", new=AsyncMock(return_value=loc)
    ):
        result = await get_proxy_config("1.2.3.4", "example.com", s)
    assert result is not None
    assert result.type_ == "massive"


@pytest.mark.asyncio
async def test_get_proxy_config_non_us_origin_country_only_url() -> None:
    s = _settings(
        MASSIVE_PROXY_USERNAME="mu",
        MASSIVE_PROXY_PASSWORD="mp",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
        DEFAULT_PROXY_TYPE="massive",
    )
    # Simulate MaxMind returning non-US location with subdivision/postal.
    loc = GeoLocation(country="DE", subdivision="BY", postal_code="80331")
    with patch(
        "getgather.browsers.residential_proxy.get_location", new=AsyncMock(return_value=loc)
    ):
        result = await get_proxy_config("4.5.6.7", None, s)
    assert result is not None
    url = result.get_proxy_url("sess")
    assert "country-de" in url
    assert "subdivision" not in url
    assert "zipcode" not in url


@pytest.mark.asyncio
async def test_get_proxy_config_no_provider_returns_none() -> None:
    # Explicitly zero-out proxy credentials so env credentials don't leak in.
    s = _settings(
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
        MASSIVE_PROXY_USERNAME="",
        MASSIVE_PROXY_PASSWORD="",
        OXYLABS_USERNAME="",
        OXYLABS_PASSWORD="",
    )
    loc = _us_location()
    with patch(
        "getgather.browsers.residential_proxy.get_location", new=AsyncMock(return_value=loc)
    ):
        result = await get_proxy_config("1.2.3.4", None, s)
    assert result is None


# --- get_proxy_config: x-country ---


@pytest.mark.asyncio
async def test_get_proxy_config_country_wins_without_maxmind_call() -> None:
    # x-country present -> no MaxMind call at all, even with an origin_ip set.
    s = _settings(
        MASSIVE_PROXY_USERNAME="mu",
        MASSIVE_PROXY_PASSWORD="mp",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
        DEFAULT_PROXY_TYPE="massive",
    )
    get_location_mock = AsyncMock(side_effect=AssertionError("MaxMind must not be called"))
    with patch("getgather.browsers.residential_proxy.get_location", new=get_location_mock):
        result = await get_proxy_config("1.2.3.4", None, s, "de")
    assert result is not None
    assert "country-de" in result.get_proxy_url("sess")
    get_location_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_proxy_config_country_without_origin_ip() -> None:
    # Country alone is enough to build the proxy; MaxMind is not required.
    s = _settings(
        MASSIVE_PROXY_USERNAME="mu",
        MASSIVE_PROXY_PASSWORD="mp",
        DEFAULT_PROXY_TYPE="massive",
    )
    result = await get_proxy_config(None, None, s, "US")
    assert result is not None
    assert "country-us" in result.get_proxy_url("sess")


@pytest.mark.asyncio
async def test_get_proxy_config_invalid_country_skips_proxy_not_maxmind() -> None:
    # An invalid x-country is treated as absent/miss: skip proxy, do not fall back to MaxMind.
    s = _settings(
        MASSIVE_PROXY_USERNAME="mu",
        MASSIVE_PROXY_PASSWORD="mp",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
    )
    get_location_mock = AsyncMock(side_effect=AssertionError("MaxMind must not be called"))
    with patch("getgather.browsers.residential_proxy.get_location", new=get_location_mock):
        result = await get_proxy_config("1.2.3.4", None, s, "xyz")
    assert result is None
    get_location_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_proxy_config_empty_country_falls_back_to_maxmind() -> None:
    # An empty string country is falsy -> same as absent, so MaxMind is used as usual.
    s = _settings(
        MASSIVE_PROXY_USERNAME="mu",
        MASSIVE_PROXY_PASSWORD="mp",
        MAXMIND_ACCOUNT_ID=1,
        MAXMIND_LICENSE_KEY="k",
        DEFAULT_PROXY_TYPE="massive",
    )
    loc = _us_location()
    with patch(
        "getgather.browsers.residential_proxy.get_location", new=AsyncMock(return_value=loc)
    ):
        result = await get_proxy_config("1.2.3.4", None, s, "")
    assert result is not None
    assert "country-us" in result.get_proxy_url("sess")
