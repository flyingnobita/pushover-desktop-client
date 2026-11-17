"""Offscreen browser QA against the live QA container.

Skipped when QA_BASE_URL is unset; a set QA_BASE_URL turns every check into a
hard failure instead of a skip.
"""

from __future__ import annotations

import os
import re

import pytest

BASE_URL = os.environ.get("QA_BASE_URL")
ADMIN_PASSWORD = os.environ.get("QA_ADMIN_PASSWORD", "qa-only")

pytestmark = pytest.mark.skipif(not BASE_URL, reason="QA_BASE_URL is not set")


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(base_url=BASE_URL)
        open_page = context.new_page()
        yield open_page
        context.close()
        browser.close()


def select_item(page, message_id: str) -> None:
    page.check(f'input[name="ids"][value="{message_id}"]')


def test_full_operator_flow(page) -> None:
    # Login
    page.goto("/")
    page.wait_for_url(re.compile(r".*/login.*"))
    page.fill("#password", ADMIN_PASSWORD)
    page.click('form[action="/login"] button[type="submit"]')
    page.wait_for_url(re.compile(r"/$"))

    # Receiver status line is truthful for the stub backend
    page.goto("/")
    status = page.locator(".status")
    status.get_by_text("Stubbed receiver").wait_for()
    status.get_by_text("last successful sync").wait_for()

    # Newest-first list with the seeded stub content
    titles = page.locator(".message-title")
    assert titles.count() == 3
    assert titles.nth(0).inner_text() == "Deploy finished"
    assert titles.nth(2).inner_text() == "Disk health warning"

    # Text search
    page.fill('input[name="q"]', "certificate")
    page.click(".filters button")
    page.wait_for_load_state()
    assert page.locator(".message-title").count() == 1
    assert page.locator(".message-title").inner_text() == "Certificate expires soon"

    # Source filter
    page.goto("/?scope=all&source=Monitor")
    assert page.locator(".message-title").count() == 2

    # Scope switch keeps the list honest
    page.goto("/?scope=archived")
    page.get_by_text("No notifications yet.").wait_for()

    # Detail view and emergency display
    page.goto("/?scope=inbox")
    page.get_by_role("link", name="Disk health warning").click()
    page.locator("h1", has_text="Disk health warning").wait_for()
    page.locator("article.priority-2").wait_for()
    page.get_by_text("Emergency").wait_for()
    assert "acknowledg" not in page.content().lower()

    # Opening detail marked the item read
    page.goto("/?scope=inbox&read=read")
    assert page.locator(".message-title").count() == 1
    page.goto("/?scope=inbox&read=unread")
    assert page.locator(".message-title").count() == 2

    # Mark read and unread on selected items
    page.goto("/?scope=all&read=unread")
    select_item(page, "1700000300")
    page.click('button[formaction="/messages/read"]')
    page.wait_for_load_state()
    page.goto("/?scope=all&read=read")
    assert page.locator(".message-title").count() == 2
    select_item(page, "1700000300")
    page.click('button[formaction="/messages/unread"]')
    page.wait_for_load_state()
    page.goto("/?scope=all&read=unread")
    assert page.locator(".message-title").count() == 2

    # Archive, then restore from the Archived scope
    page.goto("/?scope=inbox")
    select_item(page, "1700000300")
    page.click('button[formaction="/messages/archive"]')
    page.wait_for_load_state()
    page.goto("/?scope=inbox")
    assert page.locator(".message-title").count() == 2
    page.goto("/?scope=archived")
    assert page.locator(".message-title").inner_text() == "Deploy finished"
    select_item(page, "1700000300")
    page.click('button[formaction="/messages/restore"]')
    page.wait_for_load_state()
    page.goto("/?scope=inbox")
    assert page.locator(".message-title").count() == 3

    # Selected permanent deletion
    page.goto("/?scope=all")
    select_item(page, "1700000200")
    page.click('button[formaction="/messages/delete"]')
    page.wait_for_load_state()
    page.goto("/?scope=all")
    assert page.locator(".message-title").count() == 2
    assert "Certificate expires soon" not in page.content()

    # Delete all history requires the counted confirmation page
    page.goto("/?scope=all")
    page.get_by_role("link", name=re.compile("Delete all history")).click()
    page.get_by_text(re.compile(r"2 notification\(s\)")).first.wait_for()
    page.get_by_role("button", name=re.compile("Delete 2 notification")).click()
    page.wait_for_load_state()
    page.get_by_text("No notifications yet.").wait_for()

    # Receiver status is still shown after deletion
    page.locator(".status").get_by_text("Stubbed receiver").wait_for()
