import asyncio
import json
import re
import urllib.parse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from glob import glob
from pathlib import Path
from typing import Any, Callable, Coroutine, cast

import sentry_sdk
import zendriver as zd
from bs4 import BeautifulSoup
from bs4.element import Tag
from loguru import logger
from nanoid import generate

from getgather.browser import (
    get_new_page,
    page_batch_extract,
    page_query_selector,
    wait_for_ready_state,
    zen_navigate_with_retry,
)
from getgather.config import settings


@dataclass
class Pattern:
    name: str
    pattern: BeautifulSoup


@dataclass
class Match:
    name: str
    priority: int
    distilled: str


ConversionResult = list[dict[str, str | list[str]]]

ErrorReporter = Callable[..., Coroutine[Any, Any, None]]

NETWORK_ERROR_PATTERNS = (
    "err-timed-out",
    "err-ssl-protocol-error",
    "err-tunnel-connection-failed",
    "err-proxy-connection-failed",
    "err-service-unavailable",
    "err-connection-closed",
    "err-empty-response",
    "err-http-protocol-error",
    "amazon-something-error-ca",
)


def get_selector(input_selector: str | None) -> tuple[str | None, str | None]:
    pattern = r"^(iframe(?:[^\s]*\[[^\]]+\]|[^\s]+))\s+(.+)$"
    if not input_selector:
        return None, None
    match = re.match(pattern, input_selector)
    if not match:
        return input_selector, None
    return match.group(2), match.group(1)


def _first_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        items = cast(list[Any], value)
        for item in items:
            if isinstance(item, str):
                return item
    return None


def get_match_attr(el: Tag) -> str | None:
    """Return the rb-match (or gg-match) selector value, coerced to a single string."""
    return _first_str(el.get("rb-match")) or _first_str(el.get("gg-match"))


def get_match_html_attr(el: Tag) -> str | None:
    """Return the rb-match-html (or gg-match-html) selector value, coerced to a single string."""
    return _first_str(el.get("rb-match-html")) or _first_str(el.get("gg-match-html"))


def get_domain_attr(el: Tag) -> str | None:
    """Return the rb-domain (or gg-domain) value, coerced to a single string."""
    return _first_str(el.get("rb-domain")) or _first_str(el.get("gg-domain"))


def get_priority_attr(el: Tag) -> str | None:
    """Return the rb-priority (or gg-priority) value, coerced to a single string."""
    return _first_str(el.get("rb-priority")) or _first_str(el.get("gg-priority"))


def get_convert_attr(el: Tag) -> str | None:
    """Return the rb-convert (or gg-convert) value, coerced to a single string."""
    return _first_str(el.get("rb-convert")) or _first_str(el.get("gg-convert"))


def get_error_attr(el: Tag) -> str | None:
    """Return the rb-error (or gg-error) value, coerced to a single string."""
    return _first_str(el.get("rb-error")) or _first_str(el.get("gg-error"))


def get_stop_attr(el: Tag) -> str | None:
    """Return the rb-stop (or gg-stop) value, coerced to a single string.

    Unlike value-bearing attributes, stop is a flag (valueless when present),
    so presence is checked via ``el.attrs`` rather than truthiness.
    """
    if "rb-stop" in el.attrs:
        return _first_str(el.get("rb-stop"))
    return _first_str(el.get("gg-stop"))


def get_optional_attr(el: Tag) -> str | None:
    """Return the rb-optional (or gg-optional) value, coerced to a single string.

    Like stop, optional is a flag (valueless when present), so presence is
    checked via ``el.attrs`` rather than truthiness.
    """
    if "rb-optional" in el.attrs:
        return _first_str(el.get("rb-optional"))
    return _first_str(el.get("gg-optional"))


def find_stop_elements(pattern: BeautifulSoup) -> list[Tag]:
    """Return elements carrying rb-stop (or gg-stop), deduped in document order."""
    seen: set[int] = set()
    out: list[Tag] = []
    for name in ("rb-stop", "gg-stop"):
        for el in pattern.find_all(attrs={name: True}):
            if isinstance(el, Tag) and id(el) not in seen:
                seen.add(id(el))
                out.append(el)
    return out


def find_error_elements(pattern: BeautifulSoup) -> list[Tag]:
    """Return elements carrying rb-error (or gg-error), deduped in document order."""
    seen: set[int] = set()
    out: list[Tag] = []
    for name in ("rb-error", "gg-error"):
        for el in pattern.find_all(attrs={name: True}):
            if isinstance(el, Tag) and id(el) not in seen:
                seen.add(id(el))
                out.append(el)
    return out


def find_match_elements(pattern: BeautifulSoup) -> list[Tag]:
    """Return elements carrying rb-match/rb-match-html (or gg-match/gg-match-html), deduped in document order."""
    seen: set[int] = set()
    out: list[Tag] = []
    for name in ("rb-match", "rb-match-html", "gg-match", "gg-match-html"):
        for el in pattern.find_all(attrs={name: True}):
            if isinstance(el, Tag) and id(el) not in seen:
                seen.add(id(el))
                out.append(el)
    return out


def extract_value(item: Tag, attribute: str | None = None) -> str:
    """Extract text or attribute value from a BeautifulSoup Tag."""
    if attribute:
        value = item.get(attribute)
        if isinstance(value, list):
            value = value[0] if value else ""
        return value.strip() if isinstance(value, str) else ""
    return item.get_text(strip=True)


def _load_converter_from_file(json_path: Path) -> dict[str, Any] | None:
    """Load converter configuration from a JSON file."""
    if not json_path.exists():
        return None

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return cast(dict[str, Any], json.load(f))
    except Exception as error:
        logger.warning(f"Failed to load converter from {json_path}: {error}")
        return None


async def convert(distilled: str, pattern_path: str | None = None):
    """Convert distilled HTML to structured data"""
    document = BeautifulSoup(distilled, "html.parser")
    converter = None

    if pattern_path:
        stops = find_stop_elements(document)
        for stop in stops:
            gg_convert = get_convert_attr(stop)
            if gg_convert and gg_convert.strip():
                pattern_dir = Path(pattern_path).parent
                json_path = pattern_dir / gg_convert.strip()
                logger.info(f"Loading converter from gg-convert: {json_path}")
                converter = _load_converter_from_file(json_path)
                if converter:
                    logger.info(f"Loaded converter from {json_path}")
                    break

    if converter is None:
        snippet = document.find("script", {"type": "application/json"})
        if snippet:
            script_content = snippet.get_text().strip()
            if script_content:
                logger.info("Found converter in HTML script tag")
                try:
                    converter = json.loads(script_content)
                except Exception as error:
                    logger.error(f"Failed to parse converter from HTML: {error}")
                    return None

    if converter is None:
        logger.debug("No converter found")
        return None

    # Perform conversion
    try:
        rows_selector = converter.get("rows", "")
        if not isinstance(rows_selector, str) or not rows_selector:
            logger.warning("Converter missing 'rows' selector")
            return None

        raw_columns = converter.get("columns", [])
        if not isinstance(raw_columns, list):
            logger.warning("Converter 'columns' must be a list")
            return None
        columns = cast(list[dict[str, Any]], raw_columns)

        logger.info(f"Converting using converter with {len(columns)} columns")
        rows = document.select(str(rows_selector))
        logger.info(f"Found {len(rows)} rows")

        converted: ConversionResult = []
        for el in rows:
            kv: dict[str, str | list[str]] = {}
            for col_dict in columns:
                name = col_dict.get("name")
                selector = col_dict.get("selector")
                if not name or not selector:
                    continue

                attribute = col_dict.get("attribute")
                kind = col_dict.get("kind")

                if kind == "list":
                    items = el.select(str(selector))
                    kv[name] = [extract_value(item, attribute) for item in items]
                else:
                    item = el.select_one(str(selector))
                    if item:
                        kv[name] = extract_value(item, attribute)

            if kv:
                converted.append(kv)

        logger.info(f"Conversion done: {len(converted)} entries")
        return converted
    except Exception as error:
        logger.error(f"Conversion error: {error}")
        return None


async def terminate(distilled: str) -> bool:
    document = BeautifulSoup(distilled, "html.parser")
    stops = find_stop_elements(document)
    if len(stops) > 0:
        logger.info("Found stop elements, terminating session...")
        return True
    return False


async def get_error(distilled: str) -> str | None:
    document = BeautifulSoup(distilled, "html.parser")
    error_elements = find_error_elements(document)
    for error_element in error_elements:
        error_value = get_error_attr(error_element)
        if error_value:
            logger.info(f"Found error element: {error_value}")
            return error_value
    return None


# Parsing the ~470 pattern files costs ~3.5s of blocking CPU on the event loop, and
# every MCP tool call used to redo it. Sharing one parse is safe because the only
# consumer (run_distillation_loop) deepcopies each tree before mutating it.
# Trade-off: pattern files are read once per process, so edits need a restart.
@lru_cache(maxsize=8)
def load_distillation_patterns(path: str) -> list[Pattern]:
    patterns: list[Pattern] = []
    for name in glob(path, recursive=True):
        with open(name, "r", encoding="utf-8") as f:
            content = f.read()
        patterns.append(Pattern(name=name, pattern=BeautifulSoup(content, "html.parser")))
    return patterns


def _safe_fragment(value: str) -> str:
    """Convert a value to a safe filename fragment."""
    fragment = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return fragment or "distill"


async def capture_page_artifacts(
    page: zd.Tab,  # type: ignore[name-defined]
    *,
    identifier: str,
    prefix: str,
    capture_html: bool = True,
) -> tuple[Path, Path | None, str | None]:
    """Capture a screenshot (and optional HTML) for debugging/triage."""

    settings.screenshots_dir.mkdir(parents=True, exist_ok=True)

    base_identifier = _safe_fragment(identifier)
    base_prefix = _safe_fragment(prefix)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    token = generate(size=5)
    filename = f"{base_identifier}_{base_prefix}_{timestamp}_{token}.png"
    screenshot_path = settings.screenshots_dir / filename

    await page.save_screenshot(filename=str(screenshot_path), full_page=True)  # type: ignore[attr-defined]

    html_path: Path | None = None
    html_content: str | None = None
    if capture_html:
        try:
            html_content = await page.get_content()  # type: ignore[attr-defined]
        except Exception as exc:  # ignore navigation races during capture
            logger.debug(f"⚠️ Can't capture page content during navigation: {exc}")
        else:
            html_path = screenshot_path.with_suffix(".html")
            html_path.write_text(html_content, encoding="utf-8")

    logger.debug(
        "📸 Distill artifact saved",
        extra={
            "screenshot": f"file://{screenshot_path}",
            "html": f"file://{html_path}" if html_path else None,
        },
    )

    return screenshot_path, html_path, html_content


async def zen_report_distill_error(
    *,
    error: Exception,
    page: zd.Tab | None,  # type: ignore[name-defined]
    profile_id: str,
    location: str,
    hostname: str,
    iteration: int,
) -> None:
    screenshot_path: Path | None = None
    html_path: Path | None = None

    if page:
        try:
            screenshot_path, html_path, _ = await capture_page_artifacts(
                page,
                identifier=profile_id,
                prefix="distill_error",
            )
        except Exception as capture_error:
            logger.warning(f"Failed to capture distillation artifacts: {capture_error}")

    context: dict[str, Any] = {
        "location": location,
        "hostname": hostname,
        "iteration": iteration,
    }

    logger.error(
        "Distillation error",
        extra={
            "profile_id": profile_id,
            "location": location,
            "iteration": iteration,
            "screenshot": f"file://{screenshot_path}" if screenshot_path else None,
        },
    )

    if settings.SENTRY_DSN:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_context("distill", context)
            if screenshot_path:
                scope.add_attachment(
                    filename=screenshot_path.name,
                    path=str(screenshot_path),
                )
            if html_path:
                scope.add_attachment(
                    filename=html_path.name,
                    path=str(html_path),
                )

            sentry_sdk.capture_exception(error)


def make_error_reporter(browser: zd.Browser, location: str | None = None) -> ErrorReporter:
    profile_id = cast(str, browser.id)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    async def _report(
        *,
        error: Exception,
        page: zd.Tab | None,
        hostname: str,
        iteration: int,
    ) -> None:
        await zen_report_distill_error(
            error=error,
            page=page,
            profile_id=profile_id,
            location=location or "",
            hostname=hostname,
            iteration=iteration,
        )

    return _report


async def distill(
    hostname: str | None, page: zd.Tab, patterns: list[Pattern], reload_on_error: bool = True
) -> Match | None:
    result: list[tuple[Match, list[str]]] = []
    pattern_runs: list[tuple[str, int, BeautifulSoup, list[dict[str, object]]]] = []
    all_batch_queries: list[dict[str, object]] = []
    next_query_key = 0

    for item in patterns:
        name = item.name
        pattern = deepcopy(item.pattern)

        root = pattern.find("html")
        gg_priority = get_priority_attr(root) if isinstance(root, Tag) else None
        try:
            priority = int(str(gg_priority or "-1").lstrip("= "))
        except ValueError:
            priority = -1
        domain = get_domain_attr(root) if isinstance(root, Tag) else None

        if domain and hostname:
            local = "localhost" in hostname or "127.0.0.1" in hostname
            if not local and domain.lower() not in hostname.lower():
                logger.trace(f"Skipping {name} due to mismatched domain {domain}")
                continue

        logger.debug(f"Checking {name} with priority {priority}")

        targets = find_match_elements(pattern)
        target_specs: list[dict[str, object]] = []

        for target in targets:
            html_attr = get_match_html_attr(target)
            selector, iframe_selector = get_selector(
                str(html_attr if html_attr else get_match_attr(target))
            )
            if not selector:
                continue

            is_html = html_attr is not None
            query_key = str(next_query_key)
            next_query_key += 1
            optional = get_optional_attr(target) is not None

            target_specs.append({
                "target": target,
                "selector": selector,
                "iframe_selector": iframe_selector,
                "html": is_html,
                "optional": optional,
                "query_key": query_key,
            })
            all_batch_queries.append({
                "query_key": query_key,
                "selector": selector,
                "iframe_selector": iframe_selector,
                "wants_html": is_html,
                "wants_text": not is_html,
                "wants_value": not is_html,
            })

        pattern_runs.append((name, priority, pattern, target_specs))

    safe_results: dict[str, dict[str, object]] = {}
    if all_batch_queries:
        batch_results = await page_batch_extract(page, all_batch_queries)
        safe_results = batch_results if isinstance(batch_results, dict) else {}

    for name, priority, pattern, target_specs in pattern_runs:
        found = True
        match_count = 0
        optional_visible_text: list[str] = []

        for spec in target_specs:
            target = spec.get("target")
            selector = spec.get("selector")
            query_key = spec.get("query_key")
            html = bool(spec.get("html"))
            optional = bool(spec.get("optional"))

            if not isinstance(target, Tag):
                continue
            if not isinstance(selector, str) or not isinstance(query_key, str):
                continue

            source = safe_results.get(query_key, {})
            source_found = bool(source.get("found"))

            if source_found:
                if optional:
                    text_content = ""
                    raw_text = source.get("text")
                    if isinstance(raw_text, str):
                        text_content = raw_text.strip()
                    if not text_content:
                        raw_value = source.get("value")
                        if isinstance(raw_value, str):
                            text_content = raw_value.strip()
                    if not text_content:
                        raw_html = source.get("html")
                        if isinstance(raw_html, str):
                            text_content = BeautifulSoup(raw_html, "html.parser").get_text(
                                " ", strip=True
                            )
                    if text_content:
                        optional_visible_text.append(text_content)
                if html:
                    target.clear()
                    html_content = source.get("html", "")
                    fragment = BeautifulSoup("<div>" + str(html_content) + "</div>", "html.parser")
                    if fragment.div:
                        for child in list(fragment.div.children):
                            child.extract()
                            target.append(child)
                else:
                    raw_text = source.get("text")
                    if isinstance(raw_text, str) and raw_text:
                        target.string = raw_text.strip()
                    tag = str(source.get("tag", "")).lower()
                    input_type = str(target.get("type", "")).lower()
                    if tag in ["input", "textarea", "select"] and input_type != "radio":
                        value = source.get("value", "")
                        target["value"] = value if isinstance(value, str) else ""
                match_count += 1
                continue

            if optional:
                logger.debug(f"Optional {selector} has no match")
                target.extract()
                continue
            found = False

        if found and match_count > 0:
            distilled = str(pattern)
            result.append((
                Match(
                    name=name,
                    priority=priority,
                    distilled=distilled,
                ),
                optional_visible_text,
            ))

    result = sorted(result, key=lambda x: x[0].priority)

    if len(result) == 0:
        logger.debug("No matches found")
        return None
    else:
        logger.debug(f"Number of matches: {len(result)}")
        for item, _optional_visible_text in result:
            logger.debug(f" - {item.name} with priority {item.priority}")
        match, optional_visible_text = result[0]

        browser_id = getattr(getattr(page, "browser", None), "id", None)

        logger.bind(
            event="distill_best_match",
            best_match_name=match.name,
            best_match_priority=match.priority,
            best_match_optional_visible_text=optional_visible_text,
            hostname=hostname,
            browser_id=browser_id,
        ).info("Best match selected")

        if reload_on_error and any(pattern in match.name for pattern in NETWORK_ERROR_PATTERNS):
            logger.info(f"Error pattern detected: {match.name}")
            try:
                await page.send(zd.cdp.page.reload())
                await wait_for_ready_state(page)
            except Exception as e:
                logger.warning(f"Failed to reload page: {e}")
            logger.info("Retrying distillation after error...")
            return await distill(hostname, page, patterns, reload_on_error=False)
        return match


async def autoclick(page: zd.Tab, distilled: str, expr: str):
    document = BeautifulSoup(distilled, "html.parser")
    elements = document.select(expr)
    for el in elements:
        selector, iframe_selector = get_selector(str(get_match_attr(el)))
        if selector:
            target = await page_query_selector(page, selector, iframe_selector=iframe_selector)
            if target:
                logger.debug(f"Clicking {selector}")
                await target.click()
            else:
                logger.warning(f"Selector {selector} not found, can't click on it")


async def run_distillation_loop(
    *,
    location: str | None = None,
    patterns: list[Pattern],
    browser: zd.Browser,
    timeout: int = 15,
    page: zd.Tab | None = None,
    error_reporter: ErrorReporter | None = None,
) -> tuple[bool, str, ConversionResult | None]:
    """Run the distillation loop with zendriver.

    Returns:
        terminated: bool indicating successful termination
        distilled: the raw distilled HTML
        converted: the converted JSON if successful, otherwise None
    """
    if len(patterns) == 0:
        logger.error("No distillation patterns provided")
        raise ValueError("No distillation patterns provided")

    hostname = (location and urllib.parse.urlparse(location).hostname) or ""

    if page is None:
        page = await get_new_page(browser)
        logger.info(f"Navigating to {location}")
        if location:
            try:
                await zen_navigate_with_retry(page, location)
            except Exception as error:
                if error_reporter is not None:
                    await error_reporter(
                        error=error,
                        page=page,
                        hostname=hostname,
                        iteration=0,
                    )
                raise ValueError(f"Failed to navigate to {location}: {error}")

    TICK = 1  # seconds
    max = timeout // TICK

    current = Match(name="", priority=-1, distilled="")

    # Let the page settle (e.g. redirect chains, JS/CSS) before polling, so
    # matched elements pass isVisible() and short timeouts don't falsely report
    # "No matched pattern found".
    try:
        await wait_for_ready_state(page, timeout=5)
    except Exception as e:
        logger.warning(f"Error waiting for page ready state: {e}")

    for iteration in range(max):
        logger.debug(f"Iteration {iteration + 1} of {max}")
        await asyncio.sleep(TICK)

        try:
            current_url = str(await page.evaluate("window.location.href", await_promise=True))
            hostname = urllib.parse.urlparse(current_url).hostname or ""
        except Exception:
            pass  # keep previous hostname

        match = await distill(hostname, page, patterns)
        if match:
            if match.distilled == current.distilled:
                logger.debug(f"Still the same: {match.name}")
            else:
                distilled = match.distilled
                current = match

                if await terminate(distilled):
                    converted = await convert(distilled, pattern_path=match.name)
                    return (True, distilled, converted)

                current.distilled = distilled

        else:
            logger.debug(f"No matched pattern found")

    # Only report when nothing matched at all. A non-terminating match (e.g. a sign-in
    # page) is the expected sign-in fallthrough, not an error worth reporting.
    nothing_matched = current.name == ""
    if error_reporter is not None and nothing_matched:
        await error_reporter(
            error=ValueError("No matched pattern found"),
            page=page,
            hostname=hostname,
            iteration=max,
        )
    return (False, current.distilled, None)
