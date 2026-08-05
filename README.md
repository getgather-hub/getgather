# Remote Browser

[![PyPI](https://img.shields.io/pypi/v/remotebrowser)](https://pypi.org/project/remotebrowser/)

Remote Browser is an open-source, self-hosted browser orchestration system for AI agent [harness engineering](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents).

It launches and manages multiple isolated, containerized Chrome instances with CDP ([Chrome Devtools Protocol](https://chromedevtools.github.io/devtools-protocol/)) support for scalable web automation. Remote Browser is designed to integrate with AI agent runtimes and browser tools, and works with [OpenClaw](https://openclaw.ai), [Hermes Agent](https://hermes-agent.nousresearch.com), etc.

It also bundles an MCP server for extracting personal data from many services: Amazon order history, Garmin activity stats, Zillow favorites, and more. This MCP server works with [Claude Code](https://claude.ai/code), [LM Studio](https://lmstudio.ai), [Gemini CLI](https://google-gemini.github.io/gemini-cli), and many more.

![Screenshot of Claude Code using Remote Browser MCP](claude-code-remotebrowser-mcp.png)

## Quickstart

Remote Browser is a Python app. To run it, you need [uv](https://docs.astral.sh/uv) and [Podman](https://podman.io):

```bash
uvx remotebrowser
```

Then open `http://localhost:23456`.

## MCP

**Standard config** works with most tools:

```js
{
  "mcpServers": {
    "remotebrowser-mcp": {
      "url": "http://127.0.0.1:23456/mcp"
    }
  }
}
```

<details>
<summary>Claude Code</summary>

Use the Claude Code CLI to add the MCP server:

```bash
claude mcp add --transport http remotebrowser-mcp http://localhost:23456/mcp
```

</details>

<details>
<summary>Claude Desktop</summary>

Follow the MCP install [guide](https://modelcontextprotocol.io/quickstart/user), use the standard config above.

</details>

<details>
<summary>Gemini CLI</summary>

Follow the MCP install [guide](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md#configure-the-mcp-server-in-settingsjson), use the standard config above.

</details>

<details>
<summary>LM Studio</summary>

Go to `Program` in the right sidebar -> `Install` -> `Edit mcp.json`. Use the standard config above.

</details>

<details>
<summary>VS Code</summary>

Follow the MCP install [guide](https://code.visualstudio.com/docs/copilot/chat/mcp-servers#_add-an-mcp-server), use the standard config above.

</details>

### Local mock Amazon

The Amazon US connector can target a deployed instance of the deterministic `apps/mock-amazon`
app from the [`corelens-engineering/demos` repository](https://github.com/corelens-engineering/demos)
instead of the real site. Set its browser-reachable origin before starting Remote Browser:

```bash
AMAZON_BASE_URL=https://your-deployed-mock-amazon.example.com
```

The override applies to Amazon US only, including its Prime Video pagination APIs. Amazon Canada
and an unset configuration continue to use the real sites.

With both servers running, exercise all eleven Amazon US MCP tools with:

```bash
uv run pytest tests/mcp/test_amazon.py -s -p no:xdist
```

The test signs in with `joe@example.com` / `trustno1`. It is skipped when `AMAZON_BASE_URL` is
unset, so the normal MCP suite does not require a deployed mock.

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

### Get distilled page JSON

`GET /api/v1/browsers/{browser_id}/pages/{page_id}/distilled` returns the distilled JSON representation of the specified page, produced by matching the page against distillation patterns. Returns HTTP 404 if the browser or page is not found.

_Example_: `curl localhost:23456/api/v1/browsers/test/pages/96FDE4162B8EEEBF98E26756D21CF0C5/distilled`

### Submit distilled page form

`POST /api/v1/browsers/{browser_id}/pages/{page_id}/distill` submits form field values for the specified page and continues the distillation loop, returning the next rendered distilled HTML form. The request body is form-encoded (`application/x-www-form-urlencoded` or `multipart/form-data`); each field is mapped by its `name` to the corresponding form input. Returns HTTP 404 if the browser or page is not found.

_Example_: `curl -X POST localhost:23456/api/v1/browsers/test/pages/96FDE4162B8EEEBF98E26756D21CF0C5/distill -d 'email=user@example.com&password=secret'` returns the next distilled HTML page.

### List all patterns

`GET /api/v1/patterns` returns a JSON array of all pattern filenames (sorted alphabetically), including the extension (`.html` or `.json`).

_Example_: `curl localhost:8300/api/v1/patterns` returns:

```json
["amazon_signin.html", "amazon_signin.json", "goodreads_signin.html"]
```

### Get a pattern

`GET /api/v1/patterns/{pattern_name}` returns the content of the named pattern. Accepts an optional `ext` query parameter (`html` or `json`, defaults to `html`). Returns HTTP 404 if the pattern is not found, HTTP 400 if the name contains invalid characters or the extension is unsupported.

_Example_: `curl localhost:8300/api/v1/patterns/amazon_signin` returns the raw HTML of `amazon_signin.html`.

_Example_: `curl localhost:8300/api/v1/patterns/amazon_signin?ext=json` returns the JSON content of `amazon_signin.json`.

### Create or update a pattern

`POST /api/v1/patterns/{pattern_name}` writes content to the named pattern file, creating it if it does not exist. Accepts an optional `ext` query parameter (`html` or `json`, defaults to `html`). Returns HTTP 400 if the name contains invalid characters or the extension is unsupported.

_Example_: `curl -X POST localhost:8300/api/v1/patterns/amazon_signin -H 'Content-Type: application/json' -d '{"content": "<div gg-match=\"...\">"}'` returns:

```json
{ "pattern_name": "amazon_signin", "status": "created" }
```

On subsequent calls the status field is `"updated"`.

_Example (JSON)_: `curl -X POST 'localhost:8300/api/v1/patterns/amazon_signin?ext=json' -H 'Content-Type: application/json' -d '{"content": "{\"rows\": \"tr\"}"}'`

### Delete a pattern

`DELETE /api/v1/patterns/{pattern_name}` removes the named pattern file. Accepts an optional `ext` query parameter (`html` or `json`, defaults to `html`). Returns HTTP 404 if the pattern is not found, HTTP 400 if the name contains invalid characters or the extension is unsupported.

_Example_: `curl -X DELETE localhost:8300/api/v1/patterns/amazon_signin` returns:

```json
{ "pattern_name": "amazon_signin", "status": "deleted" }
```

_Example (JSON)_: `curl -X DELETE 'localhost:8300/api/v1/patterns/amazon_signin?ext=json'`

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
