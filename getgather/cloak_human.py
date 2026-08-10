"""CloakBrowser humanize algorithms over zendriver CDP (Option B).

Uses ``cloakbrowser.human`` move/click/type helpers with thin zendriver adapters
so distillation can keep zendriver while getting Bézier mouse paths and human typing.
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from loguru import logger
from zendriver import cdp
from zendriver.core.keys import KeyEvents, KeyModifiers, KeyPressEvent, SpecialKeys

HumanPreset = Literal["default", "careful"]

_CLOAK_CURSOR_ATTR = "_cloak_human_cursor"


class _CursorState:
    __slots__ = ("x", "y", "initialized")

    def __init__(self) -> None:
        self.x = 0.0
        self.y = 0.0
        self.initialized = False


class CloakHumanizeUnavailable(RuntimeError):
    """Raised when humanize is requested but the cloakbrowser package is missing."""


def _require_cloak_human() -> Any:
    try:
        import cloakbrowser.human.config as human_config
        import cloakbrowser.human.keyboard_async as keyboard_async
        import cloakbrowser.human.mouse as mouse_sync
        import cloakbrowser.human.mouse_async as mouse_async
    except ImportError as exc:
        raise CloakHumanizeUnavailable(
            "cloakbrowser is required for humanize (install with: uv sync --extra daytona)"
        ) from exc
    return human_config, keyboard_async, mouse_sync, mouse_async


def get_human_config(preset: HumanPreset = "default") -> Any:
    human_config, _, _, _ = _require_cloak_human()
    return human_config.resolve_config(preset)


def position_to_box(position: Any) -> dict[str, float]:
    return {
        "x": float(position.x),
        "y": float(position.y),
        "width": float(position.width),
        "height": float(position.height),
    }


def is_input_tag(tag: str) -> bool:
    tag_lower = tag.lower()
    return tag_lower in ("input", "textarea", "select") or tag_lower == "textarea"


def _cursor_for_tab(tab: Any, cfg: Any) -> _CursorState:
    # zendriver Tab is not hashable — store cursor state on the tab instance.
    cursor = getattr(tab, _CLOAK_CURSOR_ATTR, None)
    if cursor is None:
        cursor = _CursorState()
        setattr(tab, _CLOAK_CURSOR_ATTR, cursor)
    if not cursor.initialized:
        human_config, _, _, _ = _require_cloak_human()
        cursor.x = human_config.rand_range(cfg.initial_cursor_x)
        cursor.y = human_config.rand_range(cfg.initial_cursor_y)
        cursor.initialized = True
    return cursor


_PLAYWRIGHT_SPECIAL: dict[str, SpecialKeys] = {
    "Shift": SpecialKeys.SHIFT,
    "Backspace": SpecialKeys.BACKSPACE,
    "Control": SpecialKeys.CTRL,
    "Meta": SpecialKeys.META,
    "Alt": SpecialKeys.ALT,
    "Enter": SpecialKeys.ENTER,
    "Tab": SpecialKeys.TAB,
    "Delete": SpecialKeys.DELETE,
    "ArrowLeft": SpecialKeys.ARROW_LEFT,
    "ArrowUp": SpecialKeys.ARROW_UP,
    "ArrowRight": SpecialKeys.ARROW_RIGHT,
    "ArrowDown": SpecialKeys.ARROW_DOWN,
}


async def _send_key_payload(tab: Any, payload: KeyEvents.Payload) -> None:
    await tab.send(
        cdp.input_.dispatch_key_event(
            type_=payload["type_"],
            modifiers=payload.get("modifiers") or 0,
            text=payload.get("text"),
            key=payload.get("key"),
            code=payload.get("code"),
            windows_virtual_key_code=payload.get("windows_virtual_key_code"),
            native_virtual_key_code=payload.get("native_virtual_key_code"),
        )
    )


async def _press_key(tab: Any, key: str, modifiers: KeyModifiers = KeyModifiers.Default) -> None:
    if key in _PLAYWRIGHT_SPECIAL:
        events = KeyEvents(_PLAYWRIGHT_SPECIAL[key]).to_cdp_events(KeyPressEvent.DOWN_AND_UP)
    elif len(key) == 1:
        events = KeyEvents(key, modifiers).to_cdp_events(KeyPressEvent.DOWN_AND_UP)
    else:
        raise ValueError(f"Unsupported key for humanize keyboard: {key!r}")
    for payload in events:
        await _send_key_payload(tab, payload)


class ZendriverCdpSession:
    """Minimal CDP session shape for cloakbrowser shift-symbol typing."""

    def __init__(self, tab: Any) -> None:
        self._tab = tab

    async def send(self, method: str, params: dict[str, Any]) -> None:
        if method != "Input.dispatchKeyEvent":
            logger.warning(f"ZendriverCdpSession ignoring unsupported method {method!r}")
            return
        await self._tab.send(
            cdp.input_.dispatch_key_event(
                type_=str(params["type"]),
                modifiers=params.get("modifiers"),
                key=params.get("key"),
                code=params.get("code"),
                text=params.get("text"),
                unmodified_text=params.get("unmodifiedText"),
                windows_virtual_key_code=params.get("windowsVirtualKeyCode"),
                native_virtual_key_code=params.get("nativeVirtualKeyCode"),
            )
        )


class ZendriverRawMouse:
    def __init__(self, tab: Any) -> None:
        self._tab = tab
        self._x = 0.0
        self._y = 0.0

    async def move(self, x: float, y: float) -> None:
        self._x = float(x)
        self._y = float(y)
        await self._tab.send(
            cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=self._x, y=self._y)
        )

    async def down(self, click_count: int = 1) -> None:
        await self._tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mousePressed",
                x=self._x,
                y=self._y,
                button=cdp.input_.MouseButton("left"),
                click_count=click_count,
            )
        )

    async def up(self, click_count: int = 1) -> None:
        await self._tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mouseReleased",
                x=self._x,
                y=self._y,
                button=cdp.input_.MouseButton("left"),
                click_count=click_count,
            )
        )

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self._tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mouseWheel",
                x=self._x,
                y=self._y,
                delta_x=float(delta_x),
                delta_y=float(delta_y),
            )
        )


def _key_event_payload(key: str, event_type: KeyPressEvent) -> KeyEvents.Payload:
    if key in _PLAYWRIGHT_SPECIAL:
        key_event = KeyEvents(_PLAYWRIGHT_SPECIAL[key])
    elif len(key) == 1:
        key_event = KeyEvents(key)
    else:
        raise ValueError(f"Unsupported key: {key!r}")
    return key_event._to_basic_event(event_type)  # pyright: ignore[reportPrivateUsage]


class ZendriverRawKeyboard:
    def __init__(self, tab: Any) -> None:
        self._tab = tab

    async def down(self, key: str) -> None:
        await _send_key_payload(self._tab, _key_event_payload(key, KeyPressEvent.KEY_DOWN))

    async def up(self, key: str) -> None:
        await _send_key_payload(self._tab, _key_event_payload(key, KeyPressEvent.KEY_UP))

    async def type(self, text: str) -> None:
        for ch in text:
            await _press_key(self._tab, ch)

    async def insert_text(self, text: str) -> None:
        await self._tab.send(cdp.input_.insert_text(text))


def reset_cursor_for_tab(tab: Any) -> None:
    if hasattr(tab, _CLOAK_CURSOR_ATTR):
        delattr(tab, _CLOAK_CURSOR_ATTR)


async def human_pre_action_idle(
    tab: Any, duration_ms: int, preset: HumanPreset = "default"
) -> None:
    """Drift the mouse briefly before the first interaction (Akamai warmup)."""
    if duration_ms <= 0:
        return
    human_config, _, _, mouse_async = _require_cloak_human()
    cfg = human_config.resolve_config(preset)
    cursor = _cursor_for_tab(tab, cfg)
    raw_mouse = ZendriverRawMouse(tab)
    await mouse_async.async_human_idle(raw_mouse, duration_ms / 1000, cursor.x, cursor.y, cfg)


async def human_click_element(
    element: Any,
    *,
    tag: str,
    tab: Any,
    preset: HumanPreset = "default",
) -> None:
    """Humanized click on a zendriver Element (Bézier move + realistic hold)."""
    human_config, _, mouse_sync, mouse_async = _require_cloak_human()
    cfg = human_config.resolve_config(preset)

    await element.scroll_into_view()
    position = await element.get_position()
    if position is None:
        logger.warning("human_click: no position for element, skipping")
        return

    box = position_to_box(position)
    is_input = is_input_tag(tag)
    target = mouse_sync.click_target(box, is_input, cfg)

    cursor = _cursor_for_tab(tab, cfg)
    raw_mouse = ZendriverRawMouse(tab)
    await mouse_async.async_human_move(raw_mouse, cursor.x, cursor.y, target.x, target.y, cfg)
    cursor.x = target.x
    cursor.y = target.y
    await mouse_async.async_human_click(raw_mouse, is_input, cfg)


async def human_type_into_element(
    element: Any,
    *,
    tag: str,
    tab: Any,
    text: str,
    preset: HumanPreset = "default",
) -> None:
    """Humanized fill: focus click, select-all clear, then cloakbrowser typing."""
    human_config, keyboard_async, _, _ = _require_cloak_human()
    cfg = human_config.resolve_config(preset)

    await human_click_element(element, tag=tag, tab=tab, preset=preset)

    await human_config.async_sleep_ms(human_config.rand(100, 250))

    select_mod = KeyModifiers.Meta if sys.platform == "darwin" else KeyModifiers.Ctrl
    await _press_key(tab, "a", select_mod)
    await human_config.async_sleep_ms(human_config.rand(30, 80))
    await _press_key(tab, "Backspace")
    await human_config.async_sleep_ms(human_config.rand(50, 150))

    raw_keyboard = ZendriverRawKeyboard(tab)
    cdp_session = ZendriverCdpSession(tab)
    await keyboard_async.async_human_type(None, raw_keyboard, text, cfg, cdp_session=cdp_session)
