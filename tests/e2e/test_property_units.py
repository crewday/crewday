"""Property detail unit-management smoke coverage for cd-0rua."""

from __future__ import annotations

from uuid import uuid4

from playwright.sync_api import Page, expect

from app.auth.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from tests.e2e._helpers.visual import compare_screenshot

EXPECT_TIMEOUT_MS = 15_000


def _open_manager_page(page: Page, *, base_url: str, path: str) -> None:
    base = base_url.rstrip("/")
    page.goto(f"{base}/mocks/", wait_until="domcontentloaded")
    page.get_by_role("button", name="Manager").click()
    page.goto(f"{base}/mocks{path}", wait_until="domcontentloaded")


def _csrf_headers(page: Page) -> dict[str, str]:
    for cookie in page.context.cookies():
        if cookie["name"] == CSRF_COOKIE_NAME:
            return {CSRF_HEADER_NAME: cookie["value"]}
    raise AssertionError(f"no {CSRF_COOKIE_NAME!r} cookie present")


def test_multi_unit_property_shows_units_tab_and_order(
    page: Page,
    base_url: str,
) -> None:
    _open_manager_page(page, base_url=base_url, path="/property/p-villa-sud")
    units_tab = page.locator(".tabs").get_by_text("Units", exact=True)
    expect(units_tab).to_be_visible(timeout=EXPECT_TIMEOUT_MS)

    units_tab.click()
    rows = page.locator(".property-units__table tbody tr")
    expect(rows).to_have_count(2, timeout=EXPECT_TIMEOUT_MS)
    expect(rows.nth(0)).to_contain_text("Main house")
    expect(rows.nth(1)).to_contain_text("Garden studio")
    expect(page.get_by_role("button", name="Add unit")).to_be_visible()

    page.wait_for_timeout(500)
    compare_screenshot(page, name="property_units_tab")


def test_single_unit_property_reveals_units_after_manage_action(
    page: Page,
    base_url: str,
) -> None:
    _open_manager_page(page, base_url=base_url, path="/property/p-apt-3b")
    units_tab = page.locator(".tabs").get_by_text("Units", exact=True)
    expect(units_tab).to_be_hidden(timeout=EXPECT_TIMEOUT_MS)
    page.get_by_role("button", name="Manage units").click()

    expect(units_tab).to_be_visible(timeout=EXPECT_TIMEOUT_MS)
    expect(page.locator(".property-units__table tbody tr")).to_have_count(
        1,
        timeout=EXPECT_TIMEOUT_MS,
    )
    page.get_by_role("button", name="Delete").click()
    expect(page.get_by_text("Cannot delete the last live unit")).to_be_visible()
    expect(page.get_by_role("button", name="Delete unit")).to_be_disabled()

    response = page.request.delete(
        f"{base_url.rstrip('/')}/mocks/api/v1/units/u-apt-3b-default",
        headers=_csrf_headers(page),
    )
    assert response.status == 409
    assert response.json()["detail"] == "cannot_delete_last_live_unit"


def test_unit_mutations_update_rows_and_audit_feed(
    page: Page,
    base_url: str,
) -> None:
    suffix = uuid4().hex[:8]
    unit_name = f"Guest cottage {suffix}"
    renamed_name = f"Guest cottage renamed {suffix}"

    _open_manager_page(page, base_url=base_url, path="/property/p-villa-lac")
    units_tab = page.locator(".tabs").get_by_text("Units", exact=True)
    expect(units_tab).to_be_visible(timeout=EXPECT_TIMEOUT_MS)
    units_tab.click()

    page.get_by_role("button", name="Add unit").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name").fill(unit_name)
    dialog.get_by_label("Order").fill("2")
    dialog.get_by_label("Default check-in").fill("14:30")
    dialog.get_by_label("Default check-out").fill("11:30")
    dialog.get_by_label("Max guests").fill("3")
    dialog.get_by_role("button", name="Add unit").click()
    expect(dialog).to_be_hidden(timeout=EXPECT_TIMEOUT_MS)
    new_row = page.locator(".property-units__table tbody tr").filter(has_text=unit_name)
    expect(new_row).to_have_count(1, timeout=EXPECT_TIMEOUT_MS)
    expect(new_row).to_contain_text("14:30 / 11:30")
    expect(new_row).to_contain_text("3")

    new_row.get_by_role("button", name="Edit").click()
    dialog.get_by_label("Name").fill(renamed_name)
    dialog.get_by_label("Order").fill("3")
    dialog.get_by_role("button", name="Save changes").click()
    expect(dialog).to_be_hidden(timeout=EXPECT_TIMEOUT_MS)
    renamed_row = page.locator(".property-units__table tbody tr").filter(
        has_text=renamed_name,
    )
    expect(renamed_row).to_have_count(1, timeout=EXPECT_TIMEOUT_MS)
    expect(renamed_row).to_contain_text("3")

    renamed_row.get_by_role("button", name="Delete").click()
    expect(dialog.get_by_text(f"Delete {renamed_name}?")).to_be_visible()
    expect(dialog.get_by_role("button", name="Delete unit")).to_be_enabled()
    dialog.get_by_role("button", name="Delete unit").click()
    expect(dialog).to_be_hidden(timeout=EXPECT_TIMEOUT_MS)
    expect(
        page.locator(".property-units__table tbody tr").filter(
            has_text=renamed_name,
        ),
    ).to_have_count(0)

    page.goto(f"{base_url.rstrip('/')}/mocks/audit", wait_until="domcontentloaded")
    expect(page.locator("tbody tr").nth(0)).to_contain_text("unit.delete")
    expect(page.locator("tbody tr").nth(1)).to_contain_text("unit.update")
    expect(page.locator("tbody tr").nth(2)).to_contain_text("unit.create")
