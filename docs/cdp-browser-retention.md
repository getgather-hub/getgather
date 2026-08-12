# CDP browser retention leak — before/after

Measured on `tap-connect-mock-flyfleet` (Fly org `remote-browsers-dev`, machine
`819525a9015e38`, `shared-cpu-2x:2048MB`, sjc). Sidecar is getgather under uvicorn on
127.0.0.1:23456. Logfire project `getgather`, environment `mock-flyfleet`.

Load is a test suite on a 5-minute cadence driving ~12 browsers/hour. Both arms of the
comparison run the same suite, so the span rates below double as proof the load was
identical and the RSS numbers are like-for-like.

## Symptom

Sidecar RSS grew ~140 MiB/h, perfectly linear, no flattening. Never OOM-killed — it
thrashed the VM until `/health` timed out. At 1080 MiB RSS: 44 socket fds, 10 threads,
zero sockets in CLOSE_WAIT, zero swap, `Private_Dirty` 1060 MiB of 1080 MiB. py-spy showed
the event loop idle and every thread parked. So: dirty anonymous Python heap, *retained*
rather than in-flight, and not an fd or socket leak.

## Root cause

`_create_browser_from_cdp_websocket` took process-lifetime ownership of every browser it
attached to, via two independent strong references:

- `util.get_registered_instances().add(instance)` — `zendriver.core.util.__registered__instances__`
  is a module-global `Set[Browser]`. In the installed zendriver it is written at
  `util.py:24` and read only by the accessor at `util.py:133`; **nothing in zendriver ever
  iterates it**, and getgather was its only writer. Pure retention with no consumer.
- `asyncio_atexit.register(browser_atexit)` — the closure captures `instance`, and the
  registry lives for the lifetime of the event loop.

Neither is removed by `terminate_remote_browser`, which only calls the ChromeFleet DELETE
API. On the flyfleet flow `terminate_remote_browser` is not reached at all.

The multiplier is that `get_remote_browser` has no identity map: it does a ChromeFleet
existence check and then unconditionally builds a **new** `zd.Browser` + `Connection` +
handler set for a browser_id that already has one. `dpage.py` calls it once per MCP tool
call (`:692`, `:711`, `:766`, `:778`, `:803`), so one remote browser accumulates one local
wrapper per tool call.

Per 5-minute test cycle, 36 instances for a single browser_id:

| tool call | per cycle |
| --- | --- |
| `get_purchase_history_with_details` | 21 |
| `get_watch_history_with_pagination` | 8 |
| `get_watchlist_with_pagination` | 2 |
| `get_browsing_history` | 1 |
| `get_prime_library` | 1 |
| `get_browser_ip_address` | 1 |
| `check_signin` | 1 |
| initial `create_remote_browser` | 1 |
| **total** | **36** |

The `amazon_*`-prefixed spans are alias wrappers around the same calls — same counts, same
trace — and must not be double-counted.

This also explains the two-tier socket rate: 432/h browser-level sockets (one per
`get_remote_browser`, redundant) plus ~444/h tab-level sockets (one per `get_new_page`,
legitimate) = the 876/h measured header-attach rate.

### Why PR #1439 did not fix it

`#1439` ("always close client_ws in CDP websocket proxy relay", `dce078d`) is present in
the measured build — verified with `git merge-base --is-ancestor dce078d HEAD`. It closes
`client_ws` in `router.py`'s `websocket_proxy`, which is the sidecar-as-*server* relay. The
flyfleet tenant never exercises that path: the sidecar is the CDP *client*, dialling out to
`flyfleet-dev.flycast`. Growth was unchanged with it deployed.

## Instrumentation added

Teardown logging, so connects and closes can be paired 1:1. `cdp websocket close {browser_id}`
emits from a `finally` on all three relay paths — `router.py`, `browserbase_browsers.py`,
and `browser.py`'s `terminate_remote_browser` — carrying `browser_id`, `outcome` and
`lifetime_ms`. The log sits in an inner `finally` so it survives a `close()` that raises,
and `CancelledError` is caught and re-raised so cancellation is tagged rather than counted
as a clean exit.

`registered_instances=len(util.get_registered_instances())` is sampled on the connect span
and at terminate. It is an O(1) `len()` on a set, cheap enough for 432 calls/hour, and it
measures retention directly — which answered the question without a gc census or
`MEMORY_XRAY` (whose tracemalloc mode cost 40% CPU under load and corrupted the measurement
it existed to produce).

## BEFORE — control

Build: merge of `origin/main` into `feat/tap-connect-sidecar` (includes #1439) plus teardown
instrumentation. Deployed 2026-08-12 22:06 UTC. Measurement window 22:08–22:45 UTC.

RSS from `/proc/<pid>/stat` field 24 × 4096. PID re-discovered each sample; elapsed time
measured, never assumed.

| sample (UTC) | RSS MiB | utime | stime | threads |
| --- | --- | --- | --- | --- |
| 22:08:52 | 142.5 | 648 | 111 | 10 |
| 22:14:00 | 176.9 | 2695 | 167 | 10 |
| 22:21:02 | *probe timeout* | | | |
| 22:26:11 | 203.8 | 7256 | 299 | 10 |
| 22:31:19 | 213.0 | 9301 | 346 | 10 |
| 22:36:28 | 230.6 | 11578 | 414 | 10 |
| 22:41:36 | 243.8 | 13695 | 459 | 10 |

**Growth: 176.9 → 243.8 MiB over a measured 27.6 min = 145.4 MiB/h.**

The first interval (142.5 → 176.9, 402 MiB/h) is boot warmup — uvicorn mounting ~40 MCP
bundles — and is excluded. Individual 5-minute intervals scatter 107–205 MiB/h from
allocator and GC granularity, so only the multi-sample aggregate is meaningful.

CPU: 133.9 CPU-s over 1964 s = 6.8% of one core. Threads flat at 10. Neither is the problem.

Span rates, 5-minute buckets:

| bucket (UTC) | connects | closes | hdrs | browsers | reg_min | reg_max |
| --- | --- | --- | --- | --- | --- | --- |
| 22:10 | 36 | 0 | 72 | 1 | 0 | 35 |
| 22:15 | 36 | 0 | 74 | 1 | 36 | 71 |
| 22:20 | 36 | 0 | 74 | 1 | 72 | 107 |
| 22:25 | 36 | 0 | 74 | 1 | 108 | 143 |
| 22:30 | 36 | 0 | 74 | 1 | 144 | 179 |
| 22:35 | 36 | 0 | 74 | 1 | 180 | 215 |
| 22:40 | 36 | 0 | 74 | 1 | 216 | 251 |

`registered_instances` runs 0→35, 36→71, 72→107 … with no gap and no repeat at the bucket
boundaries: a pure counter, +35 every bucket, **never a single decrement**. Growth is flat
rather than accelerating, which matches the linear RSS curve and rules out anything
quadratic. `browsers=1` per bucket confirms all 36 reconnects hit one browser_id.

Derived: 145.4 MiB/h ÷ 432 connects/h = **0.337 MiB retained per connect**, matching the
0.32 MiB/connect measured over the original 6.7-hour window.

| metric | original 6.7h baseline | control |
| --- | --- | --- |
| RSS growth | ~140 MiB/h | 145.4 MiB/h |
| cdp websocket connect | 432/h | 432/h |
| CDP websocket headers attached | 888/h | 876/h |
| browsers started | 12/h | 12/h |
| close/teardown spans | 0 | 0 |
| retained per connect | ~0.32 MiB | 0.337 MiB |
| threads | 10 | 10 |
| sidecar CPU | ~7% of one core | 6.8% |

## AFTER — fix A

<!-- filled in after the fix has run ≥90 min under the same 5-minute test cadence -->

## Scope

Fix A stops the retention only. It deliberately does **not** add an identity map to
`get_remote_browser` (fix B), which would cut instance creation ~36× and remove 420
redundant browser-level handshakes per hour. B is the better fix but changes lifetime
semantics — shared `connection.handlers` and `targets` under concurrency, staleness
invalidation, tab cleanup — and is kept separate.

Order matters for measurement: B alone would leave one un-discarded instance per browser,
~4 MiB/h at 12 browsers/h, which is below the 107–205 MiB/h noise floor of a 5-minute
sample. Shipping B first would look like a fix while leaving a process-global set growing
forever. A is measured by RSS slope; B is measured by span rates.
