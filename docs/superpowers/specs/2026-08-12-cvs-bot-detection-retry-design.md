# CVS Bot Detection Reload Retry — Design

**Date:** 2026-08-12  
**Status:** Approved for implementation planning

## Goal

When `cvs-signin-bot-detection.html` matches during CVS sign-in, reload the page, restore the submitted email, and re-run distillation (including auto-submit on `cvs-signin.html`). Retry up to 3 reloads. If bot detection still matches after retries are exhausted, stop with `rb-error="captcha"`.

## Context

Today the bot-detection pattern matches at priority 1 with `rb-stop rb-error="captcha"`, so the first hit ends the flow. That is too aggressive for a transient CVS banner that often clears after a page reload.

Existing `rb-reload-before-actions` in `distill_post_loop` reloads once before acting on a matched pattern, but:

1. It is limited by a single boolean (`reload_before_actions_done`) for the whole POST.
2. Field values are deleted from `fields` after fill (`del fields[name_str]`), so a later retry cannot resubmit email.
3. `rb-stop` runs after reload handling — we need to skip stop while retries remain.

## Approach

**Extend `rb-reload-before-actions`** (not a new attribute family, not CVS-only code).

### Pattern

```html
<html
  rb-domain="cvs"
  rb-priority="1"
  rb-reload-before-actions
  rb-reload-max="3"
  rb-settle-ms="3000"
>
  <head>
    <title>CVS Bot Detection</title>
  </head>
  <body>
    <div rb-stop rb-error="captcha" rb-match="div.profile-input-error h2.banner-error-text"></div>
  </body>
</html>
```

- `rb-reload-before-actions` — request reload when this pattern newly matches.
- `rb-reload-max="3"` — allow up to 3 reloads for this pattern name per dpage POST.
- `rb-settle-ms="3000"` — wait after reload before re-distilling.
- `rb-stop` / `rb-error="captcha"` — honored only after reload budget is exhausted.

`cvs-signin.html` stays at `rb-priority="2"` so bot-detection wins while the banner is present.

### Loop changes (`getgather/mcp/dpage.py`)

Replace `reload_before_actions_done: bool` with:

- `reload_counts: dict[str, int]` — keyed by pattern `match.name`
- `consumed_fields: dict[str, str]` — values saved before `del fields[name_str]`

On match with `rb-reload-before-actions`:

1. If `reload_counts[name] < rb-reload-max` (default 1): reload, settle, restore `fields.update(consumed_fields)`, reset `current` match state, `continue` **without** processing `rb-stop`.
2. If budget exhausted: do not reload; fall through so `terminate` / `get_error` apply and return captcha error.

After a successful reload clears the banner, `cvs-signin.html` matches again, refills email from restored fields, and autoclicks Continue.

### Defaults / compatibility

- Patterns with `rb-reload-before-actions` and no `rb-reload-max` keep max=1 (current one-shot behavior).
- No change to `zen_distill.py` network-error reload path.

## Flow

```
POST email
  → cvs-signin fills + submits
  → bot banner → bot-detection matches
  → reload #1..#3 + restore email (skip rb-stop)
  → cvs-signin re-matches → refill + Continue
  → success path (confirm) OR after 3 reloads still bot → captcha stop
```

## Edge cases

| Case                                     | Behavior                                    |
| ---------------------------------------- | ------------------------------------------- |
| Bot clears after reload                  | Sign-in pattern proceeds normally           |
| Bot persists through 3 reloads           | 4th match stops with captcha                |
| No consumed email                        | Reload still runs; form may need user input |
| Other brands using reload-before-actions | Unchanged default max=1                     |

## Out of scope

- Underlying anti-bot / CloakBrowser changes
- Humanize timing on `cvs-signin.html` (optional follow-up if retries still fail)
- Walmart-style captcha patterns (they remain stop-immediately unless they opt into reload attrs)

## Testing

- Unit: `_pattern_reload_max` default=1 and custom=3
- Unit/integration as practical: field restore on reload; stop after max retries
- Manual: CVS sign-in dpage — observe reload logs then either confirm page or captcha after 3 tries
