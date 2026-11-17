"""Capture the README screenshots from the running QA container.

Plain Playwright script, not a pytest module: prints one line per capture and
exits nonzero on the first failure. Every pixel comes from the stub backend's
deterministic demo messages, so the captures can be regenerated at any time.
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import Page, sync_playwright

BASE_URL = os.environ.get("QA_BASE_URL")
ADMIN_PASSWORD = os.environ.get("QA_ADMIN_PASSWORD", "")
OUTPUT_DIR = "/qa/out"
VIEWPORT = {"width": 1280, "height": 800}


@contextmanager
def step(name: str) -> Iterator[None]:
    try:
        yield
    except Exception:  # noqa: BLE001 - any failed capture must stop the run
        print(f"FAIL {name}", flush=True)
        traceback.print_exc()
        sys.exit(1)
    print(f"ok   {name}", flush=True)


def capture(page: Page, name: str) -> None:
    path = os.path.join(OUTPUT_DIR, f"{name}.png")
    page.screenshot(path=path)
    print(f"ok   wrote {path}", flush=True)


def run(page: Page) -> None:
    with step("login with the QA password"):
        page.goto("/")
        page.wait_for_url(re.compile(r".*/login.*"))
        page.fill("#password", ADMIN_PASSWORD)
        page.click('form[action="/login"] button[type="submit"]')
        page.wait_for_url(re.compile(r"/$"))

    with step("capture the Inbox list"):
        page.goto("/")
        page.locator(".message-title").first.wait_for()
        capture(page, "history-list")

    with step("capture the Disk health warning detail"):
        page.get_by_role("link", name="Disk health warning").click()
        page.locator("h1", has_text="Disk health warning").wait_for()
        capture(page, "history-detail")


def main() -> int:
    if not BASE_URL or not ADMIN_PASSWORD:
        print("QA_BASE_URL and QA_ADMIN_PASSWORD are required", file=sys.stderr)
        return 2
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(base_url=BASE_URL, viewport=VIEWPORT)
        try:
            run(context.new_page())
        finally:
            context.close()
            browser.close()
    print("SCREENSHOTS PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
