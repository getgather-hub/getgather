"""Tests for the JSON projection of a distilled sign-in step.

These are pure parsing tests — no browser and no Chrome Fleet. They run against the real
pattern files in `getgather/mcp/patterns/`, so a pattern change that breaks the contract shows
up here.

Note that pattern files ship *empty* prose and label elements; distillation fills them from the
live page. Tests that care about that text fill it in first, the way `distill()` would.
"""

from pathlib import Path

import pytest
from bs4 import BeautifulSoup, Tag
from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from getgather.mcp.dpage import FINISHED_MSG, LoopOutcome, render_outcome, signin_state
from getgather.mcp.form_schema import (
    ButtonBlock,
    CheckboxBlock,
    InputBlock,
    RadioGroupBlock,
    SigninSubmission,
    TextBlock,
    parse_blocks,
)

PATTERNS = Path(__file__).parent.parent / "getgather" / "mcp" / "patterns"


def load(name: str) -> BeautifulSoup:
    return BeautifulSoup((PATTERNS / name).read_text(), "html.parser")


def html_of(response: HTMLResponse) -> str:
    return bytes(response.body).decode()


def test_amazon_signin_yields_two_inputs_and_a_submit() -> None:
    blocks = parse_blocks(load("amazon-signin.html"))

    assert [b.type for b in blocks] == ["input", "input", "button"]

    email, password, submit = blocks
    assert isinstance(email, InputBlock)
    assert (email.name, email.input_type) == ("email", "email")
    assert email.placeholder == "Email or mobile phone number"
    assert email.autofocus is True

    assert isinstance(password, InputBlock)
    assert (password.name, password.input_type) == ("password", "password")

    assert isinstance(submit, ButtonBlock)
    assert submit.label == "Sign in"
    assert submit.submit is True


def test_text_inputs_are_required_because_the_loop_demands_every_field() -> None:
    # No pattern uses the `required` attribute; the loop only submits once every non-radio
    # field has a value, so a surfaced input is required regardless of markup.
    blocks = parse_blocks(load("amazon-signin.html"))
    assert all(b.required for b in blocks if isinstance(b, InputBlock))


def test_google_choice_buttons_exclude_hidden_elements() -> None:
    blocks = parse_blocks(load("google-signin-choose-method.html"))

    # The hidden `input[name=none]` and hidden `button[name=submit]` exist only to satisfy the
    # autofill loop and must not reach the client.
    assert all(isinstance(b, ButtonBlock) for b in blocks)
    assert [b.value for b in blocks if isinstance(b, ButtonBlock)] == [
        "phone-tap-yes",
        "authenticator",
        "sms",
        "recovery-email-tap",
    ]
    # These are branch choices, not form submissions.
    assert all(not b.submit for b in blocks if isinstance(b, ButtonBlock))


def fill(document: BeautifulSoup, tag_name: str, texts: list[str]) -> None:
    """Fill empty elements with text, the way `distill()` does from the live page."""
    tags = [t for t in document.find_all(tag_name) if isinstance(t, Tag)]
    for tag, text in zip(tags, texts):
        tag.string = text


def _filled_nordstrom() -> BeautifulSoup:
    """nordstrom-mfa-choice.html as distillation would leave it after a real page load."""
    document = load("nordstrom-mfa-choice.html")
    fill(
        document,
        "p",
        [
            "Verify your identity",
            "How should we send your code?",
            "Standard rates apply",  # the <p for="sms"> hint
        ],
    )
    fill(document, "strong", ["j•••@x.com", "•••-1234"])
    return document


def test_nordstrom_groups_radios_and_keeps_prose_order() -> None:
    blocks = parse_blocks(_filled_nordstrom())

    # Order matters: the prose explaining the choice must precede the choice.
    assert [b.type for b in blocks] == ["text", "text", "radio_group", "button"]

    intro, question, group, send = blocks
    assert isinstance(intro, TextBlock) and intro.text == "Verify your identity"
    assert isinstance(question, TextBlock)
    assert question.text == "How should we send your code?"

    assert isinstance(group, RadioGroupBlock)
    assert group.name == "delivery-method"
    assert [(o.value, o.label, o.checked) for o in group.options] == [
        ("EMAIL", "Email", True),
        ("sms", "Text Message", False),
    ]

    assert isinstance(send, ButtonBlock) and send.label == "Send Code"


def test_nordstrom_option_hints_carry_masked_contact_details() -> None:
    blocks = parse_blocks(_filled_nordstrom())
    group = next(b for b in blocks if isinstance(b, RadioGroupBlock))

    # `id="EMAIL"` pairs with `for="email"`, so association must be case-insensitive.
    assert group.options[0].hint == "j•••@x.com"
    # Multiple `for=` elements collapse into one hint; this is how the user tells options apart.
    assert group.options[1].hint == "•••-1234 Standard rates apply"


def test_label_text_is_not_also_emitted_as_a_standalone_block() -> None:
    blocks = parse_blocks(_filled_nordstrom())
    texts = [b.text for b in blocks if isinstance(b, TextBlock)]
    assert "j•••@x.com" not in texts
    assert "Email" not in texts


def test_hidden_subtrees_are_skipped_but_visible_display_rules_are_not() -> None:
    document = BeautifulSoup(
        """
        <html><body>
          <div style="display: none"><input name="secret" type="text"/></div>
          <div style="display: flex; align-items: center"><input name="shown" type="text"/></div>
        </body></html>
        """,
        "html.parser",
    )
    blocks = parse_blocks(document)
    assert [b.name for b in blocks if isinstance(b, InputBlock)] == ["shown"]


def test_checkbox_becomes_its_own_block() -> None:
    document = BeautifulSoup(
        """
        <html><body>
          <input type="checkbox" name="remember" id="rm" checked/>
          <span for="rm">Keep me signed in</span>
        </body></html>
        """,
        "html.parser",
    )
    checkbox = parse_blocks(document)[0]
    assert isinstance(checkbox, CheckboxBlock)
    assert (checkbox.name, checkbox.label, checkbox.checked) == (
        "remember",
        "Keep me signed in",
        True,
    )


def test_unknown_input_type_falls_back_to_text() -> None:
    document = BeautifulSoup(
        '<html><body><input type="date" name="dob"/></body></html>', "html.parser"
    )
    block = parse_blocks(document)[0]
    assert isinstance(block, InputBlock)
    assert block.input_type == "text"


# --- outcome projection ------------------------------------------------------------------


def _outcome_from(name: str) -> LoopOutcome:
    return LoopOutcome(kind="need_input", title="Amazon Sign In", document=load(name))


def test_need_input_projects_to_need_signin_with_blocks() -> None:
    state = signin_state(_outcome_from("amazon-signin.html"), "b--t")
    assert state.status == "NEED_SIGNIN"
    assert state.signin_id == "b--t"
    assert state.title == "Amazon Sign In"
    assert state.error_code is None
    assert [b.type for b in state.blocks] == ["input", "input", "button"]


def test_finished_without_error_is_success() -> None:
    state = signin_state(LoopOutcome(kind="finished", title="Done"), "b--t")
    assert state.status == "SUCCESS"
    assert state.blocks == []


def test_finished_with_error_code_is_error() -> None:
    # The HTML path deliberately treats an rb-error pattern as merely "finished" so polling
    # stops; JSON splits it out so consumers stop keyword-matching the page.
    outcome = LoopOutcome(kind="finished", title="Closed", error_code="reset_password")
    state = signin_state(outcome, "b--t")
    assert state.status == "ERROR"
    assert state.error_code == "reset_password"


def test_timeout_is_a_status_not_a_503() -> None:
    state = signin_state(LoopOutcome(kind="timeout"), "b--t")
    assert state.status == "TIMEOUT"


def test_error_pattern_still_reports_the_page_text() -> None:
    document = load("amazon-error-account-closed.html")
    blocks = parse_blocks(document)
    assert any(isinstance(b, TextBlock) and b.text for b in blocks)


# --- HTML path must be unchanged ---------------------------------------------------------


def test_render_outcome_still_raises_503_on_timeout() -> None:
    with pytest.raises(HTTPException) as excinfo:
        render_outcome(LoopOutcome(kind="timeout"), "/dpage/x")
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == "Timeout reached"


def test_render_outcome_finished_renders_the_finished_message() -> None:
    body = html_of(render_outcome(LoopOutcome(kind="finished", title="Done"), "/dpage/x"))
    assert FINISHED_MSG in body
    assert "<title>Done</title>" in body


def test_render_outcome_surfaces_error_code_as_meta_tag() -> None:
    outcome = LoopOutcome(kind="finished", title="Closed", error_code="reset_password")
    body = html_of(render_outcome(outcome, "/dpage/x"))
    assert 'name="error-message" content="reset_password"' in body


def test_render_outcome_need_input_embeds_the_distilled_body_and_action() -> None:
    document = load("amazon-signin.html")
    outcome = LoopOutcome(kind="need_input", title="Amazon Sign In", document=document)
    body = html_of(render_outcome(outcome, "/dpage/abc"))

    assert 'action="/dpage/abc"' in body
    original_body = document.find("body")
    assert isinstance(original_body, Tag)
    # The HTML path passes the distilled body through verbatim, exactly as it did before.
    assert str(original_body) in body


# --- submission ---------------------------------------------------------------------------


def test_button_stays_out_of_values() -> None:
    submission = SigninSubmission(values={"email": "a@b.c"}, button="sms")
    assert submission.values == {"email": "a@b.c"}
    assert submission.button == "sms"


def test_a_site_field_named_button_is_not_a_button_choice() -> None:
    # google-signin-choose-method.html declares `<button name="button">` alongside real inputs,
    # so the two namespaces must stay separate all the way into distill_post_loop.
    submission = SigninSubmission(values={"button": "not-a-choice"})
    assert submission.button is None
    assert submission.values == {"button": "not-a-choice"}


def test_empty_submission_is_valid() -> None:
    submission = SigninSubmission()
    assert submission.values == {}
    assert submission.button is None
