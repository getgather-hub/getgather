from __future__ import annotations

from contextlib import asynccontextmanager
from typing import ClassVar

from fastapi import HTTPException
from patchright.async_api import BrowserContext, Page, Playwright, async_playwright

from getgather.browser.profile import BrowserProfile
from getgather.logs import logger


class BrowserStartupError(HTTPException):
    """Raised when browser fails to start."""

    def __init__(self, message: str):
        super().__init__(status_code=503, detail=message, headers={"X-No-Retry": "true"})


class BrowserSession:
    _sessions: ClassVar[dict[str, BrowserSession]] = {}

    def __init__(self, profile: BrowserProfile):
        self.profile: BrowserProfile = profile
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    @classmethod
    async def get(cls, profile: BrowserProfile) -> BrowserSession:
        await profile.init()
        if profile.profile_id in cls._sessions:  # retrieve active session
            return cls._sessions[profile.profile_id]
        else:  # create new session
            return BrowserSession(profile)

    @property
    def context(self) -> BrowserContext:
        assert self._context is not None, "Browser session not started"
        return self._context

    @property
    def playwright(self) -> Playwright:
        assert self._playwright is not None, "Browser session not started"
        return self._playwright

    async def page(self) -> Page:
        # TODO: It's okay for now to return the last page. We may want to track all pages in the future.
        if not self.context.pages:
            await self.context.new_page()
        return self.context.pages[-1]

    async def start(self):
        try:
            if self.profile.profile_id in BrowserSession._sessions:
                # Session already started
                return

            BrowserSession._sessions[self.profile.profile_id] = self

            logger.info(
                f"Starting new session with profile {self.profile.profile_id}",
                extra={"profile_id": self.profile.profile_id},
            )

            self._playwright = await async_playwright().start()
            self._context = await self.profile.config.launch(
                profile_id=self.profile.profile_id, browser_type=self.playwright.chromium
            )
        except Exception as e:
            logger.error(f"Error starting browser: {e}")
            raise BrowserStartupError(f"Failed to start browser: {e}") from e

    async def stop(self):
        logger.info(
            "Closing browser",
            extra={
                "profile_id": self.profile.profile_id,
            },
        )
        try:
            if self.context.browser:
                await self.context.browser.close()
        except Exception as e:
            logger.error(f"Error closing browser. Stopping playwright manually: {e}")
            raise
        finally:
            await self.playwright.stop()

        # clean up local browser profile after playwright is stopped
        self.profile.config.cleanup(self.profile.profile_id)

        del self._sessions[self.profile.profile_id]


@asynccontextmanager
async def browser_session(profile: BrowserProfile, *, nested: bool = False):
    session = await BrowserSession.get(profile)
    if not nested:
        await session.start()
    try:
        yield session
    finally:
        if not nested:
            await session.stop()
