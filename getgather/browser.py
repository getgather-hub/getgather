import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar, cast
from urllib.parse import urlparse

import asyncio_atexit
import logfire
import sentry_sdk
import websockets
import zendriver as zd
from loguru import logger
from nanoid import generate
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from zendriver.core import util
from zendriver.core._contradict import ContraDict
from zendriver.core.config import Config
from zendriver.core.connection import Connection, ProtocolException

from getgather.browsers.fleet_browsers import build_chromefleet_headers, call_chromefleet_api
from getgather.config import FRIENDLY_CHARS, settings

_ws_extra_headers_var: ContextVar[dict[str, str] | None] = ContextVar(
    "_ws_extra_headers_var", default=None
)
_ws_connect_patched = False
_original_websockets_connect = websockets.connect


def _traced_websocket_connect(*args: Any, **kwargs: Any) -> Any:
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)

    merged: dict[str, str] = dict(kwargs.get("additional_headers") or {})
    context_headers = _ws_extra_headers_var.get()
    if context_headers:
        merged.update(context_headers)
    if carrier:
        merged.update(carrier)
    kwargs["additional_headers"] = merged
    kwargs.setdefault("open_timeout", settings.CHROMEFLEET_CDP_OPEN_TIMEOUT_SECONDS)

    logger.info(
        "CDP websocket headers attached",
        target_domain=merged.get("x-target-domains"),
        open_timeout=kwargs.get("open_timeout"),
        keys=sorted(merged.keys()),
    )
    return _original_websockets_connect(*args, **kwargs)


def _ensure_ws_connect_patched() -> None:
    global _ws_connect_patched
    if _ws_connect_patched:
        return
    websockets.connect = _traced_websocket_connect  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    _ws_connect_patched = True


@contextmanager
def _inject_headers_into_websockets(extra_headers: dict[str, str] | None = None):
    # Zendriver calls `websockets.connect(url, ...)` with no hook for headers.
    # Install a single process-wide wrapper once, then pass per-request headers
    # via ContextVar so concurrent requests do not overwrite each other.
    _ensure_ws_connect_patched()

    token = _ws_extra_headers_var.set(extra_headers or None)
    try:
        yield
    finally:
        _ws_extra_headers_var.reset(token)


async def _create_browser_from_cdp_websocket(
    *,
    browser_id: str,
    websocket_url: str,
    target_domain: str | None = None,
    config: Config | None = None,
) -> zd.Browser:
    parsed = urlparse(websocket_url)
    host = parsed.hostname or "127.0.0.1"
    host = f"[{host.strip('[]')}]" if ":" in host else host  # handle ipv6 addresses
    port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 80)

    if not config:
        config = Config(host=host, port=port, browser_executable_path="remote")

    config.host = host
    config.port = port

    instance = zd.Browser(config)
    instance.info = ContraDict({"webSocketDebuggerUrl": websocket_url}, silent=True)
    instance.connection = Connection(websocket_url, _owner=instance)

    async def _safe_handle_target_update(event: object) -> None:
        try:
            await instance._handle_target_update(event)  # type: ignore[reportPrivateUsage]
        except RuntimeError as exc:
            # zendriver may raise "coroutine raised StopIteration" for
            # out-of-order target lifecycle events in remote CDP sessions.
            if "StopIteration" in str(exc):
                logger.debug("Ignored transient target update race: {}", exc)
                return
            raise
        except StopIteration:
            logger.debug("Ignored transient target update race: StopIteration")

    extra_headers = build_chromefleet_headers(target_domain=target_domain)
    with (
        logfire.span(
            "cdp websocket connect {browser_id}",
            browser_id=browser_id,
            cdp_url=websocket_url,
        ),
        _inject_headers_into_websockets(extra_headers=extra_headers),
    ):
        if instance.config.autodiscover_targets:
            instance.connection.handlers[zd.cdp.target.TargetInfoChanged] = [  # type: ignore[reportUnknownMemberType]
                _safe_handle_target_update
            ]
            instance.connection.handlers[zd.cdp.target.TargetCreated] = [  # type: ignore[reportUnknownMemberType]
                _safe_handle_target_update
            ]
            instance.connection.handlers[zd.cdp.target.TargetDestroyed] = [  # type: ignore[reportUnknownMemberType]
                _safe_handle_target_update
            ]
            instance.connection.handlers[zd.cdp.target.TargetCrashed] = [  # type: ignore[reportUnknownMemberType]
                _safe_handle_target_update
            ]
            try:
                await asyncio.wait_for(
                    instance.connection.send(zd.cdp.target.set_discover_targets(discover=True)),
                    timeout=settings.CHROMEFLEET_CDP_HANDSHAKE_TIMEOUT_SECONDS,
                )
            except websockets.ConnectionClosedError as e:
                raise ConnectionError(
                    f"CDP WebSocket closed by remote (code={e.rcvd.code if e.rcvd else 'unknown'},"
                    f" reason={e.rcvd.reason if e.rcvd else ''!r}): browser_id={browser_id}"
                ) from e
            except asyncio.TimeoutError:
                raise ConnectionError(
                    f"CDP WebSocket handshake timed out after "
                    f"{settings.CHROMEFLEET_CDP_HANDSHAKE_TIMEOUT_SECONDS:g}s: "
                    f"browser_id={browser_id}"
                )

        try:
            await asyncio.wait_for(
                instance.update_targets(),
                timeout=settings.CHROMEFLEET_CDP_HANDSHAKE_TIMEOUT_SECONDS,
            )
        except websockets.ConnectionClosedError as e:
            raise ConnectionError(
                f"CDP WebSocket closed by remote (code={e.rcvd.code if e.rcvd else 'unknown'},"
                f" reason={e.rcvd.reason if e.rcvd else ''!r}): browser_id={browser_id}"
            ) from e
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"CDP WebSocket update_targets timed out after "
                f"{settings.CHROMEFLEET_CDP_HANDSHAKE_TIMEOUT_SECONDS:g}s: "
                f"browser_id={browser_id}"
            )
    util.get_registered_instances().add(instance)

    async def browser_atexit() -> None:
        if not instance.stopped:
            await instance.stop()
        await instance._cleanup_temporary_profile()  # type: ignore[reportPrivateUsage]

    asyncio_atexit.register(browser_atexit)  # type: ignore[reportUnknownMemberType]

    instance.id = browser_id  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    return instance


def find_browser_tab(browser: zd.Browser, target_id: str) -> zd.Tab | None:
    """Find a browser tab by its target ID.

    zendriver's ``Browser.update_targets`` populates ``self.targets`` with plain
    ``Connection`` objects for targets that already existed when the browser was
    attached (e.g. via ChromeFleet CDP), instead of ``Tab`` objects. Only targets
    created later through a ``TargetCreated`` event get promoted to ``Tab``.
    Since ``evaluate`` (and most page-level helpers) live on ``Tab`` and not on
    ``Connection``, calling ``page.evaluate(...)`` on such a target fails with
    ``'Connection' object has no attribute 'evaluate'``.

    Here we upgrade any matched ``Connection`` to a ``Tab`` (mirroring what
    ``_handle_target_update`` does for newly created targets) and replace it in
    ``browser.targets`` so subsequent lookups return the ``Tab`` as well.
    """
    for idx, target in enumerate(browser.targets):
        if target.target_id != target_id:
            continue
        if isinstance(target, zd.Tab):
            return target
        if target.target is None:
            return None
        tab = zd.Tab(target.websocket_url, target=target.target, browser=browser)
        browser.targets[idx] = tab
        return tab
    return None


def _setup_cdp_url(browser_id: str) -> str:
    cdp_base = settings.effective_chromefleet_url.replace("https://", "wss://").replace(
        "http://", "ws://"
    )
    return f"{cdp_base}/cdp/{browser_id}"


async def get_remote_browser_cdp_url(browser_id: str) -> str:
    await call_chromefleet_api("GET", browser_id, timeout=20.0)  # retries=3 default
    return _setup_cdp_url(browser_id)


async def get_remote_browser(browser_id: str) -> zd.Browser | None:
    logger.debug(f"Finding the ChromeFleet browser: {browser_id}")
    try:
        await call_chromefleet_api("GET", browser_id)
    except Exception:
        return None

    cdp_websocket_url = _setup_cdp_url(browser_id)
    logger.debug(f"Connecting to ChromeFleet CDP at {cdp_websocket_url}")
    browser = await _create_browser_from_cdp_websocket(
        browser_id=browser_id, websocket_url=cdp_websocket_url
    )
    return browser


async def create_remote_browser(
    browser_id: str = "",
    target_domain: str | None = None,
) -> zd.Browser:
    """
    Connect to a remote Chrome via ChromeFleet CDP.
    ChromeFleet auto-starts the browser on first CDP access if it doesn't exist.
    """
    if not browser_id:
        browser_id = generate(FRIENDLY_CHARS, 6)
    logger.info(f"Connecting to ChromeFleet browser: {browser_id}")
    cdp_websocket_url = _setup_cdp_url(browser_id)
    logger.debug(f"Connecting to ChromeFleet CDP at {cdp_websocket_url}")
    browser = await _create_browser_from_cdp_websocket(
        browser_id=browser_id, websocket_url=cdp_websocket_url, target_domain=target_domain
    )
    return browser


async def terminate_remote_browser(browser: zd.Browser) -> None:
    """Terminate an existing remote Chrome via ChromeFleet."""
    browser_id = cast(str, browser.id)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    logger.info(f"Terminating ChromeFleet browser: {browser_id}")
    # no need to raise for error (which would fail the whole process)
    await call_chromefleet_api("DELETE", browser_id, timeout=1.0, retries=0, raise_for_status=False)


_CREDENTIALS_BLOCK_SCRIPT = r"""
(() => {
  "use strict";

  if (!window.CredentialsContainer || !navigator.credentials) return;

  Object.defineProperty(CredentialsContainer.prototype, "get", {
    configurable: true,
    writable: true,
    value: function () {
      return Promise.reject(
        new DOMException("Credentials API blocked", "NotAllowedError")
      );
    },
  });
})();
"""


async def get_new_page(browser: zd.Browser) -> zd.Tab:
    page = await browser.get("about:blank", new_tab=True)

    async def handle_request(event: zd.cdp.fetch.RequestPaused) -> None:
        resource_type = event.resource_type
        request_url = event.request.url

        deny = resource_type in [
            zd.cdp.network.ResourceType.IMAGE,
            zd.cdp.network.ResourceType.MEDIA,
            zd.cdp.network.ResourceType.FONT,
        ]

        if not deny:
            try:
                await page.send(zd.cdp.fetch.continue_request(request_id=event.request_id))
            except (ProtocolException, websockets.ConnectionClosedError) as e:
                if isinstance(e, ProtocolException) and (
                    "Invalid state for continueInterceptedRequest" in str(e)
                    or "Invalid InterceptionId" in str(e)
                    or "Fetch domain is not enabled" in str(e)
                ):
                    logger.debug(
                        f"Request already processed or invalid interception ID: {request_url}"
                    )
                elif isinstance(e, websockets.ConnectionClosedError):
                    logger.debug(f"Page closed while continuing request: {request_url}")
                else:
                    raise
            return

        logger.trace(f" DENY resource: {request_url}")

        try:
            await page.send(
                zd.cdp.fetch.fail_request(
                    request_id=event.request_id,
                    error_reason=zd.cdp.network.ErrorReason.BLOCKED_BY_CLIENT,
                )
            )
        except (ProtocolException, websockets.ConnectionClosedError) as e:
            if isinstance(e, ProtocolException) and (
                "Invalid state for continueInterceptedRequest" in str(e)
                or "Invalid InterceptionId" in str(e)
                or "Fetch domain is not enabled" in str(e)
            ):
                logger.debug(f"Request already processed or invalid interception ID: {request_url}")
            elif isinstance(e, websockets.ConnectionClosedError):
                logger.debug(f"Page closed while blocking request: {request_url}")
            else:
                raise

    # Enable fetch domain to intercept requests. Will be overridden if proxy auth is set up.
    await page.send(zd.cdp.fetch.enable())
    page.add_handler(zd.cdp.fetch.RequestPaused, handle_request)  # type: ignore[reportUnknownMemberType]

    # Block the entire Credentials API before page for passkeys
    # Page domain must be enabled before addScriptToEvaluateOnNewDocument, otherwise CDP will reject the script
    await page.send(zd.cdp.page.enable())
    await page.send(
        zd.cdp.page.add_script_to_evaluate_on_new_document(
            source=_CREDENTIALS_BLOCK_SCRIPT, run_immediately=True
        )
    )

    return page


async def safe_close_page(page: zd.Tab) -> None:
    """Safely close a page by disabling fetch domain first to prevent orphaned tasks.

    When page.close() is called while fetch handlers are pending, it can leave
    orphaned tasks waiting for CDP responses that will never arrive. This function
    disables the fetch domain first to clean up handlers before closing.
    """
    try:
        # Disable fetch domain to cancel pending request handlers
        await page.send(zd.cdp.fetch.disable())
        logger.debug("Fetch domain disabled before page close")
    except (ProtocolException, websockets.ConnectionClosedError) as e:
        # Page/connection already closed, which is fine
        logger.debug(f"Could not disable fetch (connection already closed): {e}")
    except Exception as e:
        # Log but don't fail - we still want to close the page
        logger.warning(f"Unexpected error disabling fetch domain: {e}")

    try:
        target = getattr(page, "target", None)
        raw_target_id = getattr(target, "target_id", None) or getattr(page, "target_id", None)
        normalized_target_id = (
            zd.cdp.target.TargetID(raw_target_id.split("@", 1)[-1])
            if isinstance(raw_target_id, str)
            else None
        )

        if normalized_target_id:
            await page.send(zd.cdp.target.close_target(target_id=normalized_target_id))
        else:
            await page.close()
        logger.info("Page closed successfully")
    except Exception as e:
        logger.warning(f"Error closing page: {e}")


async def wait_for_ready_state(
    page: zd.Tab,
    timeout: int = 10,
) -> bool:
    """
    Waits for the page to reach a certain ready state (interactive or complete).
    :param timeout: The maximum number of seconds to wait.
    :type timeout: int
    :raises asyncio.TimeoutError: If the timeout is reached before the ready state is reached.
    :return: True if the ready state is reached.
    :rtype: bool
    """
    loop = asyncio.get_event_loop()
    start_time = loop.time()

    while True:
        state = await page.evaluate("document.readyState")
        if state == "interactive" or state == "complete":
            return True

        if loop.time() - start_time > timeout:
            raise asyncio.TimeoutError("time ran out while waiting for load page until %s" % state)

        await asyncio.sleep(0.1)


async def zen_navigate_with_retry(page: zd.Tab, url: str, wait_for_ready: bool = True) -> zd.Tab:
    """Navigate to URL with retry logic for resilient navigation.

    Args:
        page: Zendriver tab to navigate
        url: URL to navigate to
        wait_for_ready: Whether to wait for page ready state (default True).
            Set to False for simple pages that load instantly.

    Returns:
        The page after successful navigation

    Raises:
        Exception: If navigation fails after all retries
    """
    MAX_RETRIES = 3
    FIRST_TIMEOUT = 45  # seconds, extended for first attempt
    NORMAL_TIMEOUT = 30  # seconds, for retry attempts

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        timeout = FIRST_TIMEOUT if attempt == 0 else NORMAL_TIMEOUT
        try:

            async def navigate_and_wait() -> zd.Tab:
                _frame_id, _loader_id, error_text = await page.send(zd.cdp.page.navigate(url))

                # Check for navigation errors (connection refused, DNS failure, SSL errors, etc.)
                if error_text:
                    raise ConnectionError(f"Navigation failed: {error_text}")

                if not wait_for_ready:
                    return page

                # Wait for page to be interactive
                try:
                    await wait_for_ready_state(page)
                except Exception:
                    # If wait fails, that's okay - page might already be loaded
                    pass
                return page

            result = await asyncio.wait_for(navigate_and_wait(), timeout=timeout)
            return result
        except Exception as error:
            last_error = error
            if attempt < MAX_RETRIES - 1:
                logger.warning(
                    f"Navigation to {url} failed (attempt {attempt + 1}/{MAX_RETRIES}): {error}. "
                    f"Retrying in 1 second..."
                )
                await asyncio.sleep(1)
            else:
                logger.error(f"Failed to navigate to {url} after {MAX_RETRIES} attempts")

    # This should never be reached, but satisfies type checker
    raise last_error or Exception(f"Failed to navigate to {url}")


@dataclass
class ElementConfig:
    """Configuration for element typing operations."""

    # Typing configuration
    typing_clear_delay: float = 0.1
    typing_char_delay_min: float = 0.01
    typing_char_delay_max: float = 0.05
    action_delay_ms: float = 0


class Element:
    """Wrapper to handle both CSS and XPath selector differences for browser elements."""

    def __init__(
        self,
        element: zd.Element,
        css_selector: str | None = None,
        xpath_selector: str | None = None,
        config: ElementConfig | None = None,
    ):
        self.element = element
        self.tag = element.tag
        self.page = element.tab
        self.css_selector = css_selector
        self.xpath_selector = xpath_selector
        self.config = config or ElementConfig()

    async def inner_html(self) -> str:
        return await self.element.get_html()

    async def inner_text(self) -> str:
        return self.element.text_all

    async def is_visible(self) -> bool:
        try:
            selector = self.xpath_selector or self.css_selector
            if not selector:
                return False

            # Optimized: Single JavaScript evaluation with minimal operations
            escaped_selector = selector.replace("\\", "\\\\").replace('"', '\\"')

            if self.xpath_selector:
                # Use a more efficient check - combine all checks in one expression
                js_code = f"""
                    (() => {{
                        const el = document.evaluate("{escaped_selector}", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        return s.visibility !== "hidden" && s.display !== "none" && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
                    }})()
                """
            else:
                # Optimized: Single expression, no intermediate variables
                js_code = f"""
                    (() => {{
                        const el = document.querySelector("{escaped_selector}");
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        return s.visibility !== "hidden" && s.display !== "none" && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
                    }})()
                """

            return bool(await self.page.evaluate(js_code))
        except Exception as e:
            logger.debug(f"is_visible check failed: {e}")
            return False

    async def click(self) -> None:
        if self.config.action_delay_ms > 0:
            await asyncio.sleep(self.config.action_delay_ms / 1000)
        if self.css_selector:
            await self.css_click()
        else:
            await self.xpath_click()
        await asyncio.sleep(0.25)

    async def select_option(self, value: str) -> None:
        # Only support CSS selectors for now
        if not self.css_selector:
            logger.warning("Cannot perform CSS select_option: no css_selector available")
            return
        logger.debug(f"Attempting JavaScript CSS select_option for {self.css_selector}")
        try:
            escaped_selector = self.css_selector.replace("\\", "\\\\").replace('"', '\\"')
            value_selector = f"option[value='{value}']"
            js_code = f"""
                (() => {{
                    const select = document.querySelector("{escaped_selector}");
                    const option = select?.querySelector("{value_selector}");
                    if (!select || !option) return false;

                    // Scroll into view
                    select.scrollIntoView({{ block: "center" }});

                    // Open dropdown (if needed)
                    select.dispatchEvent(new PointerEvent("pointerdown", {{ bubbles: true }}));
                    select.dispatchEvent(new PointerEvent("pointerup", {{ bubbles: true }}));
                    select.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));

                    // Select the option
                    option.selected = true;

                    // Trigger change event
                    select.dispatchEvent(new Event("change", {{ bubbles: true }}));

                    return true;
                }})();
            """
            result = await self.page.evaluate(js_code)
            if result:
                logger.info(f"JavaScript CSS select_option succeeded for {self.css_selector}")
                return
            else:
                logger.warning(
                    f"JavaScript CSS select_option could not find element {self.css_selector}"
                )
        except Exception as js_error:
            logger.error(f"JavaScript CSS select_option failed: {js_error}")

        await asyncio.sleep(0.25)

    async def check(self) -> None:
        logger.error("TODO: Element#check")
        await asyncio.sleep(0.25)

    async def _refresh_node(self) -> bool:
        """Re-resolve the underlying zendriver node from this element's selector.

        The CDP backendNodeId cached in ``self.element`` becomes stale when the page
        re-renders the node (common on OTP/verification inputs that auto-advance).
        Re-selecting swaps in a live node so a retried CDP op can resolve it.
        """
        try:
            if self.xpath_selector:
                elements = await self.page.xpath(self.xpath_selector, 0)
                node = elements[0] if elements else None
            elif self.css_selector:
                node = await self.page.select(self.css_selector)
            else:
                return False
        except Exception:
            return False
        if node is None:
            return False
        self.element = node
        self.tag = node.tag
        return True

    async def _clear_input(self) -> None:
        for attempt in range(2):
            try:
                await self.element.clear_input_by_deleting()
                await asyncio.sleep(self.config.typing_clear_delay)
                await self.element.clear_input()
                await asyncio.sleep(self.config.typing_clear_delay)
                return
            except ProtocolException as e:
                if attempt == 0 and await self._refresh_node():
                    logger.warning(f"clear_input hit stale node; refreshed and retrying: {e}")
                    continue
                raise

    async def type_text(self, text: str) -> None:
        if self.config.action_delay_ms > 0:
            await asyncio.sleep(self.config.action_delay_ms / 1000)
        await self._clear_input()
        for char in text:
            await self.element.send_keys(char)
            await asyncio.sleep(
                random.uniform(self.config.typing_char_delay_min, self.config.typing_char_delay_max)
            )

    async def css_click(self) -> None:
        if not self.css_selector:
            logger.warning("Cannot perform CSS click: no css_selector available")
            return
        logger.debug(f"Attempting JavaScript CSS click for {self.css_selector}")
        try:
            escaped_selector = self.css_selector.replace("\\", "\\\\").replace('"', '\\"')
            js_code = f"""
            (() => {{
                const selector = "{escaped_selector}";
                function findInDocument(doc) {{
                    try {{
                        const el = doc.querySelector(selector);
                        if (el) return el;
                    }} catch (e) {{
                        // Cross-origin iframe → skip
                    }}
                    // Look inside all iframes of this document
                    const iframes = doc.querySelectorAll("iframe");
                    for (const frame of iframes) {{
                        try {{
                            const childDoc = frame.contentDocument || frame.contentWindow.document;
                            const found = findInDocument(childDoc);   // recursion
                            if (found) return found;
                        }} catch (e) {{
                            // Cross-origin iframe → skip
                        }}
                    }}
                    return null;
                }}
                const element = findInDocument(document);
                if (!element) return false;
                element.scrollIntoView({{ block: "center" }});
                element.dispatchEvent(new PointerEvent("pointerdown", {{ bubbles: true }}));
                element.dispatchEvent(new PointerEvent("pointerup", {{ bubbles: true }}));
                element.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
                return true;
            }})()
            """
            result = await self.page.evaluate(js_code)
            if result:
                logger.info(f"JavaScript CSS click succeeded for {self.css_selector}")
                return
            else:
                logger.warning(f"JavaScript CSS click could not find element {self.css_selector}")
        except Exception as js_error:
            logger.error(f"JavaScript CSS click failed: {js_error}")

    async def xpath_click(self) -> None:
        if not self.xpath_selector:
            logger.warning(f"Cannot perform XPath click: no xpath_selector available")
            return
        logger.debug(f"Attempting JavaScript XPath click for {self.xpath_selector}")
        try:
            escaped_selector = self.xpath_selector.replace("\\", "\\\\").replace('"', '\\"')
            js_code = f"""
            (() => {{
                let element = document.evaluate("{escaped_selector}", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                if (!element) return false;
                element.scrollIntoView({{ block: "center" }});
                element.dispatchEvent(new PointerEvent("pointerdown", {{ bubbles: true }}));
                element.dispatchEvent(new PointerEvent("pointerup", {{ bubbles: true }}));
                element.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
                return true;
            }})()
            """
            result = await self.page.evaluate(js_code)
            if result:
                logger.info(f"JavaScript XPath click succeeded for {self.xpath_selector}")
                return
            else:
                logger.warning(
                    f"JavaScript XPath click could not find element {self.xpath_selector}"
                )
        except Exception as js_error:
            logger.error(f"JavaScript XPath click failed: {js_error}")


async def get_url(page: zd.Tab) -> str | None:
    current_url = await page.evaluate("window.location.href", await_promise=True)
    if current_url:
        return str(current_url)
    return None


async def page_query_selector(
    page: zd.Tab,
    selector: str,
    timeout: float = 0,
    iframe_selector: str | None = None,
    skip_visibility_check: bool = False,
    config: ElementConfig | None = None,
) -> Element | None:
    try:
        if selector.startswith("//"):
            elements = await page.xpath(selector, timeout)
            if elements and len(elements) > 0:
                element = Element(elements[0], xpath_selector=selector, config=config)
                if skip_visibility_check or await element.is_visible():
                    return element
            return None

        if iframe_selector is not None:
            element = await page.select_all(selector, timeout=timeout, include_frames=True)
            if element and len(element) > 0:
                element = Element(element[0], css_selector=selector, config=config)
                if skip_visibility_check or await element.is_visible():
                    return element
        else:
            element = await page.select(selector, timeout=timeout)
            if element:
                element = Element(element, css_selector=selector, config=config)
                if skip_visibility_check or await element.is_visible():
                    return element
        return None
    except (asyncio.TimeoutError, Exception):
        return None


async def page_batch_extract(
    page: zd.Tab, queries: list[dict[str, object]]
) -> dict[str, dict[str, object]] | None:
    if len(queries) == 0:
        return {}

    payload = json.dumps(queries)
    js_code = f"""
    (() => {{
        const queries = {payload};
        const output = {{}};

        function isVisible(element) {{
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return (
                style.visibility !== "hidden" &&
                style.display !== "none" &&
                rect.width > 0 &&
                rect.height > 0
            );
        }}

        function findCss(selector, doc, iframeSelector) {{
            try {{
                if (iframeSelector) {{
                    const iframe = doc.querySelector(iframeSelector);
                    if (iframe) {{
                        const childDoc = iframe.contentDocument || iframe.contentWindow.document;
                        const directIframeMatch = childDoc.querySelector(selector);
                        if (directIframeMatch) {{
                            return directIframeMatch;
                        }}
                        return findCss(selector, childDoc, null);
                    }}
                }}
            }} catch (error) {{
                return null;
            }}

            try {{
                const direct = doc.querySelector(selector);
                if (direct) return direct;
            }} catch (error) {{
                return null;
            }}

            const iframes = doc.querySelectorAll("iframe");
            for (const iframe of iframes) {{
                try {{
                    const childDoc = iframe.contentDocument || iframe.contentWindow.document;
                    const nested = findCss(selector, childDoc, null);
                    if (nested) return nested;
                }} catch (error) {{}}
            }}

            return null;
        }}

        for (const query of queries) {{
            const selector = query?.selector;
            const iframeSelector = query?.iframe_selector;
            const queryKey = query?.query_key;
            if (typeof selector !== "string" || selector.length === 0) {{
                continue;
            }}
            if (typeof queryKey !== "string" || queryKey.length === 0) {{
                continue;
            }}

            let element = null;

            if (selector.startsWith("//")) {{
                try {{
                    element = document.evaluate(
                        selector,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null
                    ).singleNodeValue;
                }} catch (error) {{
                    element = null;
                }}
            }} else {{
                const resolvedIframeSelector = typeof iframeSelector === "string" &&
                    iframeSelector.length > 0
                    ? iframeSelector
                    : null;
                element = findCss(selector, document, resolvedIframeSelector);
            }}

            if (!element || !isVisible(element)) {{
                output[queryKey] = {{ found: false }};
                continue;
            }}

            const tag = String(element.tagName || "").toLowerCase();
            const item = {{ found: true, tag }};

            if (query?.wants_html) {{
                item.html = element.innerHTML || "";
            }}
            if (query?.wants_text) {{
                item.text = element.innerText || "";
            }}
            if (query?.wants_value && ["input", "textarea", "select"].includes(tag)) {{
                const value = element.value;
                if (typeof value === "string") {{
                    item.value = value;
                }} else if (value == null) {{
                    item.value = "";
                }} else {{
                    item.value = String(value);
                }}
            }}

            output[queryKey] = item;
        }}

        return output;
    }})()
    """

    try:
        result = await page.evaluate(js_code)
        if isinstance(result, dict):
            return cast(dict[str, dict[str, object]], result)
        logger.warning(f"Batch extract returned unexpected type: {type(result)}")
        sentry_sdk.capture_message(f"Batch extract returned unexpected type: {type(result)}")
    except Exception as error:
        logger.warning(f"Batch extract failed: {error}")
        sentry_sdk.capture_exception(error)
    return None


T = TypeVar("T")


async def retry_with_navigation(
    tab: zd.Tab,
    operation: Callable[[], Awaitable[T]],
    navigation_url: str | None = None,
    max_retries: int = 3,
    timeout_seconds: float | None = None,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    default_on_max_retries: T | None = None,
    re_raise_on_max_retries: bool = False,
    operation_name: str = "operation",
) -> T:
    for attempt in range(1, max_retries + 1):
        logger.info(f"{operation_name} attempt {attempt}/{max_retries}")

        try:
            if navigation_url:
                await zen_navigate_with_retry(tab, navigation_url, wait_for_ready=False)

            if timeout_seconds is not None:
                result = await asyncio.wait_for(operation(), timeout=timeout_seconds)
            else:
                result = await operation()

            logger.info(f"Successfully completed {operation_name}.")
            return result

        except exceptions as e:
            error_type = type(e).__name__
            logger.warning(
                f"{operation_name} attempt {attempt}/{max_retries} failed with {error_type}: {e}"
            )

            if attempt == max_retries:
                logger.error(f"Max retries reached for {operation_name}.")
                if re_raise_on_max_retries:
                    raise
                if default_on_max_retries is not None:
                    return default_on_max_retries
                raise ValueError(
                    f"Max retries reached for {operation_name} and no default value or re-raise specified"
                )

            logger.info(f"Retrying {operation_name}...")

    if default_on_max_retries is not None:
        return default_on_max_retries
    raise RuntimeError(f"Unexpected end of retry loop for {operation_name}")


async def page_batch_actions(page: zd.Tab, actions: list[dict[str, str]]) -> dict[str, bool] | None:
    if len(actions) == 0:
        return {}

    payload = json.dumps(actions)
    js_code = f"""
    (async () => {{
        const actions = {payload};
        const output = {{}};

        function findCss(selector, doc) {{
            try {{
                const direct = doc.querySelector(selector);
                if (direct) return direct;
            }} catch (error) {{
                return null;
            }}

            const iframes = doc.querySelectorAll("iframe");
            for (const iframe of iframes) {{
                try {{
                    const childDoc = iframe.contentDocument || iframe.contentWindow.document;
                    const nested = findCss(selector, childDoc);
                    if (nested) return nested;
                }} catch (error) {{
                    // Cross-origin iframe.
                }}
            }}
            return null;
        }}

        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype,
            "value"
        )?.set;
        const nativeTextAreaValueSetter = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype,
            "value"
        )?.set;

        const setNativeValue = (el, nextValue) => {{
            if (el instanceof HTMLInputElement && nativeInputValueSetter) {{
                nativeInputValueSetter.call(el, nextValue);
            }} else if (el instanceof HTMLTextAreaElement && nativeTextAreaValueSetter) {{
                nativeTextAreaValueSetter.call(el, nextValue);
            }} else {{
                el.value = nextValue;
            }}
        }};

        async function fillInput(el, value, typingDelayMs) {{
            el.focus();
            setNativeValue(el, "");
            el.dispatchEvent(new InputEvent("input", {{
                bubbles: true,
                inputType: "deleteContentBackward",
                data: null,
            }}));
            let currentValue = "";
            for (const char of value) {{
                el.dispatchEvent(new KeyboardEvent("keydown", {{ key: char, bubbles: true }}));
                currentValue += char;
                setNativeValue(el, currentValue);
                el.dispatchEvent(new InputEvent("input", {{
                    bubbles: true,
                    inputType: "insertText",
                    data: char,
                }}));
                el.dispatchEvent(new KeyboardEvent("keyup", {{ key: char, bubbles: true }}));
                if (typingDelayMs > 0) await sleep(typingDelayMs);
            }}
            el.dispatchEvent(new Event("change", {{ bubbles: true }}));
            el.dispatchEvent(new Event("blur", {{ bubbles: true }}));
        }}

        async function setValueWithPoll(selector, value, typingDelayMs, timeout) {{
            const el = document.querySelector(selector);
            if (!el) return {{ success: false, reason: "element not found" }};
            await fillInput(el, value, typingDelayMs);

            let refillCount = 0;
            let stableCount = 0;
            const deadline = Date.now() + timeout;
            while (Date.now() < deadline) {{
                await sleep(50);
                const inputEl = document.querySelector(selector);
                if (!inputEl) {{ stableCount = 0; continue; }}
                if (inputEl.value !== value) {{
                    stableCount = 0;
                    refillCount++;
                    console.log(`set_value: refill #${{refillCount}}, value was "${{inputEl.value}}", expected "${{value}}"`);
                    await fillInput(inputEl, value, typingDelayMs);
                    continue;
                }}
                stableCount++;
                if (stableCount >= 5) {{
                    return {{
                        success: true,
                        actualValue: inputEl.value,
                        refillCount: refillCount,
                    }};
                }}
            }}
            return {{ success: false, reason: "timeout" }};
        }}

        for (const [index, action] of actions.entries()) {{
            const actionDelayMs = Number(action?.action_delay_ms) || 0;
            if (index > 0 && actionDelayMs > 0) {{
                await new Promise(resolve => setTimeout(resolve, actionDelayMs));
            }}
            const key = action?.key;
            const kind = action?.kind;
            const selector = action?.selector;
            if (typeof key !== "string" || key.length === 0) {{
                continue;
            }}
            if (typeof selector !== "string" || selector.length === 0) {{
                output[key] = false;
                continue;
            }}

            let element = null;
            if (selector.startsWith("//")) {{
                try {{
                    element = document.evaluate(
                        selector,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null
                    ).singleNodeValue;
                }} catch (error) {{
                    element = null;
                }}
            }} else {{
                element = findCss(selector, document);
            }}

            if (!element) {{
                output[key] = false;
                continue;
            }}

            try {{
                if (kind === "click") {{
                    element.click();
                    output[key] = true;
                }} else if (kind === "set_value") {{
                    const value = typeof action?.value === "string" ? action.value : "";
                    const requestedDelay = Number(action?.typing_delay_ms);
                    const typingDelayMs = Number.isFinite(requestedDelay)
                        ? Math.max(0, Math.min(250, requestedDelay))
                        : 25;
                    const timeout = Number(action?.timeout_ms) || 5000;
                    if (actionDelayMs > 0) await sleep(actionDelayMs);
                    output[key] = await setValueWithPoll(selector, value, typingDelayMs, timeout);
                }} else {{
                    output[key] = false;
                }}
            }} catch (error) {{
                output[key] = false;
            }}
        }}

        return output;
    }})()
    """

    try:
        result = await page.evaluate(js_code, await_promise=True)
        if isinstance(result, dict):
            output: dict[str, bool] = {}
            for k, v in result.items():  # pyright: ignore[reportUnknownVariableType]
                if isinstance(v, dict):
                    vd: dict[str, object] = v  # pyright: ignore[reportUnknownVariableType]
                    logger.info(
                        f"set_value debug: key={k} "
                        f"success={vd.get('success')} actualValue={vd.get('actualValue')!r} "
                        f"refillCount={vd.get('refillCount')} reason={vd.get('reason')!r}"
                    )
                    output[str(k)] = vd.get("success") is True  # pyright: ignore[reportUnknownArgumentType]
                else:
                    output[str(k)] = bool(v)  # pyright: ignore[reportUnknownArgumentType]
            logger.info(f"Batch actions result: {output}")
            return output
    except Exception as error:
        logger.error(f"Batch actions failed: {error}")
    return None
