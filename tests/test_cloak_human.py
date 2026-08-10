from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest import MonkeyPatch

from getgather import cloak_human


class _FakeTab:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, cmd: Any) -> None:
        self.sent.append(cmd)


def _cmd_locals(cmd: Any) -> dict[str, Any]:
    frame = cmd.gi_frame
    assert frame is not None
    return dict(frame.f_locals)


@pytest.mark.asyncio
async def test_zendriver_raw_mouse_move_and_click_use_last_coordinates() -> None:
    tab = _FakeTab()
    mouse = cloak_human.ZendriverRawMouse(tab)
    await mouse.move(100, 200)
    await mouse.down()
    await mouse.up()

    assert len(tab.sent) == 3
    moved = _cmd_locals(tab.sent[0])
    pressed = _cmd_locals(tab.sent[1])
    released = _cmd_locals(tab.sent[2])
    assert moved["x"] == 100 and moved["y"] == 200
    assert pressed["x"] == 100 and pressed["y"] == 200
    assert released["x"] == 100 and released["y"] == 200


def test_position_to_box() -> None:
    pos = MagicMock()
    pos.x = 10
    pos.y = 20
    pos.width = 100
    pos.height = 30
    assert cloak_human.position_to_box(pos) == {
        "x": 10.0,
        "y": 20.0,
        "width": 100.0,
        "height": 30.0,
    }


@pytest.mark.asyncio
async def test_human_click_element_moves_before_press(monkeypatch: MonkeyPatch) -> None:
    tab = _FakeTab()
    element = MagicMock()
    element.scroll_into_view = AsyncMock()
    position = MagicMock()
    position.x = 0
    position.y = 0
    position.width = 200
    position.height = 40
    element.get_position = AsyncMock(return_value=position)

    move_calls: list[tuple[float, float, float, float]] = []

    async def fake_move(
        raw_mouse: Any, sx: float, sy: float, ex: float, ey: float, cfg: Any
    ) -> None:
        del cfg
        move_calls.append((sx, sy, ex, ey))
        await raw_mouse.move(ex, ey)

    async def fake_click(raw_mouse: Any, is_input: bool, cfg: Any) -> None:
        del is_input, cfg
        await raw_mouse.down()
        await raw_mouse.up()

    monkeypatch.setattr(
        cloak_human,
        "_require_cloak_human",
        lambda: (
            MagicMock(
                resolve_config=lambda preset: MagicMock(
                    initial_cursor_x=(1, 1),
                    initial_cursor_y=(2, 2),
                    click_input_x_range=(0.1, 0.2),
                ),
                rand_range=lambda r: r[0],
            ),
            MagicMock(),
            MagicMock(click_target=lambda box, is_input, cfg: MagicMock(x=50, y=10)),
            MagicMock(async_human_move=fake_move, async_human_click=fake_click),
        ),
    )

    await cloak_human.human_click_element(element, tag="input", tab=tab)
    assert move_calls
    pressed = [
        _cmd_locals(cmd) for cmd in tab.sent if _cmd_locals(cmd).get("type_") == "mousePressed"
    ]
    assert pressed


@pytest.mark.asyncio
async def test_zendriver_cdp_session_dispatches_shift_symbol_key_events() -> None:
    tab = _FakeTab()
    session = cloak_human.ZendriverCdpSession(tab)
    await session.send(
        "Input.dispatchKeyEvent",
        {
            "type": "keyDown",
            "modifiers": 8,
            "key": "@",
            "code": "Digit2",
            "windowsVirtualKeyCode": 50,
            "text": "@",
            "unmodifiedText": "@",
        },
    )
    assert len(tab.sent) == 1


def test_is_input_tag() -> None:
    assert cloak_human.is_input_tag("input") is True
    assert cloak_human.is_input_tag("BUTTON") is False


def test_cursor_for_tab_works_on_unhashable_tab(monkeypatch: MonkeyPatch) -> None:
    class _UnhashableTab:
        pass

    tab = _UnhashableTab()
    cfg = MagicMock(initial_cursor_x=(10, 10), initial_cursor_y=(20, 20))
    monkeypatch.setattr(
        cloak_human,
        "_require_cloak_human",
        lambda: (MagicMock(rand_range=lambda r: r[0]), MagicMock(), MagicMock(), MagicMock()),
    )
    cursor = cloak_human._cursor_for_tab(tab, cfg)  # pyright: ignore[reportPrivateUsage]
    assert cursor.initialized is True
    assert getattr(tab, cloak_human._CLOAK_CURSOR_ATTR) is cursor
