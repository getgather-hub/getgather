"""Log-only probe attributing what keeps Daytona sandboxes out of auto-stop.

Daytona stops a sandbox after `AUTO_STOP_MINUTES` with no sandbox activity, and only a stopped
sandbox is free. Billing data shows sandboxes running 20-46h continuously (billed hours == lifetime
hours, ~0 stopped time), so something resets that idle clock every few minutes. This probe records
who: every call this process makes into a sandbox, plus every long-lived connection it holds open
through the sandbox's preview proxy (a CDP relay or a noVNC stream is activity even when no API call
is made).

It only reads process-local state and emits logs — no Daytona API calls of its own, no change to any
lifecycle decision. Enabled by DaytonaBackend on construction, so other backends emit nothing.

Logfire: filter `attributes->>'probe' = 'daytona'`; `daytona.probe.report` carries the verdict per
browser, `daytona.probe.touch` / `daytona.probe.conn` the raw events.
"""

import sys
import time
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import count

from loguru import logger

WINDOW_SECONDS = 2 * 60 * 60  # touch history retained per browser
MAX_TOUCHES_PER_BROWSER = 500
MAX_CALLER_FRAMES = 25  # cap the stack walk; the app-level caller is always near the top
TOP_CALLERS = 3

# Frames from these files are plumbing between the probe and the app code that triggered the touch.
_TRANSPARENT_FILES = ("daytona_probe.py", "daytona_browsers.py", "contextlib.py")

# Daytona's idle window, supplied by the backend via enable() (daytona_browsers.AUTO_STOP_MINUTES).
# A sandbox whose consecutive touches never exceed it is, by definition, why it never auto-stops.
_enabled = False
_auto_stop_seconds = 15 * 60


@dataclass(frozen=True)
class _Touch:
    at: float
    op: str
    caller: str


@dataclass
class _Conn:
    browser_id: str
    kind: str
    caller: str
    opened_at: float


_touches: dict[str, deque[_Touch]] = {}
_conns: dict[int, _Conn] = {}
_conn_ids = count(1)


def enable(auto_stop_minutes: int) -> None:
    global _enabled, _auto_stop_seconds
    _auto_stop_seconds = auto_stop_minutes * 60
    if not _enabled:
        _enabled = True
        logger.info(f"Daytona probe enabled (auto_stop={auto_stop_minutes}m)")


def _caller() -> str:
    """Nearest frame outside the probe and the backend — the app code that triggered the touch."""
    try:
        frame = sys._getframe(2)  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    except ValueError:
        return "unknown"
    for _ in range(MAX_CALLER_FRAMES):
        if frame is None:
            break
        filename = frame.f_code.co_filename
        if not filename.endswith(_TRANSPARENT_FILES):
            short = filename.rsplit("/", 1)[-1]
            return f"{short}:{frame.f_lineno} {frame.f_code.co_name}"
        frame = frame.f_back
    return "unknown"


def _record(browser_id: str, op: str, caller: str, now: float) -> tuple[int, float | None]:
    history = _touches.setdefault(browser_id, deque(maxlen=MAX_TOUCHES_PER_BROWSER))
    gap = now - history[-1].at if history else None
    history.append(_Touch(at=now, op=op, caller=caller))
    return len(history), gap


def touch(browser_id: str, op: str, detail: str | None = None) -> None:
    """Record one call this process made into `browser_id`'s sandbox."""
    if not _enabled:
        return
    try:
        now = time.time()
        caller = _caller()
        seen, gap = _record(browser_id, op, caller, now)
        logger.debug(
            f"[probe] touch {op} on {browser_id} from {caller}",
            probe="daytona",
            event="daytona.probe.touch",
            browser_id=browser_id,
            op=op,
            detail=detail,
            caller=caller,
            since_prev_touch_s=round(gap, 1) if gap is not None else None,
            touches_in_window=seen,
        )
    except Exception as e:  # a probe must never break the call it observes
        logger.debug(f"[probe] touch failed: {type(e).__name__}: {e}")


@contextmanager
def connection(browser_id: str, kind: str) -> Generator[None]:
    """Track a connection held open through the sandbox's preview proxy for its whole lifetime."""
    if not _enabled:
        yield
        return
    conn_id = next(_conn_ids)
    caller = _caller()
    opened_at = time.time()
    try:
        _conns[conn_id] = _Conn(
            browser_id=browser_id, kind=kind, caller=caller, opened_at=opened_at
        )
        logger.info(
            f"[probe] {kind} open on {browser_id} from {caller}",
            probe="daytona",
            event="daytona.probe.conn",
            phase="open",
            browser_id=browser_id,
            kind=kind,
            caller=caller,
            open_connections=len(_conns),
        )
    except Exception as e:
        logger.debug(f"[probe] conn open failed: {type(e).__name__}: {e}")
    try:
        yield
    finally:
        try:
            _conns.pop(conn_id, None)
            held = time.time() - opened_at
            logger.info(
                f"[probe] {kind} closed on {browser_id} after {held:.0f}s",
                probe="daytona",
                event="daytona.probe.conn",
                phase="close",
                browser_id=browser_id,
                kind=kind,
                caller=caller,
                held_seconds=round(held, 1),
                exceeded_auto_stop=held > _auto_stop_seconds,
                open_connections=len(_conns),
            )
        except Exception as e:
            logger.debug(f"[probe] conn close failed: {type(e).__name__}: {e}")


def open_connection(browser_id: str, kind: str) -> int | None:
    """Register a connection with no scope to close it (a zendriver Browser handle nothing stops).

    Pair with `close_connection`; an unpaired call is the finding, not a leak in the probe — the
    reporter surfaces its age every tick.
    """
    if not _enabled:
        return None
    try:
        conn_id = next(_conn_ids)
        caller = _caller()
        _conns[conn_id] = _Conn(
            browser_id=browser_id, kind=kind, caller=caller, opened_at=time.time()
        )
        logger.info(
            f"[probe] {kind} open on {browser_id} from {caller}",
            probe="daytona",
            event="daytona.probe.conn",
            phase="open",
            browser_id=browser_id,
            kind=kind,
            caller=caller,
            open_connections=len(_conns),
        )
        return conn_id
    except Exception as e:
        logger.debug(f"[probe] conn open failed: {type(e).__name__}: {e}")
        return None


def close_connection(browser_id: str, kind: str) -> None:
    if not _enabled:
        return
    try:
        match = next(
            (
                cid
                for cid, conn in _conns.items()
                if conn.browser_id == browser_id and conn.kind == kind
            ),
            None,
        )
        if match is None:
            return
        conn = _conns.pop(match)
        held = time.time() - conn.opened_at
        logger.info(
            f"[probe] {kind} closed on {browser_id} after {held:.0f}s",
            probe="daytona",
            event="daytona.probe.conn",
            phase="close",
            browser_id=browser_id,
            kind=kind,
            caller=conn.caller,
            held_seconds=round(held, 1),
            exceeded_auto_stop=held > _auto_stop_seconds,
            open_connections=len(_conns),
        )
    except Exception as e:
        logger.debug(f"[probe] conn close failed: {type(e).__name__}: {e}")


def _prune(now: float) -> None:
    for browser_id in list(_touches):
        history = _touches[browser_id]
        while history and now - history[0].at > WINDOW_SECONDS:
            history.popleft()
        if not history:
            del _touches[browser_id]


def report() -> None:
    """Emit one verdict per browser touched in the retained window. Reads local state only."""
    if not _enabled:
        return
    try:
        now = time.time()
        _prune(now)
        idle_window = _auto_stop_seconds

        conns_by_browser: dict[str, list[_Conn]] = {}
        for conn in _conns.values():
            conns_by_browser.setdefault(conn.browser_id, []).append(conn)

        keepalive_count = 0
        for browser_id in set(_touches) | set(conns_by_browser):
            history = list(_touches.get(browser_id, ()))
            conns = conns_by_browser.get(browser_id, [])
            observed = now - history[0].at if history else 0.0

            gaps = [b.at - a.at for a, b in zip(history, history[1:])]
            max_gap = max(gaps) if gaps else None
            # Every gap inside the idle window means our own traffic, not the user's browser, is
            # what stops this sandbox from ever auto-stopping.
            touch_keepalive = observed > idle_window and all(g < idle_window for g in gaps)
            conn_keepalive = any(now - c.opened_at > idle_window for c in conns)
            if touch_keepalive or conn_keepalive:
                keepalive_count += 1

            by_op: dict[str, int] = {}
            by_caller: dict[str, int] = {}
            for t in history:
                by_op[t.op] = by_op.get(t.op, 0) + 1
                by_caller[t.caller] = by_caller.get(t.caller, 0) + 1
            top_callers = dict(sorted(by_caller.items(), key=lambda kv: -kv[1])[:TOP_CALLERS])

            logger.info(
                f"[probe] {browser_id}: {len(history)} touches over {observed / 60:.0f}m, "
                f"{len(conns)} open conn(s), "
                f"keepalive={touch_keepalive or conn_keepalive}",
                probe="daytona",
                event="daytona.probe.report",
                browser_id=browser_id,
                touches=len(history),
                observed_seconds=round(observed, 1),
                max_gap_seconds=round(max_gap, 1) if max_gap is not None else None,
                auto_stop_seconds=idle_window,
                touch_keepalive=touch_keepalive,
                conn_keepalive=conn_keepalive,
                keepalive=touch_keepalive or conn_keepalive,
                touches_by_op=by_op,
                top_callers=top_callers,
                open_connections=[
                    {
                        "kind": c.kind,
                        "caller": c.caller,
                        "age_seconds": round(now - c.opened_at, 1),
                    }
                    for c in conns
                ],
            )

        logger.info(
            f"[probe] {len(_touches)} browser(s) touched, "
            f"{len(_conns)} open conn(s), {keepalive_count} keeping a sandbox alive",
            probe="daytona",
            event="daytona.probe.summary",
            browsers_touched=len(_touches),
            open_connections=len(_conns),
            keepalive_browsers=keepalive_count,
        )
    except Exception as e:
        logger.warning(f"[probe] report failed: {type(e).__name__}: {e}")
