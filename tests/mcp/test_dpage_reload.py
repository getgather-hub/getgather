from bs4 import BeautifulSoup, Tag

from getgather.mcp.dpage import _pattern_reload_max


def test_pattern_reload_max_default() -> None:
    root = BeautifulSoup("<html rb-reload-before-actions></html>", "html.parser").find("html")
    assert isinstance(root, Tag)
    assert _pattern_reload_max(root) == 1


def test_pattern_reload_max_custom() -> None:
    root = BeautifulSoup(
        '<html rb-reload-before-actions rb-reload-max="3"></html>', "html.parser"
    ).find("html")
    assert isinstance(root, Tag)
    assert _pattern_reload_max(root) == 3


def test_pattern_reload_max_invalid_falls_back_to_default() -> None:
    root = BeautifulSoup(
        '<html rb-reload-before-actions rb-reload-max="nope"></html>', "html.parser"
    ).find("html")
    assert isinstance(root, Tag)
    assert _pattern_reload_max(root) == 1
