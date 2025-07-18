import pytest
from playwright.sync_api import Page

from getgather.connectors.spec_loader import brand_id_set
from tests.acme_corp.constants import VALID_EMAIL, VALID_LASTNAME, VALID_OTP, VALID_PASSWORD

MAX_STEPS = 20

SPECS_WITH_CHOICE = [
    "acme-email-then-password-or-otp",
    "acme-email-then-otp-direct-or-password",
]
SKIPPED_BRANDS = [
    "acme-email-then-password-mfa-choice-phone-email",
    "universal-error-page",
    "acme-network-extraction",  # Not needed in FE tests because it's testing the URL listener for a JSON network call
]
SPECS_WITHOUT_CHOICE = sorted(
    brand_id_set(include="test") - set(SPECS_WITH_CHOICE) - set(SKIPPED_BRANDS)
)


@pytest.mark.webui
@pytest.mark.parametrize("brand_id", SPECS_WITHOUT_CHOICE)
def test_auth(page: Page, site_url: str, brand_id: str):
    _run_auth(page, site_url, brand_id)


@pytest.mark.webui
@pytest.mark.acme
@pytest.mark.parametrize("brand_id", SPECS_WITH_CHOICE)
@pytest.mark.parametrize("verification_choice", ["password", "otp"])
def test_auth_with_choice(page: Page, site_url: str, brand_id: str, verification_choice: str):
    _run_auth(page, site_url, brand_id, verification_choice=verification_choice)


def _run_auth(
    page: Page,
    site_url: str,
    brand_id: str,
    *,
    verification_choice: str | None = None,
    passwords: list[str] = [VALID_PASSWORD],
):
    page.goto(f"{site_url}?test=1")
    page.get_by_test_id(f"brand-card_{brand_id}").click()
    page.wait_for_url(f"{site_url}/start/{brand_id}")

    password_index = 0

    success = False
    for i in range(MAX_STEPS):
        print(f"Brand {brand_id} Step {i}")

        # wait for processing
        page.wait_for_timeout(2_000)

        progress = page.get_by_test_id("progress")
        progress_visible = progress.is_visible()
        content = progress.text_content() or ""
        print(f"🔍 Progress visible: {progress_visible}, content: {content}")
        if progress_visible:
            if "Connection successful" in content:
                success = True
                break
            elif "Error during authentication" in content:
                break

        email_input = page.get_by_test_id("input-email")
        email_visible = email_input.is_visible()
        print(f"🔍 Email input visible: {email_visible}")
        if email_visible:
            email_input.fill(VALID_EMAIL)

        if verification_choice != "otp":
            password_input = page.get_by_test_id("input-password")
            password_visible = password_input.is_visible()
            print(f"🔍 Password input visible: {password_visible}")
            if password_visible:
                # going through the passwords in order so we can test the wrong password case
                password_input.fill(passwords[password_index])
                password_index = (password_index + 1) % len(passwords)

        if verification_choice != "password":
            otp_input = page.get_by_test_id("input-otp")
            otp_visible = otp_input.is_visible()
            print(f"🔍 OTP input visible: {otp_visible}")
            if otp_visible:
                otp_input.fill(VALID_OTP)

        lastname_input = page.get_by_test_id("input-lastname")
        lastname_visible = lastname_input.is_visible()
        print(f"🔍 Lastname input visible: {lastname_visible}")
        if lastname_visible:
            lastname_input.fill(VALID_LASTNAME)

        if page.locator("form").count() == 1:
            submit_button = page.locator("button[type=submit]")
        else:  # multiple forms means there are multiple choices
            submit_button = page.get_by_test_id(f"form-{verification_choice}").locator(
                "button[type=submit]"
            )

        submit_visible = submit_button.is_visible()
        print(f"🔍 Submit button visible: {submit_visible}")
        if submit_visible:
            submit_button.click()

    assert success


@pytest.mark.webui
@pytest.mark.acme
def test_auth_with_wrong_password(page: Page, site_url: str):
    brand_id = "acme-email-then-password"
    _run_auth(page, site_url, brand_id, passwords=["wrongpassword", VALID_PASSWORD])
