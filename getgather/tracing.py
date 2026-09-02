"""Logfire/OpenTelemetry tracing for remote-browser.

Owns all observability wiring:
- Logfire configuration and FastAPI/httpx instrumentation
- Loguru → Logfire handler
- Per-request `x-session-id` generation/echo and per-session trace grouping
  via a raw ASGI middleware

The goal is that every log emitted while processing requests sharing one
`x-session-id` lives under one Logfire trace. Two cases:

- **Caller sends a W3C `traceparent`.** The caller owns the trace. We leave
  `traceparent`/`tracestate` untouched so OTel's distributed-tracing
  propagation parents server spans naturally under the caller's trace. We
  only tag spans with `session.session_id` for filtering.

- **Caller sends no `traceparent`.** We mint a deterministic root span whose
  `trace_id` IS the `session_id` (a uuid4().hex is 32 hex chars = 128
  bits, a valid OTel trace_id), and rewrite the scope `traceparent` to point
  at it BEFORE OTel's FastAPI instrumentation extracts it. All requests for
  the session collapse into one trace whose id is the session id — pasteable
  into Logfire to find the trace.

The server doesn't track sessions itself. We generate a session id when the
client doesn't supply one and echo `x-session-id` back so the client reuses
it across requests.
"""

import hashlib
import uuid
from typing import TYPE_CHECKING

import logfire
from fastapi import FastAPI, Request
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import _Span as SDKSpan  # pyright: ignore[reportPrivateUsage]
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, TraceFlags
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from getgather.config import settings

if TYPE_CHECKING:
    from loguru import HandlerConfig


SESSION_ID_HEADER = b"x-session-id"
TRACEPARENT_HEADER = b"traceparent"
TRACESTATE_HEADER = b"tracestate"

SCOPE_SESSION_ID_KEY = "session_id"


def setup_logfire() -> None:
    if not settings.LOGFIRE_TOKEN:
        logger.warning("Logfire is disabled, no LOGFIRE_TOKEN provided")
        return

    logger.info("Initializing Logfire")
    logfire.configure(
        service_name="remote-browser",
        send_to_logfire="if-token-present",
        token=settings.LOGFIRE_TOKEN,
        environment=settings.ENVIRONMENT,
        code_source=logfire.CodeSource(
            repository="https://github.com/remotebrowser/remotebrowser", revision="main"
        ),
        distributed_tracing=True,
        console=False,
        scrubbing=False,
    )
    logfire.instrument_httpx()


def instrument_fastapi(app: FastAPI) -> None:
    if not settings.LOGFIRE_TOKEN:
        return
    logfire.instrument_fastapi(app, capture_headers=True, excluded_urls="/health")


def logfire_loguru_handler() -> "HandlerConfig | None":
    if not settings.LOGFIRE_TOKEN:
        return None
    handler = logfire.loguru_handler()
    handler["level"] = settings.LOG_LEVEL
    return handler


_emitted_session_root_spans: set[str] = set()


_SESSION_INSTRUMENTATION_SCOPE = InstrumentationScope("getgather.session")


def _endpoint_from_path(path: str) -> str:
    return path.strip("/").split("/")[0] or "root"


class SessionTraceMiddleware:
    """Raw ASGI middleware that groups requests into a session trace.

    Must wrap the FastAPI app from OUTSIDE OpenTelemetry's instrumentation.
    OTel's FastAPIInstrumentor wraps the entire user-middleware stack via
    `build_middleware_stack`, so a `@app.middleware("http")` runs too late —
    OTel has already extracted traceparent and parented the request span.
    This middleware decides what scope headers OTel will see.

    Behavior:
    - If the caller sent a valid W3C `traceparent`, leave it alone — the
      caller owns the trace, server spans parent under it naturally.
    - Otherwise, inject a deterministic `traceparent` pointing at a per-session
      root span whose trace_id == session_id, so all requests for that
      session land in one trace.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        headers: list[tuple[bytes, bytes]] = list(scope["headers"])
        header_map: dict[bytes, bytes] = {k: v for k, v in headers}

        request_session_id = header_map.get(SESSION_ID_HEADER, b"").decode() or None
        session_id = request_session_id or uuid.uuid4().hex
        scope[SCOPE_SESSION_ID_KEY] = session_id

        caller_has_trace = self._has_valid_caller_traceparent(header_map)

        if caller_has_trace:
            # Case 1: caller owns the trace. Don't touch traceparent/tracestate;
            # just canonicalize the session-id header so downstream code
            # sees the (possibly server-generated) value.
            new_headers = [(k, v) for k, v in headers if k != SESSION_ID_HEADER]
            new_headers.append((SESSION_ID_HEADER, session_id.encode()))
            scope["headers"] = new_headers
        else:
            # Case 2: no caller trace. Mint a session-deterministic traceparent
            # so OTel parents the request span under our session root.
            endpoint = _endpoint_from_path(path)
            self._emit_session_root_span_once(session_id, endpoint)
            session_traceparent = self._traceparent_for_session(session_id)
            stripped = [
                (k, v)
                for k, v in headers
                if k not in (TRACEPARENT_HEADER, TRACESTATE_HEADER, SESSION_ID_HEADER)
            ]
            stripped.append((TRACEPARENT_HEADER, session_traceparent))
            stripped.append((SESSION_ID_HEADER, session_id.encode()))
            scope["headers"] = stripped

        session_id_header_bytes = session_id.encode()

        async def send_with_session_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
                response_headers = [(k, v) for k, v in existing if k != SESSION_ID_HEADER]
                response_headers.append((SESSION_ID_HEADER, session_id_header_bytes))
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_with_session_id)

    @classmethod
    def _has_valid_caller_traceparent(cls, header_map: dict[bytes, bytes]) -> bool:
        traceparent = header_map.get(TRACEPARENT_HEADER, b"").decode()
        if not traceparent:
            return False
        carrier: dict[str, str] = {"traceparent": traceparent}
        tracestate = header_map.get(TRACESTATE_HEADER, b"").decode()
        if tracestate:
            carrier["tracestate"] = tracestate
        extracted = TraceContextTextMapPropagator().extract(carrier=carrier)
        return trace.get_current_span(extracted).get_span_context().is_valid

    @classmethod
    def _traceparent_for_session(cls, session_id: str) -> bytes:
        ctx = cls._span_context_from_session_id(session_id)
        return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01".encode()

    @classmethod
    def _emit_session_root_span_once(cls, session_id: str, endpoint: str) -> None:
        if session_id in _emitted_session_root_spans:
            return
        _emitted_session_root_spans.add(session_id)

        # tracer.start_span() assigns a random span_id from the provider's
        # IdGenerator, which would mean our deterministic session span_id (used
        # as the parent in the injected traceparent) never matches any emitted
        # span — leaving the session trace with an orphaned root. Construct the
        # SDK _Span directly so we control both trace_id AND span_id.
        provider = trace.get_tracer_provider()
        sdk_provider = getattr(provider, "provider", provider)  # unwrap logfire proxy
        span_processor = getattr(sdk_provider, "_active_span_processor", None)
        resource = getattr(sdk_provider, "resource", None)
        if span_processor is None or resource is None:
            return  # not an SDK TracerProvider — nothing to export to

        session_ctx = cls._span_context_from_session_id(session_id)
        span = SDKSpan(
            name=f"Session root, endpoint {endpoint}, session {session_id}",
            context=session_ctx,
            parent=None,
            resource=resource,
            span_processor=span_processor,
            instrumentation_scope=_SESSION_INSTRUMENTATION_SCOPE,
            attributes={"session.session_id": session_id, "session.endpoint": endpoint},
        )
        span.start()
        span.end()

    @classmethod
    def _span_context_from_session_id(cls, session_id: str) -> SpanContext:
        # A uuid4().hex is 32 hex chars = 128 bits, a valid OTel trace_id. Use
        # it directly so the session ID IS the trace ID. Fall back to SHA-256
        # for any non-hex input.
        try:
            trace_id = int(session_id, 16) & ((1 << 128) - 1)
            span_id = int(session_id[:16], 16) & ((1 << 64) - 1)
            if trace_id == 0 or span_id == 0:
                raise ValueError("invalid session id")
        except ValueError:
            digest = hashlib.sha256(session_id.encode()).digest()
            trace_id = int.from_bytes(digest[:16])
            span_id = int.from_bytes(digest[16:24])
        return SpanContext(
            trace_id=trace_id, span_id=span_id, is_remote=True, trace_flags=TraceFlags(1)
        )


def setup_session_tracing(request: Request) -> str:
    session_id: str = request.scope[SCOPE_SESSION_ID_KEY]
    if settings.LOGFIRE_TOKEN:
        trace.get_current_span().set_attribute("session.session_id", session_id)
    return session_id
