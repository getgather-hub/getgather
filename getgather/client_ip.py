import asyncio
import ipaddress
from contextvars import ContextVar
from typing import Final

import httpx
from fastapi import Request
from loguru import logger

client_ip_var: ContextVar[str | None] = ContextVar("client_ip", default=None)
request_headers_var: ContextVar[dict[str, str]] = ContextVar("request_headers", default={})

IP_CHECK_URL: Final[str] = "https://api.ipify.org"

_cached_server_public_ip: str | None = None
_server_public_ip_lock = asyncio.Lock()


def _is_local_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


async def _get_server_public_ip() -> str | None:
    """Return this machine's public IP — used only when the TCP peer is local/private
    (server and client co-located), so the server's public IP equals the user's IP.
    """
    global _cached_server_public_ip
    if _cached_server_public_ip:
        return _cached_server_public_ip
    async with _server_public_ip_lock:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(IP_CHECK_URL, timeout=5.0)
                _cached_server_public_ip = resp.text.strip()
                logger.info(f"[SERVER PUBLIC IP] Detected: {_cached_server_public_ip}")
        except Exception as e:
            logger.warning(f"[SERVER PUBLIC IP] Detection failed: {e}")
    return _cached_server_public_ip


async def resolve_client_ip(request: Request) -> tuple[str | None, str | None]:
    """Return ``(ip, source)`` for the request, with priority:

    1. ``x-origin-ip`` header (explicitly set by controlled clients)
    2. TCP peer (``request.client.host``), falling back to this server's public IP
       when the peer is loopback/private (local-dev co-location).
    """
    origin_ip = request.headers.get("x-origin-ip")
    if origin_ip:
        return origin_ip, "x-origin-ip"
    if request.client:
        ip = request.client.host
        if _is_local_ip(ip):
            public = await _get_server_public_ip()
            if public:
                return public, "server-public-ip"
        return ip, "tcp"
    return None, None
