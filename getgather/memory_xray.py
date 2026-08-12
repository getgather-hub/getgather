"""[XRAY] memory: periodic process-memory telemetry for leak hunting.

The tap-connect sidecar deployment grows ~23MB/h (147MB RSS at boot, 632MB
after 21h) and is never OOM-killed — it just thrashes the VM until /health
times out — so nothing in the runtime reports the growth. This samples the
process and ships the numbers to Logfire so growth can be *attributed* rather
than guessed at.

Four independent signals, because they fail differently and each one rules a
whole class of cause in or out:

- **RSS** — is it growing at all, and how fast.
- **fds / sockets** — leaked CDP websockets. `websocket_proxy` holds sockets
  with `max_size=10MB` and `close_timeout=7200`, so a relay that never tears
  down is worth MBs each and shows here long before it shows in a type census.
- **asyncio tasks** — fire-and-forget tasks that never finish (`_cleanup_losers`
  retries for 40s per loser; `websocket_proxy`'s pump tasks).
- **gc type census + tracemalloc** — pure-Python object growth, attributed to a
  type and then to an allocation site.

Stdlib only, by design: psutil/memray would churn uv.lock and the vendored
sidecar build for what is a diagnostic.
"""

import asyncio
import gc
import os
import tracemalloc
from collections import Counter
from typing import Any

import logfire
from loguru import logger

from getgather.config import settings

_STATUS_FIELDS = ("VmRSS", "VmSize", "VmSwap", "VmHWM")


def _proc_status_kb() -> dict[str, int]:
    """Memory fields from /proc/self/status, in kB.

    /proc is Linux-only; on a macOS dev box this returns {} and the sampler
    still emits every other counter rather than dying.
    """
    try:
        with open("/proc/self/status") as f:
            lines = f.readlines()
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in lines:
        name, _, rest = line.partition(":")
        if name in _STATUS_FIELDS:
            out[name] = int(rest.split()[0])
    return out


def _fd_counts() -> tuple[int, int]:
    """(total open fds, of which sockets). Sockets are the websocket-leak signal."""
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError:
        return (0, 0)
    sockets = 0
    for entry in entries:
        try:
            if os.readlink(f"/proc/self/fd/{entry}").startswith("socket:"):
                sockets += 1
        except OSError:
            continue  # fd closed between listdir and readlink
    return (len(entries), sockets)


def _task_count() -> int:
    try:
        return len(asyncio.all_tasks())
    except RuntimeError:
        return 0


def _type_counts() -> Counter[str]:
    """Census of live objects by type name.

    gc.get_objects() walks every tracked object, so this is the expensive part
    of a sample (~100ms at ~1M objects) — hence MEMORY_XRAY_CENSUS gating it
    separately from the cheap counters.

    Reads only tracked objects, so a flat census does NOT mean a flat heap:
    CPython untracks dicts/tuples holding only atomic values, and raw bytes/str
    buffers were never tracked to begin with. A leak of 10MB websocket frames
    shows up in tracemalloc and RSS while barely moving this number. Treat it
    as "which live object graph is growing", not as a heap total.
    """
    counts: Counter[str] = Counter()
    for obj in gc.get_objects():
        counts[type(obj).__qualname__] += 1
    return counts


def _known_suspects() -> dict[str, int]:
    """Sizes of unbounded module-level collections we already know about.

    Imported lazily so this module stays importable without pulling the tracing
    stack in at import time.
    """
    from getgather.tracing import _emitted_session_root_spans  # pyright: ignore[reportPrivateUsage]

    return {"emitted_session_root_spans": len(_emitted_session_root_spans)}


def _tracemalloc_top(
    baseline: tracemalloc.Snapshot | None, top_n: int
) -> tuple[tracemalloc.Snapshot, list[dict[str, Any]]]:
    """Top allocation sites by size growth since `baseline`.

    Compared against a baseline taken at the first sample, so the result is
    "what grew while serving traffic" — startup allocations (~40 MCP bundles)
    net out to zero instead of burying the signal.
    """
    snapshot = tracemalloc.take_snapshot()
    if baseline is None:
        return snapshot, []
    top: list[dict[str, Any]] = []
    for stat in snapshot.compare_to(baseline, "lineno")[:top_n]:
        frame = stat.traceback[0]
        top.append({
            "site": f"{frame.filename}:{frame.lineno}",
            "size_diff_kb": round(stat.size_diff / 1024, 1),
            "count_diff": stat.count_diff,
            "size_kb": round(stat.size / 1024, 1),
        })
    return snapshot, top


async def memory_xray_loop(stop_event: asyncio.Event) -> None:
    """Sample every MEMORY_XRAY_INTERVAL seconds until `stop_event` is set."""
    interval = settings.MEMORY_XRAY_INTERVAL
    top_n = settings.MEMORY_XRAY_TOP_N

    rss_gauge = logfire.metric_gauge("getgather.memory.rss_mb", unit="MB")
    fd_gauge = logfire.metric_gauge("getgather.memory.open_fds")
    socket_gauge = logfire.metric_gauge("getgather.memory.open_sockets")
    task_gauge = logfire.metric_gauge("getgather.memory.asyncio_tasks")
    objects_gauge = logfire.metric_gauge("getgather.memory.gc_objects")
    traced_gauge = logfire.metric_gauge("getgather.memory.traced_mb", unit="MB")

    if settings.MEMORY_XRAY_TRACEMALLOC:
        # Started here rather than at import: we want growth *after* startup,
        # so the ~40 MCP bundles mounted before the app binds are deliberately
        # outside the trace. nframe=1 keeps the trace overhead modest — enough
        # for a file:line, not a full call path.
        tracemalloc.start(1)
        logger.info("[XRAY] memory: tracemalloc enabled (nframe=1)")

    baseline_snapshot: tracemalloc.Snapshot | None = None
    baseline_rss: int | None = None
    baseline_types: Counter[str] | None = None

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # stop_event fired — shutting down
        except asyncio.TimeoutError:
            pass

        try:
            status = _proc_status_kb()
            rss_kb = status.get("VmRSS", 0)
            open_fds, open_sockets = _fd_counts()
            tasks = _task_count()
            gc_objects = len(gc.get_objects())

            if baseline_rss is None:
                baseline_rss = rss_kb

            rss_gauge.set(round(rss_kb / 1024, 1))
            fd_gauge.set(open_fds)
            socket_gauge.set(open_sockets)
            task_gauge.set(tasks)
            objects_gauge.set(gc_objects)

            fields: dict[str, Any] = {
                "rss_mb": round(rss_kb / 1024, 1),
                "rss_growth_mb": round((rss_kb - baseline_rss) / 1024, 1),
                "vm_size_mb": round(status.get("VmSize", 0) / 1024, 1),
                "vm_hwm_mb": round(status.get("VmHWM", 0) / 1024, 1),
                "open_fds": open_fds,
                "open_sockets": open_sockets,
                "asyncio_tasks": tasks,
                "gc_objects": gc_objects,
                "gc_collections": [s["collections"] for s in gc.get_stats()],
                **_known_suspects(),
            }

            if settings.MEMORY_XRAY_CENSUS:
                types = _type_counts()
                if baseline_types is None:
                    baseline_types = types
                    fields["top_types"] = types.most_common(top_n)
                else:
                    growth = Counter({
                        name: count - baseline_types.get(name, 0) for name, count in types.items()
                    })
                    fields["top_type_growth"] = growth.most_common(top_n)

            if settings.MEMORY_XRAY_TRACEMALLOC:
                # The total across ALL sites, not just the reported top-N. This
                # is what separates the two remaining explanations for RSS: if
                # traced stays small while RSS climbs, the memory is freed by
                # Python and retained by the allocator; if traced tracks RSS,
                # it is a real object leak spread below the top-N threshold.
                traced, traced_peak = tracemalloc.get_traced_memory()
                traced_gauge.set(round(traced / 1024 / 1024, 1))
                fields["traced_mb"] = round(traced / 1024 / 1024, 1)
                fields["traced_peak_mb"] = round(traced_peak / 1024 / 1024, 1)
                fields["untraced_mb"] = round(rss_kb / 1024 - traced / 1024 / 1024, 1)

                baseline_snapshot, top = _tracemalloc_top(baseline_snapshot, top_n)
                if top:
                    fields["top_alloc_sites"] = top

            logfire.info("[XRAY] memory sample", **fields)
        except Exception as e:
            # A diagnostic must never take the process down with it.
            logger.error(f"[XRAY] memory sample failed: {type(e).__name__}: {e}")
