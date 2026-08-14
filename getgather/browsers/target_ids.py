"""Namespacing helpers for CDP target ids.

The `/cdp/{browser_id}` websocket route multiplexes many browsers behind one proxy, so it
rewrites every `targetId` crossing it into a `<browser_id>@<target_id>` form (see
`browsers.router.patch_cdp_target_inbound`). Clients that route on the id — the
`/devtools/{path}` proxy, zendriver's per-tab websocket URLs — need the namespace; anything
comparing ids or exporting them in a public identifier wants the browser's raw id back.

This lives in its own leaf module rather than in `browsers.router` because `browser.py` and
`cdp_client.py` both need it and `router -> cdp_client -> browser` is an existing import chain.
"""

TARGET_ID_SEPARATOR = "@"


def strip_browser_id_from_target_id(target_id: str) -> str:
    """`abc@PAGE1` -> `PAGE1`. A raw id passes through untouched, so this is safe to apply to
    ids of unknown provenance."""
    if TARGET_ID_SEPARATOR not in target_id:
        return target_id
    return target_id.split(TARGET_ID_SEPARATOR, 1)[1]


def prepend_browser_id_to_target_id(target_id: str, browser_id: str) -> str:
    """`PAGE1` -> `abc@PAGE1`. Idempotent: an already-namespaced id is returned as-is, since
    re-prefixing would break the round trip back through `strip_browser_id_from_target_id`."""
    if TARGET_ID_SEPARATOR in target_id:
        return target_id
    return browser_id + TARGET_ID_SEPARATOR + target_id
