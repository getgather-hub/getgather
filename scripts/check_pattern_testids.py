"""Check that dpage pattern files use the shared data-testid convention.

Every visible <input> and <button> in getgather/mcp/patterns/*.html must carry a
data-testid from the shared vocabulary below, so playwright tests can share locator
code across brands (page.getByTestId("email"), getByTestId("submit"), ...).
"""

import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, Tag

PATTERNS_DIR = Path(__file__).resolve().parent.parent / "getgather" / "mcp" / "patterns"

EXACT_TESTIDS = {
    "answer",  # security question answer
    "day",  # date-of-birth day field
    "email",  # field that accepts an email address only
    "month",  # date-of-birth month field
    "otp",  # single verification-code field
    "password",
    "phone",
    "submit",  # the primary submit/continue button of the form
    "username",  # combo identifier field (email or phone/username/member number)
    "year",  # date-of-birth year field
    "zip-code",
}
TESTID_REGEXES = [
    re.compile(r"^otp-\d+$"),  # one box of a multi-box OTP field, 0-based
    re.compile(r"^signin-method-[a-z-]+$"),  # sign-in method radio or button
    re.compile(r"^option-\d+$"),  # generic choice radio (e.g. account/channel pickers), 1-based
]
INPUT_TYPE_TESTIDS = {
    # some combo identifier fields use type="email" upstream, so both are allowed there
    "email": frozenset({"email", "username"}),
    "password": frozenset({"password"}),
}
OTP_BOX_RE = re.compile(r"^otp-(\d+)$")


def allowed(testid: str) -> bool:
    return testid in EXACT_TESTIDS or any(r.match(testid) for r in TESTID_REGEXES)


def is_exempt(el: Tag) -> bool:
    if el.has_attr("gg-autoclick") or el.has_attr("rb-autoclick"):
        return True
    style = re.sub(r"\s", "", str(el.get("style") or ""))
    if "display:none" in style:
        return True
    if el.name == "input" and str(el.get("type") or "").lower() == "hidden":
        return True
    # match-only placeholders, e.g. an empty <button> that a gg-autoclick sibling clicks
    if el.name == "button" and not el.get_text(strip=True):
        return True
    return False


def describe(el: Tag) -> str:
    name = el.get("name")
    return f"<{el.name} name={name!r}>" if name else f"<{el.name}>"


def check_file(path: Path) -> list[str]:
    soup = BeautifulSoup(path.read_text(), "html.parser")
    errors: list[str] = []
    otp_boxes: list[int] = []
    for el in soup.find_all(["input", "button"]):
        if not isinstance(el, Tag) or is_exempt(el):
            continue
        raw = el.get("data-testid")
        if raw is None:
            errors.append(f"{describe(el)} is missing data-testid")
            continue
        testid = str(raw)
        if not allowed(testid):
            errors.append(f"{describe(el)} has unknown data-testid {testid!r}")
            continue
        if box := OTP_BOX_RE.match(testid):
            otp_boxes.append(int(box.group(1)))
        input_type = str(el.get("type") or "").lower()
        expected = INPUT_TYPE_TESTIDS.get(input_type) if el.name == "input" else None
        if expected and testid not in expected:
            errors.append(
                f"{describe(el)} has data-testid {testid!r}, "
                f"expected one of {sorted(expected)} for type={input_type!r}"
            )
    if otp_boxes and sorted(otp_boxes) != list(range(len(otp_boxes))):
        errors.append(
            f"multi-box OTP data-testids must be contiguous and 0-based, got {sorted(otp_boxes)}"
        )
    return errors


def main() -> int:
    files = sorted(PATTERNS_DIR.rglob("*.html"))
    failures: list[str] = []
    for path in files:
        name = path.relative_to(PATTERNS_DIR).as_posix()
        failures.extend(f"{name}: {error}" for error in check_file(path))

    if failures:
        print(f"{len(failures)} data-testid issue(s) in getgather/mcp/patterns:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nVisible <input>/<button> elements in pattern files need a data-testid from the"
            f"\nshared vocabulary so playwright tests can share locators: "
            f"{', '.join(sorted(EXACT_TESTIDS))},"
            "\notp-<n> (multi-box OTP, 0-based), signin-method-<method> (sign-in method radios"
            "\nor buttons), option-<n> (generic choice radios, 1-based)."
            f"\nSee {Path(__file__).name} to extend the vocabulary for a genuinely new field type.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(files)} pattern files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
