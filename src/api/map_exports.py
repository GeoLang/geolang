import asyncio
import logging
from contextlib import contextmanager

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# 3840 is the long side of a 4K UHD frame, so 3840x2160 and 2160x3840 both fit
MAXIMUM_EXPORT_SIDE_PIXELS = 3840
MAXIMUM_CONCURRENT_EXPORTS = 2
EXPORT_TIMEOUT_SECONDS = 60
BROWSER_CLOSE_TIMEOUT_SECONDS = 10
PAGE_LOAD_TIMEOUT_MILLISECONDS = 30000
TILE_SETTLE_MILLISECONDS = 2500

CALLER_EXPORT_RUNNING_REPLY = (
    "An export of yours is still running. Wait for it to finish, then try again."
)
EXPORTS_BUSY_REPLY = (
    "The server is already making as many map exports as it can. Try again in a minute."
)


class ExportRefused(Exception):
    pass


class ExportSlots:
    def __init__(self, concurrent_limit: int):
        self.concurrent_limit = concurrent_limit
        self.running_callers: set[str] = set()

    @contextmanager
    def slot(self, caller: str):
        if caller in self.running_callers:
            raise ExportRefused(CALLER_EXPORT_RUNNING_REPLY)
        if len(self.running_callers) >= self.concurrent_limit:
            raise ExportRefused(EXPORTS_BUSY_REPLY)
        self.running_callers.add(caller)
        try:
            yield
        finally:
            self.running_callers.discard(caller)


export_slots = ExportSlots(MAXIMUM_CONCURRENT_EXPORTS)


async def close_browser(browser) -> None:
    try:
        async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT_SECONDS):
            await browser.close()
    except TimeoutError:
        logger.warning("chromium did not close in time, the playwright driver kills it on exit")


async def render_map_export(url: str, width: int, height: int, write) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = None
        try:
            async with asyncio.timeout(EXPORT_TIMEOUT_SECONDS):
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": width, "height": height})
                await page.goto(
                    url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS
                )
                await page.wait_for_timeout(TILE_SETTLE_MILLISECONDS)
                await write(page)
        finally:
            if browser is not None:
                await close_browser(browser)


async def run_map_export(kind: str, caller: str, url: str, width: int, height: int, write) -> None:
    try:
        with export_slots.slot(caller):
            await render_map_export(url, width, height, write)
    except ExportRefused as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e))
    except TimeoutError:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            f"The {kind} export took longer than {EXPORT_TIMEOUT_SECONDS} seconds and was stopped.",
        )
    except Exception as e:
        logger.error(f"{kind} export failed: {e}")
        raise HTTPException(status_code=500, detail=f"{kind} export failed: {e}")
