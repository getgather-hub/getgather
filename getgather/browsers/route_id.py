from dataclasses import dataclass

PROVIDER_CODES = {"fly": "f", "daytona": "d", "browserbase": "b"}
CODE_PROVIDERS = {code: provider for provider, code in PROVIDER_CODES.items()}
ROUTED_BROWSER_ID_LENGTH = 9


@dataclass(frozen=True)
class BrowserRoute:
    provider: str
    browser_id: str


def make_routed_browser_id(provider: str, generated_browser_id: str) -> str:
    provider_code = PROVIDER_CODES.get(provider)
    if provider_code is None:
        raise ValueError(f"Unknown browser provider: {provider}")
    if len(generated_browser_id) != ROUTED_BROWSER_ID_LENGTH or not generated_browser_id.startswith(
        "B"
    ):
        raise ValueError("Expected a nine-character server-assigned browser ID")
    return f"B{provider_code}{generated_browser_id[2:]}"


def parse_routed_browser_id(browser_id: str) -> BrowserRoute | None:
    if len(browser_id) != ROUTED_BROWSER_ID_LENGTH or not browser_id.startswith("B"):
        return None
    provider = CODE_PROVIDERS.get(browser_id[1])
    return BrowserRoute(provider, browser_id) if provider is not None else None
