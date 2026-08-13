from getgather.browsers.backend import ProxyVerificationError

FALLBACK_PROBE_URL = "https://ip.fly.dev"
DEFAULT_PROXY_BEST_OF_N = 3


def effective_proxy_best_of_n(explicit: int | None) -> int:
    return max(1, explicit if explicit is not None else DEFAULT_PROXY_BEST_OF_N)


def proxy_probe_url(target_domain: str | None) -> str:
    if not target_domain or not target_domain.strip():
        return FALLBACK_PROBE_URL
    first = target_domain.split(",")[0].strip()
    if not first:
        return FALLBACK_PROBE_URL
    return f"https://{first}/"


def candidate_session_ids(browser_id: str, n: int) -> list[str]:
    return [f"{browser_id}-p{i}" for i in range(n)]


def parse_ttfb(stdout: str) -> float | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def pick_fastest_session(results: list[tuple[str, float | None]]) -> str:
    successes = [(sid, ttfb) for sid, ttfb in results if ttfb is not None]
    if not successes:
        raise ProxyVerificationError("Best-of-N proxy: no proxy candidate succeeded")
    return min(successes, key=lambda item: item[1])[0]
