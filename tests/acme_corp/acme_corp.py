from typing import Any
from fasthtml.common import (
    H1,
    A,
    Body,
    FastHTML,
    Head,
    Html,
    Li,
    Main,
    Title,
    Ul,
    picolink,
)

app = FastHTML(hdrs=(picolink,))


@app.get("/")
def index() -> Any:
    return Html(
        Head(Title("ACME Corp"), picolink),
        Body(
            Main(
                H1("ACME Corp"),
                Ul(
                    Li(A("Email and Password", href="/auth/email-and-password")),
                ),
                Ul(
                    Li(A("Email then Password", href="/auth/email-then-password")),
                ),
                Ul(
                    Li(
                        A("Email then Password then MFA", href="/auth/email-then-password-then-mfa")
                    ),
                ),
                Ul(
                    Li(
                        A(
                            "Email and Password in Overlay component",
                            href="/auth/email-password-overlay",
                        )
                    ),
                ),
                Ul(
                    Li(A("Email then OTP", href="/auth/email-then-otp")),
                ),
                Ul(
                    Li(
                        A(
                            "Email then OTP with Multi Inputs",
                            href="/auth/email-then-otp-multi-inputs",
                        )
                    ),
                ),
                Ul(
                    Li(
                        A(
                            "Email and Password with Checkbox",
                            href="/auth/email-and-password-checkbox",
                        )
                    ),
                ),
                Ul(
                    Li(
                        A(
                            "Email then Password with Long Delay",
                            href="/auth/email-then-password-long-delay",
                        )
                    ),
                ),
                Ul(
                    Li(
                        A(
                            "Email and Password Overlay with Hidden Last Name",
                            href="/auth/email-password-lastname-overlay",
                        )
                    ),
                ),
                Ul(
                    Li(
                        A(
                            "Sign-in Error Test (Universal Page)",
                            href="/universal-error-test",
                        )
                    ),
                ),
                Ul(
                    Li(
                        A(
                            "Email then Password or OTP via Radio Buttons",
                            href="/auth/email-then-password-or-otp",
                        )
                    ),
                ),
                Ul(
                    Li(
                        A(
                            "Email then OTP or Password",
                            href="/auth/email-then-otp-or-password",
                        )
                    ),
                ),
                Ul(
                    Li(
                        A(
                            "Email then Password/OTP via Raw Selection",
                            href="/auth/email-then-pass-or-otp",
                        )
                    ),
                ),
                cls="container",
            )
        ),
    )
