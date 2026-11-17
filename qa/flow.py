"""Offscreen browser QA for the running QA container.

Plain Playwright script, not a pytest module: prints one line per checked
behaviour and exits nonzero on the first failed check.
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


@contextmanager
def step(name: str) -> Iterator[None]:
    try:
        yield
    except Exception:  # noqa: BLE001 - any failed check must stop the run
        print(f"FAIL {name}", flush=True)
        traceback.print_exc()
        sys.exit(1)
    print(f"ok   {name}", flush=True)


def select_item(page: Page, message_id: str) -> None:
    page.check(f'input[name="ids"][value="{message_id}"]')


def run(page: Page) -> None:
    with step("login with the QA password"):
        page.goto("/")
        page.wait_for_url(re.compile(r".*/login.*"))
        page.fill("#password", ADMIN_PASSWORD)
        page.click('form[action="/login"] button[type="submit"]')
        page.wait_for_url(re.compile(r"/$"))

    with step("receiver status line reports the stub backend and last sync"):
        page.goto("/")
        status = page.locator(".status")
        status.get_by_text("Stubbed receiver").wait_for()
        status.get_by_text("last successful sync").wait_for()

    with step("history lists stub messages newest first"):
        titles = page.locator(".message-title")
        assert titles.count() == 3, titles.count()
        assert titles.nth(0).inner_text() == "Deploy finished"
        assert titles.nth(2).inner_text() == "Disk health warning"

    with step("text search narrows the list"):
        page.fill('input[name="q"]', "certificate")
        page.click(".filters button")
        page.wait_for_load_state()
        assert page.locator(".message-title").count() == 1
        assert page.locator(".message-title").inner_text() == "Certificate expires soon"

    with step("source filter narrows the list"):
        page.goto("/?scope=all&source=Monitor")
        assert page.locator(".message-title").count() == 2

    with step("archived scope starts empty"):
        page.goto("/?scope=archived")
        page.get_by_text("No notifications yet.").wait_for()

    with step("detail shows content, emergency marker and no acknowledgement control"):
        page.goto("/?scope=inbox")
        page.get_by_role("link", name="Disk health warning").click()
        page.locator("h1", has_text="Disk health warning").wait_for()
        page.locator("article.priority-2").wait_for()
        page.get_by_text("Emergency").wait_for()
        assert "acknowledg" not in page.content().lower()

    with step("opening detail marks the notification read"):
        page.goto("/?scope=inbox&read=read")
        assert page.locator(".message-title").count() == 1
        page.goto("/?scope=inbox&read=unread")
        assert page.locator(".message-title").count() == 2

    with step("selected mark read updates read state"):
        page.goto("/?scope=all&read=unread")
        select_item(page, "1700000300")
        page.click('button[formaction="/messages/read"]')
        page.wait_for_load_state()
        page.goto("/?scope=all&read=read")
        assert page.locator(".message-title").count() == 2

    with step("selected mark unread restores unread state"):
        select_item(page, "1700000300")
        page.click('button[formaction="/messages/unread"]')
        page.wait_for_load_state()
        page.goto("/?scope=all&read=unread")
        assert page.locator(".message-title").count() == 2

    with step("selected archive moves the notification out of Inbox"):
        page.goto("/?scope=inbox")
        select_item(page, "1700000300")
        page.click('button[formaction="/messages/archive"]')
        page.wait_for_load_state()
        page.goto("/?scope=inbox")
        assert page.locator(".message-title").count() == 2
        assert "Deploy finished" not in page.content()

    with step("archived scope shows the archived notification"):
        page.goto("/?scope=archived")
        assert page.locator(".message-title").inner_text() == "Deploy finished"

    with step("restore returns the notification to Inbox"):
        select_item(page, "1700000300")
        page.click('button[formaction="/messages/restore"]')
        page.wait_for_load_state()
        page.goto("/?scope=inbox")
        assert page.locator(".message-title").count() == 3
        assert "Deploy finished" in page.content()

    with step("selected delete removes only that notification"):
        page.goto("/?scope=all")
        select_item(page, "1700000200")
        page.click('button[formaction="/messages/delete"]')
        page.wait_for_load_state()
        page.goto("/?scope=all")
        assert page.locator(".message-title").count() == 2
        assert "Certificate expires soon" not in page.content()

    with step("delete all history shows the affected count and confirms"):
        page.goto("/?scope=all")
        page.get_by_role("link", name=re.compile("Delete all history")).click()
        page.get_by_text(re.compile(r"2 notification\(s\)")).first.wait_for()
        page.get_by_role("button", name=re.compile("Delete 2 notification")).click()
        page.wait_for_load_state()
        page.get_by_text("No notifications yet.").wait_for()

    with step("receiver status remains visible after deletion"):
        page.goto("/")
        page.locator(".status").get_by_text("Stubbed receiver").wait_for()


def main() -> int:
    if not BASE_URL or not ADMIN_PASSWORD:
        print("QA_BASE_URL and QA_ADMIN_PASSWORD are required", file=sys.stderr)
        return 2
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(base_url=BASE_URL)
        try:
            run(context.new_page())
        finally:
            context.close()
            browser.close()
    print("QA PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
