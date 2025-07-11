from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import sentry_sdk

from getgather.analytics import (
    Event,
    ExtractStepProperties,
    send_analytics_event,
)
from getgather.browser.profile import BrowserProfile
from getgather.browser.session import browser_session
from getgather.config import settings
from getgather.connectors.spec_loader import BrandIdEnum
from getgather.connectors.spec_models import BrandSpec
from getgather.flow import flow_step
from getgather.flow_state import FlowState
from getgather.logs import logger
from getgather.parse import BundleOutput, parse
from getgather.sentry import setup_error_context


class ExtractState(StrEnum):
    """The state of the extract flow."""

    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    NEED_AUTH = "NEED_AUTH"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


class ExtractOrchestrator:
    """Manages extract flows for brands."""

    def __init__(
        self,
        brand_id: BrandIdEnum,
        browser_profile: BrowserProfile,
        nested_browser_session: bool = False,
    ):
        self.brand_id = brand_id
        self.browser_profile = browser_profile
        self.state: ExtractState = ExtractState.INITIALIZING
        self.bundles: list[BundleOutput] = []
        self._brand_spec: BrandSpec | None = None
        self.nested_browser_session = nested_browser_session
        self.parsing_status: Literal["not_used", "success", "failure"] = "not_used"

    async def extract_flow(self) -> None:
        """Start the extract flow for a brand."""
        self.state = ExtractState.RUNNING

        flow_state = FlowState(brand_id=self.brand_id, type="extract", bundle_dir=self.bundle_dir)
        await flow_state.init(
            browser_profile_id=self.browser_profile.profile_id, brand_id=self.brand_id
        )
        if flow_state.spec.extract is None:
            logger.warning(f"No extract flow defined for {self.brand_id}!")
            self.state = ExtractState.FINISHED
            return

        page = None
        async with browser_session(
            self.browser_profile, nested=self.nested_browser_session
        ) as session:
            try:
                page = await session.page()
                logger.info(
                    f"🔥 Starting extraction for {self.brand_id}",
                    extra={
                        "profile_id": self.browser_profile.profile_id,
                    },
                )
                while not flow_state.finished:
                    flow_state = await flow_step(page=page, flow_state=flow_state)
                    if flow_state.paused:
                        input(flow_state.paused or "Press Enter to continue")
                        flow_state.paused = None
                    if flow_state.bundle:
                        bundle_file_path = self.bundle_dir / flow_state.bundle.name
                        logger.info(
                            (
                                f"📦 Saving {flow_state.bundle.name} to "
                                f"{bundle_file_path}"
                            ),
                            extra={
                                "profile_id": self.browser_profile.profile_id,
                            },
                        )
                        self.bundles.append(
                            BundleOutput(
                                name=flow_state.bundle.name,
                                parsed=False,
                                parse_schema=None,
                                content=flow_state.bundle.content,
                            )
                        )
                        # allow local environment to save the bundle to the file system for easier debugging
                        if settings.ENVIRONMENT == "local":
                            with open(bundle_file_path, "w") as f:
                                f.write(flow_state.bundle.content)
                            logger.info(
                                (
                                    f"📦 {flow_state.bundle.name} saved to "
                                    f"{bundle_file_path}"
                                ),
                                extra={
                                    "profile_id": self.browser_profile.profile_id,
                                },
                            )
                        flow_state.bundle = None  # Prevent re-processing

                # TODO: Some way to return the file names and the raw data to the object and store in `files`

                logger.info(
                    "✅ Extraction finished.",
                    extra={
                        "profile_id": self.browser_profile.profile_id,
                    },
                )

                if flow_state.spec.parse:
                    try:
                        logger.info(
                            "🗂️ Parsing bundles ...",
                            extra={
                                "profile_id": self.browser_profile.profile_id,
                            },
                        )
                        parsed_bundles = await parse(
                            brand_id=self.brand_id, bundle_dir=self.bundle_dir
                        )
                        if parsed_bundles:
                            self.bundles.extend(parsed_bundles)
                            self.parsing_status = "success"
                    except Exception as e:
                        logger.error(f"Error parsing bundles: {e}")
                        self.state = ExtractState.ERROR
                        self.parsing_status = "failure"
                        raise e

                self.state = ExtractState.FINISHED

            except Exception as e:
                self.state = ExtractState.ERROR
                filename = f"{self.browser_profile.profile_id}_{datetime.now().strftime('%Y%m%d_%H%M')}.png"
                filepath = settings.screenshots_dir / filename
                if page:
                    await page.screenshot(path=str(filepath), full_page=True)
                    html = await page.content()
                    with sentry_sdk.isolation_scope() as scope:
                        setup_error_context(
                            scope=scope,
                            e=e,
                            brand_id=self.brand_id,
                            error_type="extract_flow_error",
                            flow_state=flow_state.model_dump(),
                            browser_profile_id=self.browser_profile.profile_id,
                            page_content=html,
                            screenshot_path=filepath,
                        )
                        sentry_sdk.capture_exception(e)
                else:
                    logger.warning("No page was found for extract flow")
                    with sentry_sdk.isolation_scope() as scope:
                        setup_error_context(
                            scope=scope,
                            e=e,
                            brand_id=self.brand_id,
                            error_type="extract_flow_error",
                            flow_state=flow_state.model_dump(),
                            browser_profile_id=self.browser_profile.profile_id,
                        )
                        sentry_sdk.capture_exception(e)
                raise e
            finally:
                await self._send_analytics_event()

    async def _send_analytics_event(self):
        """Sends an analytics event indicating the final status of the extraction flow."""
        payload = ExtractStepProperties(
            brand_id=self.brand_id.value,
            extract_status=self.state,
            parsing_status=self.parsing_status,
            num_orders=None,  # TODO, need to do some counting in the JSON
        )
        event = Event(
            profile_id=self.browser_profile.profile_id,
            event_name="extract_step",
            event_payload=payload,
        )
        await send_analytics_event(event)

    @property
    def bundle_dir(self) -> Path:
        dir = settings.bundles_dir / self.browser_profile.profile_id / self.brand_id
        if not dir.exists():
            dir.mkdir(parents=True, exist_ok=True)
        return dir
