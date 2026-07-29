"""JSON projection of a distilled sign-in step.

The distillation engine produces HTML (`zen_distill.Match.distilled`) — a pattern skeleton whose
text and input values have been filled in from the live page. `parse_blocks` turns that document
into an ordered list of typed blocks so a client can render its own native form instead of
embedding ours.

Order is preserved because it is load-bearing: the prose that precedes a radio group is what
tells the user what the options mean.
"""

import re
from typing import Annotated, Literal

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

# Every input type that appears across getgather/mcp/patterns/**.html. There are no <select> or
# <textarea> elements in any pattern, so this projection deliberately has no block for them.
TEXT_INPUT_TYPES = frozenset({"text", "email", "password", "tel", "number"})

# Tags whose text content is prose meant for the user. Their content is scraped from the live
# page at distill time, so it carries things like masked phone numbers and challenge questions.
PROSE_TAGS = frozenset({"p", "strong", "span", "h1", "h2", "h3", "h4", "h5", "h6"})

_DISPLAY_NONE = re.compile(r"display\s*:\s*none", re.IGNORECASE)


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class InputBlock(BaseModel):
    type: Literal["input"] = "input"
    name: str
    input_type: str
    placeholder: str | None = None
    value: str | None = None
    # Always true: the polling loop only submits once every non-radio field has a value
    # (`expected_field_count == len(names)` in dpage.distill_post_loop), so a surfaced text
    # input is required whether or not the markup says so. No pattern uses the `required`
    # attribute.
    required: bool = True
    autofocus: bool = False


class RadioOption(BaseModel):
    value: str
    label: str | None = None
    hint: str | None = None
    checked: bool = False


class RadioGroupBlock(BaseModel):
    type: Literal["radio_group"] = "radio_group"
    name: str
    options: list[RadioOption] = Field(default_factory=list[RadioOption])


class CheckboxBlock(BaseModel):
    type: Literal["checkbox"] = "checkbox"
    name: str
    label: str | None = None
    checked: bool = False


class ButtonBlock(BaseModel):
    type: Literal["button"] = "button"
    label: str = ""
    # Present on choice buttons (`<button name="button" value="sms">`); the client echoes it
    # back as the top-level `button` key to pick that branch.
    value: str | None = None
    submit: bool = False


Block = Annotated[
    TextBlock | InputBlock | RadioGroupBlock | CheckboxBlock | ButtonBlock,
    Field(discriminator="type"),
]


class SigninState(BaseModel):
    """A single step of a sign-in flow."""

    status: Literal["NEED_SIGNIN", "SUCCESS", "ERROR", "TIMEOUT"]
    signin_id: str
    title: str | None = None
    error_code: str | None = None
    blocks: list[Block] = Field(default_factory=list["Block"])


class SigninSubmission(BaseModel):
    """Body of a sign-in step submission.

    `values` holds the site's own field names. `button` stays outside it and is passed to
    `distill_post_loop` separately, so a site field named `button` or `submit` cannot be
    mistaken for the button-choice key — `google-signin-choose-method.html` declares
    `<button name="button">`, `<input name="none">` and `<button name="submit">`.
    """

    values: dict[str, str] = Field(default_factory=dict[str, str])
    button: str | None = None


def _attr(tag: Tag, name: str) -> str | None:
    value = tag.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item) for item in value)  # pyright: ignore[reportUnknownVariableType]
    return None


def _is_hidden(tag: Tag) -> bool:
    style = _attr(tag, "style")
    return style is not None and _DISPLAY_NONE.search(style) is not None


def _text_for(document: BeautifulSoup, element_id: str | None) -> tuple[str | None, str | None]:
    """Resolve the label and hint that describe the element with `element_id`.

    Only 12 of 463 patterns use a real `<label>`. The rest associate text with an input via a
    `for` attribute on arbitrary tags (`<strong for="email">`, `<p for="sms">` in
    nordstrom-mfa-choice.html). Matching is case-insensitive because that file pairs
    `id="EMAIL"` with `for="email"`.

    The first non-empty text becomes the label; anything further becomes the hint, which is
    where masked contact details land.
    """
    if not element_id:
        return None, None

    wanted = element_id.casefold()
    texts: list[str] = []
    for tag in document.find_all(attrs={"for": True}):
        if not isinstance(tag, Tag) or _is_hidden(tag):
            continue
        for_value = _attr(tag, "for")
        if for_value is None or for_value.casefold() != wanted:
            continue
        text = tag.get_text(strip=True)
        if text:
            texts.append(text)

    if not texts:
        return None, None
    return texts[0], " ".join(texts[1:]) or None


def _button_block(tag: Tag, label: str) -> ButtonBlock:
    button_type = (_attr(tag, "type") or "").lower()
    submit = button_type == "submit" or "rb-autoclick" in tag.attrs or "gg-autoclick" in tag.attrs
    return ButtonBlock(label=label, value=_attr(tag, "value"), submit=submit)


def _handle_input(
    tag: Tag,
    document: BeautifulSoup,
    blocks: list[Block],
    groups: dict[str, RadioGroupBlock],
) -> None:
    input_type = (_attr(tag, "type") or "text").lower()
    if input_type == "hidden":
        return

    if input_type == "submit":
        blocks.append(_button_block(tag, _attr(tag, "value") or ""))
        return

    name = _attr(tag, "name")
    if name is None:
        return

    if input_type == "radio":
        group = groups.get(name)
        if group is None:
            # The group takes the document position of its first option.
            group = RadioGroupBlock(name=name)
            groups[name] = group
            blocks.append(group)
        label, hint = _text_for(document, _attr(tag, "id"))
        group.options.append(
            RadioOption(
                value=_attr(tag, "value") or "",
                label=label,
                hint=hint,
                checked="checked" in tag.attrs,
            )
        )
        return

    if input_type == "checkbox":
        label, _ = _text_for(document, _attr(tag, "id"))
        blocks.append(CheckboxBlock(name=name, label=label, checked="checked" in tag.attrs))
        return

    blocks.append(
        InputBlock(
            name=name,
            input_type=input_type if input_type in TEXT_INPUT_TYPES else "text",
            placeholder=_attr(tag, "placeholder"),
            value=_attr(tag, "value"),
            autofocus="autofocus" in tag.attrs,
        )
    )


def _walk(
    node: Tag,
    document: BeautifulSoup,
    blocks: list[Block],
    groups: dict[str, RadioGroupBlock],
) -> None:
    for child in node.children:
        if not isinstance(child, Tag) or _is_hidden(child):
            continue

        if child.name == "input":
            _handle_input(child, document, blocks, groups)
        elif child.name == "button":
            blocks.append(_button_block(child, child.get_text(strip=True)))
        elif child.name in PROSE_TAGS:
            # A `for` attribute means this text describes an input and is already surfaced as
            # that option's label or hint.
            if "for" not in child.attrs:
                text = child.get_text(strip=True)
                if text:
                    blocks.append(TextBlock(text=text))
        else:
            _walk(child, document, blocks, groups)


def parse_blocks(document: BeautifulSoup) -> list[Block]:
    """Project a distilled document into ordered, typed blocks.

    Subtrees hidden with `display: none` are skipped — patterns use them to hold elements that
    exist only to satisfy the autofill loop, such as the hidden `input[name=none]` and hidden
    submit button in google-signin-choose-method.html.
    """
    body = document.find("body")
    root = body if isinstance(body, Tag) else document

    blocks: list[Block] = []
    groups: dict[str, RadioGroupBlock] = {}
    _walk(root, document, blocks, groups)
    return blocks
