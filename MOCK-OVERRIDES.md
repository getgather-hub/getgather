# The amazon-mock overrides, and what this branch leaves behind

`feat/tap-connect-sidecar` carries **the amazon-mock repointing and nothing
else**, extracted from the `mock` branch.

It is the remotebrowser half of a pair. The other half is the branch of the same
name in **corelens-engineering/demos**, which carries the sidecar deployment —
`Dockerfile.sidecar`, `start-sidecar.sh`, `sync-remotebrowser.sh` and three Fly
configs for `tap-connect-mock-{flyfleet,browserbase,daytona}`. That repo's
`sync-remotebrowser.sh` vendors `getgather` out of *this* checkout into its build
context, so **whichever branch this repo is on is what gets deployed.** The two
branches are meaningfully deployable only together; see `apps/tap-connect/SIDECAR.md`
and `SIDECAR-GAPS.md` over there.

> **Do not merge this branch.** It repoints Amazon at a mock fixture. On `main`
> that would send every production caller at `amazon-mock.dataportrait.app`.
> `main` merges *in* to pick up upstream changes, never the reverse.

Extracted from `mock` at `869e5a9`, against `main` at `181421c`.

## What the repointing is

65 files, every one of them a host substitution:

| Files | Change |
| --- | --- |
| `getgather/mcp/amazon.py` | `AMAZON_US.domain` and its five absolute `*_url` fields → `amazon-mock.dataportrait.app` |
| 64 × `getgather/mcp/patterns/*.html` | `rb-domain="amazon"` → `rb-domain="amazon-mock.dataportrait.app"` |

Two changes in `amazon.py` are not pure substitution and are worth a look:

- **`base_url` no longer hard-codes `www.`.** It was `f"https://www.{self.domain}"`;
  `domain` is now the full host so a mock host with no `www.` works unchanged.
  `AMAZON_CA` compensates by carrying `www.amazon.ca` as its domain, so its
  behaviour is byte-identical to before.
- **The watch-history `Referer` now interpolates `{country.watch_history_url}`**
  instead of a literal `https://www.amazon.com/...`. That is a real fix
  independent of the mock: `_get_watch_history_with_pagination` is
  country-parameterised, so an `AMAZON_CA` call was sending a `.com` referer.

That second one is the only thing on this branch that would be worth landing on
`main` on its own merits.

## What was left on `mock`

One commit, `869e5a9` — five files of leak fixes for the loopback CDP path.
Excluded because they are product changes to the browser lifecycle, not mock
configuration, and they belong in their own PR with their own review.

**They matter operationally, though.** Without them, a sidecar deployed from this
branch leaks roughly **300 sockets and ~284 MB per run** on the `browserbase` and
`daytona` backends, reaching ~600 MB on a 1 GB VM. Past that, memory pressure
kills `tailscaled`, the tailnet database goes unreachable, `/health` 500s, Fly
deroutes the machine, and every test against it fails while the machine
simultaneously goes dark in telemetry. Deployments that set `CHROMEFLEET_URL`
(the flyfleet tenant) do not take the leaking path and are unaffected.

| File | Change | Status when the notes stop |
| --- | --- | --- |
| `browsers/settings.py` | `BROWSERBASE_SESSION_TIMEOUT: int = 900` | shipped 08-08. Sessions were dying at the 300s project default mid-sync |
| `browsers/browserbase_browsers.py` | create body sends `timeout` alongside `keepAlive` | shipped 08-08 |
| `browser.py` | `_local_instances` + `close_local_browsers`; one process-level `asyncio_atexit` hook replacing the per-browser closures; explicit per-target connection close | shipped 08-09 / 08-10 |
| `browser.py` | `_reusable_instance` in `get_remote_browser`; `reap_idle_browsers` on `BROWSER_LOCAL_IDLE_TTL_SECONDS` (1200s) | shipped 08-09, **partial** — `create_remote_browser` calls the same constructor and was missed, so it still mints ~73 instances per browser |
| `browsers/router.py` | `websocket_proxy` `close_timeout` 7200 → 10; a `finally` closing the **inbound** client socket | shipped 08-10. This is the one that stopped the accumulation |
| `main.py` | reaper hooked into the existing `timer_loop` in `lifespan` | shipped 08-09 |

After all six, sockets returned to baseline across a completed sync (peak
970 → 799 → 256; 600 → 26 idle, RSS 614 → 275 MB). Two known remainders: the
over-creation above, and `HTTP 410`s that still occur but now fail fast instead of
blocking 120s each.

The evidence for all of this is in the demos repo on `mock`, at
`apps/tap-connect/DEBUG-mock-tests.md` — it is not on that repo's
`feat/tap-connect-sidecar` either.

## Keeping it alive

Same rule as `mock`. Merge `main` in, verify the override survived, then re-vendor
on the demos side:

```sh
git checkout feat/tap-connect-sidecar && git fetch origin && git merge origin/main
rg -n 'amazon-mock\.dataportrait\.app' getgather/mcp/amazon.py   # must print
```

If that prints nothing the merge took `main`'s side. The post-deploy check in the
demos repo's `SIDECAR.md` catches the same mistake from the other end: `amazon_us
domain` must read `amazon-mock.dataportrait.app` on all three apps.
