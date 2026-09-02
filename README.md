# Remote Browser

[![PyPI](https://img.shields.io/pypi/v/remotebrowser)](https://pypi.org/project/remotebrowser/)

Remote Browser is an open-source, self-hosted browser orchestration system for AI agent [harness engineering](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents).

It launches and manages multiple isolated, containerized Chrome instances with CDP ([Chrome Devtools Protocol](https://chromedevtools.github.io/devtools-protocol/)) support for scalable web automation. Remote Browser is designed to integrate with AI agent runtimes and browser tools, and works with [OpenClaw](https://openclaw.ai), [Hermes Agent](https://hermes-agent.nousresearch.com), etc.

## Quickstart

Remote Browser is a Python app. To run it, you need [uv](https://docs.astral.sh/uv) and [Podman](https://podman.io):

```bash
uvx remotebrowser
```

Then open `http://localhost:23456`.

## API

### Start a new browser

`POST /api/v1/browsers/{browser_id}` creates a new browser with the specified `browser_id`. The browser runs in a container. If the `browser_id` is omitted, `POST /api/v1/browsers` creates a new browser with an automatically generated name.

_Example_: `curl -X POST localhost:8300/api/v1/browsers/xyz123` creates a container named `chromium-xyz123` and returns:

```json
{ "browser_id": "xyz123", "status": "created" }
```

### Stop a browser

`DELETE /api/v1/browsers/{browser_id}` terminates the browser with the specified `browser_id` and returns the container name. Returns HTTP 404 if the browser ID is not found.

_Example_: `curl -X DELETE localhost:8300/api/v1/browsers/xyz123` terminates the container named `chromium-xyz123` and returns:

```json
{ "status": "deleted" }
```

### Query a browser

`GET /api/v1/browsers/{browser_id}` returns information about the browser with the specified `browser_id`. Returns HTTP 404 if the browser is not found.

_Example_: `curl localhost:8300/api/v1/browsers/xyz123` returns:

```json
{ "last_activity_timestamp": 1772069081 }
```

### List all browsers

`GET /api/v1/browsers` returns a JSON array of all running browser IDs.

_Example_: `curl localhost:8300/api/v1/browsers` returns:

```json
["xyz123", "abc234"]
```

### List pages of a browser

`GET /api/v1/browsers/{browser_id}/pages` returns a JSON array of page identifiers (CDP target IDs) for all open pages in the specified browser. Returns HTTP 404 if the browser is not found.

_Example_: `curl localhost:23456/api/v1/browsers/test/pages` returns:

```json
["96FDE4162B8EEEBF98E26756D21CF0C5"]
```

### Connect to a browser over CDP

`GET /api/v1/browsers/{browser_id}/cdp` upgrades to a WebSocket and tunnels a CDP session to the specified browser. The browser is auto-launched if it isn't already running. Returns HTTP 4502 (WebSocket close code) if the remote debugger URL can't be resolved after retries.

_Example (Playwright with Node.js)_:

```js
const { chromium } = require("@playwright/test");
const target = "ws://localhost:23456/api/v1/browsers/xyz123/cdp";
const browser = await chromium.connectOverCDP(target);
```

### Get page HTML

`GET /api/v1/browsers/{browser_id}/pages/{page_id}/html` returns the raw HTML of the specified page. Returns HTTP 404 if the browser or page is not found.

_Example_: `curl localhost:23456/api/v1/browsers/test/pages/96FDE4162B8EEEBF98E26756D21CF0C5/html`

### Navigate a page

`POST` or `GET /api/v1/browsers/{browser_id}/pages/{page_id}/navigate` navigates the specified page to a URL taken from the request's query string. The `url` query parameter is preferred if present; otherwise the entire raw query string is used as the URL. Returns HTTP 400 if no URL is provided, HTTP 404 if the browser or page is not found, and HTTP 502 if the navigation fails.

_Example_: `curl -X POST 'localhost:23456/api/v1/browsers/test/pages/96FDE4162B8EEEBF98E26756D21CF0C5/navigate?url=https://text.npr.org/'` returns:

```json
{ "status": "success" }
```

## Backends

The browser API runs on one of three backends, selected at startup:

| Backend          | Selected by               | Browser runs as           | CDP reached via             |
| ---------------- | ------------------------- | ------------------------- | --------------------------- |
| Podman (default) | (default)                 | local podman container    | local mapped port           |
| Daytona          | `BROWSER_BACKEND=daytona` | on-demand Daytona sandbox | a signed HTTPS preview URL  |
| External Fleet   | `CHROMEFLEET_URL` set     | upstream Chrome Fleet     | the upstream fleet's `/cdp` |

Podman is the default and needs no extra setup. Daytona is an on-demand sandbox provider: install the extra (`uv sync --extra daytona`) and set `DAYTONA_API_KEY` and `DAYTONA_SNAPSHOT`, plus `DAYTONA_API_URL` for a self-hosted Daytona. Setting `CHROMEFLEET_URL` takes precedence and proxies the browser API to an external Chrome Fleet. Because Daytona is reached over a signed HTTPS preview URL rather than a local port, the VNC live view (`/live`, `/websockify`) and the residential-proxy / geo-IP features are podman only.

## Development

To run the development version, clone this repository and run:

```bash
uv run -m uvicorn getgather.main:app --port 23456
```

## Deployment

Supported deployment:

- [Deploy using Dokku](deploy-dokku.md)
