import asyncio
import ipaddress
import os
import re
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any, Self

import zendriver as zd
from bs4 import BeautifulSoup, Tag
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastmcp.server.dependencies import get_http_headers
from loguru import logger
from nanoid import generate
from zendriver.core.connection import ProtocolException

from getgather.browser import (
    ElementConfig,
    create_remote_browser,
    find_browser_tab,
    get_new_page,
    get_remote_browser,
    page_batch_actions,
    page_query_selector,
    safe_close_page,
    terminate_remote_browser,
    wait_for_ready_state,
    zen_navigate_with_retry,
)
from getgather.cloak_human import human_pre_action_idle, reset_cursor_for_tab
from getgather.config import settings
from getgather.mcp.html_renderer import DEFAULT_TITLE, render_form
from getgather.zen_distill import (
    Match,
    Pattern,
    autoclick,
    capture_page_artifacts,
    distill,
    get_error,
    get_match_attr,
    get_selector,
    load_distillation_patterns,
    make_error_reporter,
    run_distillation_loop,
    terminate,
    zen_report_distill_error,
)

router = APIRouter(prefix="/dpage", tags=["dpage"])


# Max seconds for the distillation polling loop in zen_post_dpage (per HTTP request).
DEFAULT_DPAGE_POST_POLL_TIMEOUT = 60

FRIENDLY_CHARS: str = "23456789abcdefghijkmnpqrstuvwxyz"

SIGN_IN_ID_DELIMITER = "--"


def get_host_id() -> str:
    """Stable per-host id, used as the fallback browser key when no mcp-session-id is present."""
    hostname = socket.gethostname()
    logger.warning(f"Hostname is {hostname}")
    return re.sub(r"[^a-z0-9-]", "", hostname.lower().removesuffix(".local")) + "-noauth"


@dataclass(frozen=True)
class SignInId:
    browser_id: str
    target_id: str
    mcp_session_id: str | None = None

    def __str__(self) -> str:
        parts = [self.browser_id, self.target_id]
        if self.mcp_session_id:
            parts.append(self.mcp_session_id)
        return SIGN_IN_ID_DELIMITER.join(parts)

    @classmethod
    def from_str(cls, value: str) -> Self:
        parts = value.split(SIGN_IN_ID_DELIMITER, 2)
        if len(parts) == 3:
            browser_id, target_id, mcp_session_id = parts
            return cls(browser_id, target_id, mcp_session_id or None)
        if len(parts) == 2:
            browser_id, target_id = parts
            return cls(browser_id, target_id, None)
        raise ValueError(f"Invalid SignInId: {value!r}")

    @classmethod
    def from_dpage_path(cls, path: str) -> Self | None:
        if not path.startswith("/dpage/"):
            return None
        id_str = path.removeprefix("/dpage/").split("/", 1)[0]
        if not id_str:
            return None
        try:
            return cls.from_str(id_str)
        except ValueError:
            return None

    @classmethod
    def from_request(cls, request: Request) -> Self | None:
        raw = request.headers.get("x-signin-id")
        signin_id = cls.from_str(raw) if raw else cls.from_dpage_path(request.url.path)
        if signin_id is None:
            return None
        if signin_id.mcp_session_id is None:
            header_session = request.headers.get("mcp-session-id")
            if header_session:
                signin_id = cls(signin_id.browser_id, signin_id.target_id, header_session)
        return signin_id


def _mcp_session_id_from_headers() -> str | None:
    return get_http_headers(include_all=True).get("mcp-session-id") or None


def _signin_flow_response(signin_id: SignInId) -> dict[str, Any]:
    """Build the standard sign-in flow response dict."""
    base_url = get_base_url()
    signin_id_str = str(signin_id)
    url = f"{base_url}/dpage/{signin_id_str}"
    return {
        "url": url,
        "message": f"Continue to sign in in your browser at {url}.",
        "signin_id": signin_id_str,
        "system_message": (
            f"Try open the url {url} in a browser with a tool if available. "
            "Give the url to the user so the user can open it manually in their browser. "
            "Then call check_signin with the signin_id to see when sign-in finished (it does not return tool data). "
            "Call the same MCP tool again to get the result. "
            "For incognito or explicit session flows, send the x-signin-id header on the retry. "
        ),
    }


def _target_domain_from_initial_url(initial_url: str) -> str:
    hostname = urllib.parse.urlparse(initial_url).hostname
    return hostname or ""


async def _try_action_with_probe(
    browser: zd.Browser,
    initial_url: str,
    action: Any,
    timeout: int,
) -> dict[str, Any] | None:
    """Probe whether browser is already signed in and run the action if so.

    Opens a new tab, navigates to initial_url, and runs a non-interactive
    distillation check. If the page looks signed-in (terminated), executes
    the action and returns the result. Otherwise returns None so the caller
    can fall through to an interactive sign-in flow.
    """
    page = await get_new_page(browser)
    try:
        await zen_navigate_with_retry(page, initial_url)
        terminated = await _probe_page(
            location=initial_url, page=page, browser=browser, timeout=timeout
        )
        if terminated:
            result = await action(page, browser)
            await safe_close_page(page)
            return result
        await safe_close_page(page)
        return None
    except Exception as e:
        logger.info(f"Stateless probe failed for {initial_url}: {e}")
        await safe_close_page(page)
        return None


async def _probe_page(
    *,
    location: str | None = None,
    page: zd.Tab,
    browser: zd.Browser,
    timeout: int = 2,
    patterns: list[Pattern] | None = None,
) -> bool:
    if patterns is None:
        path = os.path.join(os.path.dirname(__file__), "patterns", "**/*.html")
        patterns = load_distillation_patterns(path)
    terminated, _, _ = await run_distillation_loop(
        location=location,
        patterns=patterns,
        browser=browser,
        timeout=timeout,
        page=page,
    )
    return terminated


async def dpage_check(id: str):
    TICK = 1  # seconds
    TIMEOUT = 120  # seconds
    max = TIMEOUT // TICK

    signin_id = SignInId.from_str(id)
    browser: zd.Browser | None = None
    path = os.path.join(os.path.dirname(__file__), "patterns", "**/*.html")
    probe_patterns = load_distillation_patterns(path)

    for iteration in range(max):
        logger.debug(f"Checking dpage {id}: {iteration + 1} of {max}")
        await asyncio.sleep(TICK)

        if browser is None:
            browser = await get_remote_browser(signin_id.browser_id)
        if browser is None:
            continue

        page = find_browser_tab(browser, signin_id.target_id)
        if page is None:
            browser = None
            continue

        try:
            terminated = await _probe_page(
                page=page, browser=browser, timeout=2, patterns=probe_patterns
            )
            if terminated:
                return True
        except Exception as e:
            logger.warning(f"Remote probe failed for {id}: {e}")
            browser = None

    return None


async def dpage_finalize(id: str):
    browser_id = SignInId.from_str(id).browser_id
    if browser := await get_remote_browser(browser_id):
        await terminate_remote_browser(browser)
        return True

    raise ValueError(f"Browser profile for signin {id} not found in incognito browser profiles")


def render(content: str, options: dict[str, str] | None = None) -> str:
    """Render HTML template with content and options."""
    if options is None:
        options = {}

    title = options.get("title", DEFAULT_TITLE)
    action = options.get("action", "")
    error_code = options.get("error_code", None)

    return render_form(content, title, action, error_code)


# Since the browser can't redirect from GET to POST,
# we'll use an auto-submit form to do that.
def redirect(id: str) -> HTMLResponse:
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <body>
      <form id="redirect" action="/dpage/{id}" method="post">
      </form>
      <script>document.getElementById('redirect').submit();</script>
    </body>
    </html>
    """)


@router.get("", response_class=HTMLResponse)
@router.get("/{id}", response_class=HTMLResponse)
async def get_dpage(id: str | None = None) -> HTMLResponse:
    if id:
        return redirect(id)

    raise HTTPException(status_code=400, detail="Missing page id")


FINISHED_MSG = "Finished! You can close this window now."


@router.post("/{id}", response_class=HTMLResponse)
async def post_dpage(id: str, request: Request) -> HTMLResponse:
    signin_id = SignInId.from_request(request)
    if signin_id is None:
        raise HTTPException(status_code=400, detail="Missing or invalid sign-in id")

    browser = await get_remote_browser(signin_id.browser_id)
    if browser is None:
        raise HTTPException(status_code=404, detail="Remote browser not found")

    page = find_browser_tab(browser, signin_id.target_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")

    return await zen_post_dpage(page, id, request)


def is_local_address(host: str) -> bool:
    hostname = host.split(":")[0].lower().strip()
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_loopback
    except ValueError:
        return hostname in ("localhost", "127.0.0.1")


def get_base_url() -> str:
    headers = get_http_headers(include_all=True)
    host = headers.get("x-forwarded-host") or headers.get("host")
    if host is None:
        logger.warning("Missing Host header; defaulting to localhost")
        base_url = "http://localhost:23456"
    else:
        default_scheme = "http" if is_local_address(host) else "https"
        scheme = headers.get("x-forwarded-proto", default_scheme)
        base_url = f"{scheme}://{host}"
    return base_url


def is_incognito_request(headers: dict[str, str]) -> bool:
    return headers.get("x-incognito", "0") == "1"


def _html_int_attr(element: Tag | None, name: str, default: int = 0) -> int:
    if element is None:
        return default
    raw = element.get(name)
    if raw is None:
        return default
    try:
        return int(str(raw))
    except ValueError:
        return default


async def _maybe_reload_for_matched_pattern(
    page: zd.Tab,
    match: Match,
    patterns: list[Pattern],
) -> bool:
    """Reload once when the distilled pattern requests it (mirrors manual live-view reload)."""
    pattern_item = next((item for item in patterns if item.name == match.name), None)
    if pattern_item is None:
        return False

    root = pattern_item.pattern.find("html")
    if not isinstance(root, Tag):
        return False
    if "rb-reload-before-actions" not in root.attrs:
        return False

    settle_ms = _html_int_attr(root, "rb-settle-ms", 5000)
    logger.info(f"Reloading page before trusted actions for {match.name} (settle {settle_ms}ms)")
    await page.reload()
    reset_cursor_for_tab(page)
    try:
        await wait_for_ready_state(page, timeout=30)
    except Exception as exc:
        logger.warning(f"Ready state after reload: {exc}")
    await asyncio.sleep(settle_ms / 1000)
    return True


async def distill_post_loop(
    page: zd.Tab,
    id: str,
    fields: dict[str, str],
    action: str,
    patterns: list[Pattern] | None = None,
    timeout: int = DEFAULT_DPAGE_POST_POLL_TIMEOUT,
) -> HTMLResponse:
    if patterns is None:
        path = os.path.join(os.path.dirname(__file__), "patterns", "**/*.html")
        patterns = load_distillation_patterns(path)

    logger.info(f"Continuing distillation for page {id}...")
    logger.debug(f"Available distillation patterns: {len(patterns)}")

    TICK = 1  # seconds
    max = timeout // TICK

    current = Match(name="", priority=-1, distilled="")

    if settings.LOG_LEVEL == "DEBUG":
        await capture_page_artifacts(page, identifier=id, prefix="dpage_debug")

    # Force browser to complete rendering by evaluating document state
    try:
        await wait_for_ready_state(page, timeout=5)
        # Additional wait for any dynamic content/JavaScript to settle
        await page.sleep(1)
        logger.debug("Page ready state is complete")
    except Exception as e:
        logger.warning(f"Error waiting for page ready state: {e}")

    reload_before_actions_done = False

    for iteration in range(max):
        logger.debug(f"Iteration {iteration + 1} of {max}")
        await asyncio.sleep(TICK)

        try:
            current_url = str(await page.evaluate("window.location.href", await_promise=True))
        except Exception:
            current_url = page.url
        hostname = str(urllib.parse.urlparse(current_url).hostname) if current_url else None
        match = await distill(hostname, page, patterns)
        if not match:
            logger.info("No matched pattern found")
            continue

        distilled = match.distilled
        document = BeautifulSoup(distilled, "html.parser")

        title_element = BeautifulSoup(distilled, "html.parser").find("title")
        title = title_element.get_text() if title_element is not None else DEFAULT_TITLE
        options = {"title": title, "action": action}
        inputs = document.find_all("input")
        pending_actions: list[dict[str, str]] = []
        html_element = document.find("html")
        html_tag = html_element if isinstance(html_element, Tag) else None
        action_delay_ms = (
            int(str(html_tag.get("rb-action-delay") or 0)) if html_tag is not None else 0
        )
        element_config = (
            ElementConfig(action_delay_ms=action_delay_ms) if action_delay_ms > 0 else None
        )
        trusted_actions = html_tag is not None and "rb-trusted-actions" in html_tag.attrs
        pattern_humanize = html_tag is not None and "rb-humanize" in html_tag.attrs
        use_cdp_actions = trusted_actions or pattern_humanize
        humanize = pattern_humanize
        if humanize:
            element_config = ElementConfig(
                action_delay_ms=action_delay_ms,
                humanize=True,
            )
        elif use_cdp_actions and action_delay_ms > 0:
            element_config = ElementConfig(action_delay_ms=action_delay_ms)

        if match.distilled == current.distilled:
            logger.info(f"Still the same: {match.name}")
            has_inputs = len(inputs) > 0
            max_reached = iteration == max - 1
            if max_reached and has_inputs:
                logger.info("Still the same after timeout and need inputs, render the page...")
                return HTMLResponse(render(str(document.find("body")), options))
            continue

        if not reload_before_actions_done:
            if await _maybe_reload_for_matched_pattern(page, match, patterns):
                reload_before_actions_done = True
                continue

        current = match

        if use_cdp_actions:
            pre_action_ms = _html_int_attr(html_tag, "rb-pre-action-idle-ms", 0)
            if pre_action_ms > 0:
                logger.info(f"Pre-action idle mouse for {pre_action_ms}ms")
                await human_pre_action_idle(page, pre_action_ms)

        if await terminate(distilled):
            logger.info("Finished!")

            error = await get_error(distilled)
            if error is not None:
                logger.info(
                    f"Distillation reported page error pattern; sign-in still marked complete for polling. Pattern name: {match.name}"
                )
                options["error_code"] = error

            return HTMLResponse(render(FINISHED_MSG, options))

        names: list[str] = []

        if fields.get("button"):
            button = document.find("button", value=str(fields.get("button")))
            if button:
                logger.info(f"Clicking button button[value={fields.get('button')}]")
                await autoclick(page, distilled, f"button[value={fields.get('button')}]")
                continue

        processed_radio_groups: set[str] = set()
        radio_names_for_expected: set[str] = set()
        expected_field_count = 0
        for input in inputs:
            if isinstance(input, Tag):
                name = input.get("name")
                input_type = input.get("type")

                if input_type == "radio":
                    if name is None:
                        continue
                    name_str = str(name)
                    if name_str not in radio_names_for_expected:
                        radio_names_for_expected.add(name_str)
                        expected_field_count += 1
                    if name_str in processed_radio_groups:
                        continue
                    value = fields.get(name_str)
                    if not value or len(str(value)) == 0:
                        logger.warning(f"No form data found for radio button group {name_str}")
                        continue
                    radio = document.find(
                        "input",
                        {"type": "radio", "name": name_str, "value": str(value)},
                    )
                    if not isinstance(radio, Tag):
                        logger.warning(f"No radio button found for group {name_str} value {value}")
                        continue
                    rgm = get_match_attr(radio)
                    if not rgm:
                        continue
                    selector, frame_selector = get_selector(str(rgm))
                    config = getattr(page, "element_config", None)
                    radio_element = await page_query_selector(
                        page,
                        selector if selector is not None else "",
                        iframe_selector=frame_selector,
                        config=config,
                    )
                    if radio_element:
                        logger.info(f"Handling radio button group {name}")
                        logger.info(f"Using form data {name_str}={value}")
                        await radio_element.click()
                        radio["checked"] = "checked"
                        current.distilled = str(document)
                        names.append(name_str)
                        processed_radio_groups.add(name_str)
                    continue

                expected_field_count += 1
                gg_match = get_match_attr(input)
                selector, frame_selector = get_selector(
                    str(gg_match) if gg_match is not None else ""
                )
                config = getattr(page, "element_config", None)
                name = input.get("name")
                input_type = input.get("type")

                if input_type == "checkbox" or input_type == "radio":
                    element = await page_query_selector(
                        page,
                        selector if selector is not None else "",
                        iframe_selector=frame_selector,
                        config=config,
                    )
                    if not element:
                        continue
                    if input_type == "checkbox":
                        if not name:
                            logger.warning(f"No name for the checkbox {gg_match}")
                            continue
                        value = fields.get(str(name))
                        checked = value and len(str(value)) > 0
                        names.append(str(name))
                        logger.info(f"Status of checkbox {name}={checked}")
                        current_checked_value = (
                            element.element.get("checked") or element.element.get("value") == "true"
                        )
                        if current_checked_value != checked:
                            logger.info(f"Clicking checkbox {name} to set it to {checked}")
                            await element.click()
                    elif input_type == "radio":
                        if name is not None:
                            name_str = str(name)
                            value = fields.get(name_str)
                            if not value or len(value) == 0:
                                logger.warning(f"No form data found for radio button group {name}")
                                continue
                            radio = document.find("input", {"type": "radio", "value": str(value)})
                            if not radio or not isinstance(radio, Tag):
                                logger.warning(f"No radio button found with value {value}")
                                continue
                            logger.info(f"Handling radio button group {name}")
                            logger.info(f"Using form data {name}={value}")
                            radio_gg_match = str(get_match_attr(radio))
                            selector, frame_selector = get_selector(radio_gg_match)
                            config = getattr(page, "element_config", None)
                            radio_element = await page_query_selector(
                                page,
                                selector if selector is not None else "",
                                iframe_selector=frame_selector,
                                config=config,
                            )
                            if radio_element:
                                await radio_element.click()
                                radio["checked"] = "checked"
                                current.distilled = str(document)
                                names.append(str(input.get("id")) if input.get("id") else "radio")
                elif name is not None:
                    name_str = str(name)
                    value = fields.get(name_str)
                    if value and len(value) > 0:
                        logger.info(f"Using form data {name}")
                        names.append(name_str)
                        input["value"] = value
                        current.distilled = str(document)
                        if use_cdp_actions and input_type not in ("checkbox", "radio"):
                            text_element = await page_query_selector(
                                page,
                                selector if selector is not None else "",
                                iframe_selector=frame_selector,
                                config=element_config,
                            )
                            if text_element:
                                await text_element.type_text(str(value))
                                await asyncio.sleep(0.25)
                            else:
                                logger.warning(f"Trusted fill could not find {selector}")
                        else:
                            pending_actions.append({
                                "key": f"set:{name}:{len(pending_actions)}",
                                "kind": "set_value",
                                "selector": str(selector),
                                "value": str(value),
                                "action_delay_ms": str(action_delay_ms),
                            })
                        del fields[name_str]
                    else:
                        logger.info(f"No form data found for {name}")

        # Queue non-button auto-clicks so they can run in the same batch as field updates.
        AUTOCLICK_NON_BUTTON_SELECTOR = "[rb-autoclick]:not(button), [gg-autoclick]:not(button)"
        for auto_click_target in document.select(AUTOCLICK_NON_BUTTON_SELECTOR):
            auto_click_selector, _ = get_selector(str(get_match_attr(auto_click_target)))
            if auto_click_selector:
                pending_actions.append({
                    "key": f"click:auto:{len(pending_actions)}",
                    "kind": "click",
                    "selector": str(auto_click_selector),
                })

        should_submit = False
        submit_delay_ms = _html_int_attr(html_tag, "rb-submit-delay-ms", 0)
        after_submit_delay_ms = _html_int_attr(html_tag, "rb-after-submit-delay-ms", 0)
        SUBMIT_BUTTON = "button[rb-autoclick], button[gg-autoclick], button[type=submit]"
        if document.select(SUBMIT_BUTTON):
            if len(names) > 0 and expected_field_count == len(names):
                logger.info("Submitting form, all fields are filled...")
                if submit_delay_ms > 0:
                    logger.info(f"Waiting {submit_delay_ms}ms before submit")
                    await asyncio.sleep(submit_delay_ms / 1000)
                for submit_button in document.select(SUBMIT_BUTTON):
                    submit_selector, frame_selector = get_selector(
                        str(get_match_attr(submit_button))
                    )
                    if not submit_selector:
                        continue
                    if use_cdp_actions:
                        submit_element = await page_query_selector(
                            page,
                            submit_selector,
                            iframe_selector=frame_selector,
                            config=element_config,
                        )
                        if submit_element:
                            logger.info(f"Trusted CDP mouse click on {submit_selector}")
                            await submit_element.trusted_click()
                        else:
                            logger.warning(f"Trusted submit could not find {submit_selector}")
                    else:
                        pending_actions.append({
                            "key": f"click:submit:{len(pending_actions)}",
                            "kind": "click",
                            "selector": str(submit_selector),
                            "action_delay_ms": str(action_delay_ms),
                        })
                should_submit = True
            else:
                logger.warning("Not all form fields are filled")
                return HTMLResponse(render(str(document.find("body")), options))

        if len(pending_actions) > 0:
            action_results = await page_batch_actions(page, pending_actions)
            results = action_results if isinstance(action_results, dict) else {}

            # Fallback for failed/unexecuted actions to preserve behavior.
            for i, pending_action in enumerate(pending_actions):
                key = pending_action.get("key")
                kind = pending_action.get("kind")
                selector = pending_action.get("selector")
                if (
                    not isinstance(key, str)
                    or not isinstance(kind, str)
                    or not isinstance(selector, str)
                ):
                    continue
                if results.get(key, False):
                    continue

                config = element_config if i > 0 else None
                element = await page_query_selector(page, selector, config=config)
                if not element:
                    continue
                try:
                    if kind == "click":
                        await element.click()
                    elif kind == "set_value":
                        value = pending_action.get("value")
                        await element.type_text(value if isinstance(value, str) else "")
                except ProtocolException as e:
                    # Node went stale mid-action (page re-rendered the field). This fallback
                    # is best-effort; the distillation loop re-distills and retries next
                    # iteration, so skip this field rather than 500 the whole request.
                    logger.warning(f"Fallback {kind} on {selector!r} hit stale node, skipping: {e}")
                    continue

            await asyncio.sleep(0.25)

        if should_submit:
            if after_submit_delay_ms > 0:
                logger.info(f"Waiting {after_submit_delay_ms}ms after submit")
                await asyncio.sleep(after_submit_delay_ms / 1000)
            continue

    hostname_attr: str | None = getattr(page, "hostname", None)  # type: ignore[assignment]
    location = getattr(page, "url", "unknown")  # type: ignore[assignment]
    timeout_error = TimeoutError("Timeout reached in distill_post_loop")

    await zen_report_distill_error(
        error=timeout_error,
        page=page,
        profile_id=id,
        location=location,
        hostname=hostname_attr or "unknown",
        iteration=max,
    )
    raise HTTPException(status_code=503, detail="Timeout reached")


async def zen_post_dpage(page: zd.Tab, id: str, request: Request) -> HTMLResponse:
    form_data = await request.form()
    fields: dict[str, str] = {k: str(v) for k, v in form_data.items()}
    return await distill_post_loop(page, id, fields, f"/dpage/{id}")


async def remote_zen_dpage_mcp_tool(
    initial_url: str,
    result_key: str,
    timeout: int = 2,
    config: ElementConfig | None = None,
) -> dict[str, Any]:
    """Generic MCP tool based on distillation with remote Zendriver"""
    path = os.path.join(os.path.dirname(__file__), "patterns", "**/*.html")
    patterns = load_distillation_patterns(path)

    headers = get_http_headers(include_all=True)
    incoming_raw = headers.get("x-signin-id") or None
    incoming = SignInId.from_str(incoming_raw) if incoming_raw else None
    mcp_session_id = _mcp_session_id_from_headers()
    incognito = is_incognito_request(headers)

    browser = None
    page = None

    if incoming:
        browser_id = incoming.browser_id
        browser = await get_remote_browser(browser_id)
        if browser is None:
            raise HTTPException(status_code=400, detail="Remote browser not found")
        logger.info(f"Continue with browser {browser_id} (signed in, opening new page")
        page = await get_new_page(browser)
        signin_id = SignInId(
            browser_id, str(page.target_id), incoming.mcp_session_id or mcp_session_id
        )
    elif incognito:
        prefix = "E"  # for Ephemeral
        browser_id = prefix + generate(FRIENDLY_CHARS, 7)
        browser = await create_remote_browser(
            browser_id, target_domain=_target_domain_from_initial_url(initial_url)
        )
        page = await get_new_page(browser)
        signin_id = SignInId(browser_id, str(page.target_id), mcp_session_id)
        logger.info(f"Start with an ephemeral browser {browser_id}")
    else:
        browser_id: str = mcp_session_id or get_host_id()
        browser = await get_remote_browser(browser_id)
        if browser is None:
            browser = await create_remote_browser(
                browser_id, target_domain=_target_domain_from_initial_url(initial_url)
            )
        page = await get_new_page(browser)
        signin_id = SignInId(browser_id, str(page.target_id), mcp_session_id)
        logger.info(f"Using browser {browser_id}")

    logger.info(f"Navigating remote browser to {initial_url}")
    await zen_navigate_with_retry(page, initial_url)

    error_reporter = make_error_reporter(browser, initial_url) if not incognito else None
    terminated, distilled, converted = await run_distillation_loop(
        location=initial_url,
        patterns=patterns,
        browser=browser,
        timeout=timeout,
        page=page,
        error_reporter=error_reporter,
    )
    if terminated:
        await safe_close_page(page)
        distillation_result = converted if converted is not None else distilled
        return {result_key: distillation_result}

    page.hostname = urllib.parse.urlparse(initial_url).hostname  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    response = _signin_flow_response(signin_id)
    logger.info(
        f"Continue with the sign in at {response['url']}",
        extra={"url": response["url"], "id": response["signin_id"]},
    )
    return response


async def remote_zen_dpage_with_action(
    initial_url: str,
    action: Any,
    timeout: int = 2,
    config: ElementConfig | None = None,
) -> dict[str, Any]:
    """Execute an action after signin completion with remote Zendriver."""
    path = os.path.join(os.path.dirname(__file__), "patterns", "**/*.html")
    patterns = load_distillation_patterns(path)

    headers = get_http_headers(include_all=True)
    incoming_raw = headers.get("x-signin-id") or None
    incoming = SignInId.from_str(incoming_raw) if incoming_raw else None
    mcp_session_id = _mcp_session_id_from_headers()
    incognito = is_incognito_request(headers)

    # Probe any existing browser for an authenticated session before opening dpage.
    probe_browser = None
    if incoming:
        probe_browser = await get_remote_browser(incoming.browser_id)
    elif not incognito:
        probe_browser = await get_remote_browser(mcp_session_id or get_host_id())
    if probe_browser is not None:
        result = await _try_action_with_probe(probe_browser, initial_url, action, timeout)
        if result is not None:
            return result

    # Create interactive sign-in flow (client retries tool after check_signin)
    page = None
    if incoming:
        browser_id = incoming.browser_id
        browser = await get_remote_browser(browser_id)
        if browser is None:
            raise HTTPException(status_code=400, detail="Remote browser not found")
        page = find_browser_tab(browser, incoming.target_id)
        session_id = incoming.mcp_session_id or mcp_session_id
        if page is None:
            logger.info(
                f"Tab {incoming.target_id} no longer exists, opening new tab on browser {browser_id}"
            )
            page = await get_new_page(browser)
            signin_id = SignInId(browser_id, str(page.target_id), session_id)
        else:
            logger.info(f"Continue with remote browser {browser_id} and page {incoming.target_id}")
            signin_id = SignInId(browser_id, incoming.target_id, session_id)
    elif incognito:
        prefix = "E"
        browser_id = prefix + generate(FRIENDLY_CHARS, 7)
        browser = await create_remote_browser(
            browser_id, target_domain=_target_domain_from_initial_url(initial_url)
        )
        page = await get_new_page(browser)
        signin_id = SignInId(browser_id, str(page.target_id), mcp_session_id)
        logger.info(f"Start with ephemeral remote browser {browser_id}")
    else:
        browser_id = mcp_session_id or get_host_id()
        browser = await get_remote_browser(browser_id)
        if browser is None:
            browser = await create_remote_browser(
                browser_id, target_domain=_target_domain_from_initial_url(initial_url)
            )
        page = await get_new_page(browser)
        signin_id = SignInId(browser_id, str(page.target_id), mcp_session_id)
        logger.info(f"Using remote browser {browser_id}")

    await zen_navigate_with_retry(page, initial_url)

    error_reporter = make_error_reporter(browser, initial_url) if not incognito else None
    terminated, _, _ = await run_distillation_loop(
        location=initial_url,
        patterns=patterns,
        browser=browser,
        timeout=timeout,
        page=page,
        error_reporter=error_reporter,
    )
    if terminated:
        result = await action(page, browser)
        await safe_close_page(page)
        return result

    page.hostname = urllib.parse.urlparse(initial_url).hostname  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    response = _signin_flow_response(signin_id)
    logger.info(
        f"remote_zen_dpage_with_action: Continue with sign in at {response['url']}",
        extra={"url": response["url"], "id": response["signin_id"]},
    )
    return response
