# ---------------------------------------------------------------------------
# Email -> Password/OTP choice
# ---------------------------------------------------------------------------

import random
import time

from fasthtml.common import (
    Button,
    Input,
    Label,
)

from tests.acme_corp.acme_corp import app
from tests.acme_corp.constants import (
    MAX_TIME_DELAY,
    MIN_TIME_DELAY,
    VALID_EMAIL,
    VALID_OTP,
    VALID_PASSWORD,
)
from tests.acme_corp.helpers import (
    create_email_form,
    create_otp_form,
    render_form,
    welcome_page,
)


@app.get("/auth/email-then-pass-or-otp")
def email_then_pass_or_otp_form():
    """Initial login form that asks for email."""
    return create_email_form(action="/auth/email-then-pass-or-otp")


def create_password_or_otp_form(email: str, error_message: str = None):
    """Create a form with password input and options to use OTP instead."""
    password_fields = [
        Input(type="hidden", name="email", value=email),
        Label(
            "Password:",
            Input(
                type="password",
                name="password",
                # Password only required for login button
                required=False,
            ),
        ),
        Button(
            "Log in",
            type="submit",
            formaction="/submit/email-then-pass-or-otp/password",
            # Add form validation for password only on this button
            onclick=(
                "if(!this.form.password.value) { event.preventDefault(); "
                "this.form.password.required = true; "
                "this.form.password.reportValidity(); return false; }"
            ),
        ),
        Button(
            "Use a one-time code instead",
            type="submit",
            formaction="/submit/email-then-pass-or-otp/request-otp",
        ),
    ]
    return render_form(
        password_fields,
        action="/submit/email-then-pass-or-otp/password",
        error_message=error_message,
    )


@app.post("/auth/email-then-pass-or-otp")
def handle_email_submission(email: str):
    """Handle the email submission and show password page with OTP option."""
    time.sleep(random.uniform(MIN_TIME_DELAY, MAX_TIME_DELAY))

    if email != VALID_EMAIL:
        error_msg = "Invalid email address"
        return create_email_form(
            action="/auth/email-then-pass-or-otp",
            error_message=error_msg,
        )

    return create_password_or_otp_form(email)


@app.post("/submit/email-then-pass-or-otp/request-otp")
def handle_otp_request(email: str):
    """Handle the request for OTP and show OTP input form."""
    time.sleep(random.uniform(MIN_TIME_DELAY, MAX_TIME_DELAY))
    return create_otp_form(
        email,
        action="/submit/email-then-pass-or-otp/verify-otp",
    )


@app.post("/submit/email-then-pass-or-otp/password")
def handle_password_verification(email: str, password: str = None):
    """Handle the password verification step."""
    time.sleep(random.uniform(MIN_TIME_DELAY, MAX_TIME_DELAY))

    if email == VALID_EMAIL and password == VALID_PASSWORD:
        return welcome_page(email)

    error_msg = "Invalid password" if password is not None else None
    return create_password_or_otp_form(email, error_message=error_msg)


@app.post("/submit/email-then-pass-or-otp/verify-otp")
def handle_otp_verification(email: str, otp: str):
    """Handle the OTP verification step."""
    time.sleep(random.uniform(MIN_TIME_DELAY, MAX_TIME_DELAY))

    if email == VALID_EMAIL and otp == VALID_OTP:
        return welcome_page(email)

    error_msg = "Invalid code"
    return create_otp_form(
        email,
        action="/submit/email-then-pass-or-otp/verify-otp",
        error_message=error_msg,
    )
