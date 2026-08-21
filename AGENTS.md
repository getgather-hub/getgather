# AGENTS Instructions

This file provides guidance when working with code in this repository.

## Overview

A FastAPI server (`getgather`) that launches and manages remote, containerized Chrome browsers and exposes their CDP ([Chrome Devtools Protocol](https://chromedevtools.github.io/devtools-protocol/)) sessions over a REST/WebSocket API. It drives browsers via [zendriver](https://github.com/stephanlensky/zendriver) (CDP) across pluggable backends (Podman, Daytona, or an external Chrome Fleet). The server itself is stateless; browser sessions live in the selected backend, identified by `browser_id`.

## Common Commands

```bash
# Dev server
make dev                                  # uvicorn on :23456, --reload

# Static analysis (matches CI + pre-push hook)
make check                                # all of the below
make check-backend-format                 # ruff check + ruff format --check
make format-backend                       # ruff format + ruff check --fix
make typecheck                            # pyright . (strict mode) + ty check
make check-frontend-format                # prettier check on html/js/ts/css/json/md
make check-yaml-format                    # yamlfix --check

# Tests (markers: api, webui; anything else = unit)
make test                                 # unit tests (CI)
uv run pytest -m "api" -s -p no:xdist     # integration vs live server
uv run pytest tests/test_browser_api_e2e.py                              # single test file
uv run pytest tests/test_browser_api_e2e.py::TestPageContent::test_page_html
```

## Architecture

### Browser backends

`getgather/browsers/backend.py` defines the pluggable backend interface; `getgather/browsers/router.py` mounts the generic `/api/v1/browsers` CRUD API on top of it. The active backend is selected at startup:

- **Podman** (default) — launches a local container per browser
- **Daytona** (`BROWSER_BACKEND=daytona`) — an on-demand Daytona sandbox, reached over a signed HTTPS preview URL
- **External Fleet** (`CHROMEFLEET_URL` set) — proxies to an upstream Chrome Fleet instance; takes precedence over the other two

### CDP session plumbing

`getgather/browser.py` holds the core zendriver/CDP helpers used across the app: `create_remote_browser`/`terminate_remote_browser`, page lookup/navigation (`get_new_page`, `zen_navigate_with_retry`), and low-level element/selector primitives (`page_query_selector`, `page_batch_extract`). `getgather/cdp_client.py` provides a raw CDP client used to tunnel WebSocket sessions through `GET /api/v1/browsers/{id}/cdp`.

### Tracing

`getgather/tracing.py` configures Logfire (if `LOGFIRE_TOKEN` set) and a `SessionTraceMiddleware` that reparents per-request OTel spans under a session-root span keyed by `x-session-id`. The session ID doubles as the trace ID, so it can be pasted into Logfire. This middleware wraps the FastAPI app last so it runs before OTel's FastAPI instrumentation. `getgather/logs.py`'s `LoggingContextMiddleware` attaches the same session id (and resolved client IP) to loguru's contextvars for every request.

## Conventions

- Python 3.11+, pyright **strict** mode — avoid `Any` drift, annotate returns
- ruff lint selects `I, UP045, UP006, UP007` (isort + modern typing) with `line-length = 100`
- Pre-push hook runs `make check`; don't bypass with `--no-verify`.
- Settings via pydantic `BaseSettings` reads `.env`; see `.env.template` for keys
