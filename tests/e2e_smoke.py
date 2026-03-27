#!/usr/bin/env python3
"""E2E smoke test for HOAproxy. Run after major deploys.

Usage:
    # Public pages only
    python tests/e2e_smoke.py

    # Full run with auth
    python tests/e2e_smoke.py --email test@example.com --password MyPass123

    # Or use env vars (SMOKE_TEST_EMAIL, SMOKE_TEST_PASSWORD)
    python tests/e2e_smoke.py

    # Debug mode (visible browser)
    python tests/e2e_smoke.py --headed --slow-mo 500

    # Against local dev
    python tests/e2e_smoke.py --url http://localhost:8000

    # Run only multi-user API tests (group 8) or browser tests (group 9)
    python tests/e2e_smoke.py --group 8
    python tests/e2e_smoke.py --group 9

Env vars:
    SMOKE_TEST_EMAIL / SMOKE_TEST_PASSWORD       — primary test account (groups 4, 9)
    SMOKE_TEST_EMAIL_2 / SMOKE_TEST_PASSWORD_2   — second account (group 9 only)
    SMOKE_TEST_EMAIL_3 / SMOKE_TEST_PASSWORD_3   — third account (group 9 only)
    TESTMAIL_API_KEY / TESTMAIL_NAMESPACE         — email delivery tests (group 6)

    Group 8 (multi-user API) creates throwaway accounts — no extra env vars needed.
    Group 9 (multi-user browser) requires all 3 accounts to be pre-registered
    and members of at least one shared HOA.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ---------------------------------------------------------------------------
# Settings env loader
# ---------------------------------------------------------------------------

def _load_settings_env():
    """Load settings.env into os.environ if it exists."""
    env_path = Path(__file__).resolve().parent.parent / "settings.env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


# ---------------------------------------------------------------------------
# Results tracker
# ---------------------------------------------------------------------------

class SmokeResults:
    def __init__(self):
        self.results: list[tuple[str, str, str]] = []

    def passed(self, name: str):
        self.results.append((name, "PASS", ""))
        print(f"  \033[32mPASS\033[0m  {name}")

    def failed(self, name: str, detail: str):
        self.results.append((name, "FAIL", detail))
        print(f"  \033[31mFAIL\033[0m  {name} — {detail}")

    def skipped(self, name: str, reason: str):
        self.results.append((name, "SKIP", reason))
        print(f"  \033[33mSKIP\033[0m  {name} — {reason}")

    def warn(self, name: str, note: str):
        self.results.append((name, "WARN", note))
        print(f"  \033[33mWARN\033[0m  {name} — {note}")

    def summary(self):
        total = len(self.results)
        passed = sum(1 for _, s, _ in self.results if s == "PASS")
        failed = sum(1 for _, s, _ in self.results if s == "FAIL")
        warned = sum(1 for _, s, _ in self.results if s == "WARN")
        skipped = sum(1 for _, s, _ in self.results if s == "SKIP")

        print(f"\n{'='*60}")
        print(f"  TOTAL: {total}   \033[32mPASS: {passed}\033[0m   "
              f"\033[31mFAIL: {failed}\033[0m   "
              f"\033[33mWARN: {warned}\033[0m   SKIP: {skipped}")
        print(f"{'='*60}")

        failures = [(n, d) for n, s, d in self.results if s == "FAIL"]
        if failures:
            print("\n  Failures:")
            for name, detail in failures:
                print(f"    - {name}: {detail}")

        warns = [(n, d) for n, s, d in self.results if s == "WARN"]
        if warns:
            print("\n  Warnings:")
            for name, detail in warns:
                print(f"    - {name}: {detail}")

    def all_passed(self) -> bool:
        return not any(s == "FAIL" for _, s, _ in self.results)


# ---------------------------------------------------------------------------
# Benign JS error filter
# ---------------------------------------------------------------------------

_BENIGN_PATTERNS = [
    "favicon",
    "fonts.googleapis",
    "fonts.gstatic",
    "the server responded with a status of 404",
    "Failed to load resource",  # favicon, etc.
    "redirect",  # Auth redirect console messages
    "navigation",
    "net::ERR",
]

def _is_benign(msg: str) -> bool:
    lower = msg.lower()
    return any(p.lower() in lower for p in _BENIGN_PATTERNS)


# ---------------------------------------------------------------------------
# Testmail.app helpers
# ---------------------------------------------------------------------------

def _testmail_fetch(api_key: str, namespace: str, tag: str,
                    timeout: float = 30.0, timestamp_from: int = 0):
    """Poll testmail.app for an email matching the given tag. Returns email dict or None."""
    ts = timestamp_from or int((time.time() - 5) * 1000)
    url = (
        f"https://api.testmail.app/api/json"
        f"?apikey={api_key}&namespace={namespace}"
        f"&tag={tag}&livequery=true&timestamp_from={ts}"
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                emails = data.get("emails", [])
                if emails:
                    return emails[0]
        except Exception:
            pass
        time.sleep(2)
    return None


def _extract_reset_token(email_html: str) -> str | None:
    """Extract the reset token from a password reset email HTML body."""
    match = re.search(r'/reset-password\?token=([A-Za-z0-9_-]+)', email_html)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PAGE_TIMEOUT = 15_000  # 15s for Render cold starts
INTERACTION_TIMEOUT = 20_000  # 20s for API-backed interactions


def _hoa_list_from_summary(data: dict) -> list[dict]:
    """Extract HOA list from summary response, normalising field names."""
    raw = data.get("results") or data.get("hoas") or []
    out = []
    for h in raw:
        out.append({
            "id": h.get("hoa_id") or h.get("id"),
            "name": h.get("hoa") or h.get("name"),
        })
    return out


def _clear_errors(errors: list[str]) -> list[str]:
    """Return non-benign errors and clear the list."""
    real = [e for e in errors if not _is_benign(e)]
    errors.clear()
    return real


def _check_page(page, url: str, results: SmokeResults, test_name: str,
                js_errors: list[str], expect_selector: str | None = None,
                expect_text: str | None = None):
    """Navigate to a page and run basic checks."""
    js_errors.clear()
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if resp and resp.status >= 400:
            results.failed(test_name, f"HTTP {resp.status}")
            return False

        if expect_selector:
            try:
                page.wait_for_selector(expect_selector, timeout=PAGE_TIMEOUT)
            except PwTimeout:
                results.failed(test_name, f"Selector '{expect_selector}' not found")
                return False

        if expect_text:
            try:
                page.wait_for_function(
                    f"document.body.innerText.includes({json.dumps(expect_text)})",
                    timeout=PAGE_TIMEOUT,
                )
            except PwTimeout:
                results.failed(test_name, f"Text '{expect_text}' not found on page")
                return False

        page.wait_for_timeout(500)  # let late JS errors fire
        real_errors = _clear_errors(js_errors)
        if real_errors:
            results.failed(test_name, f"JS errors: {real_errors[:3]}")
            return False

        results.passed(test_name)
        return True
    except PwTimeout:
        results.failed(test_name, "Page load timeout")
        return False
    except Exception as exc:
        results.failed(test_name, str(exc)[:120])
        return False


def _check_auth_gate(page, url: str, results: SmokeResults, test_name: str,
                     js_errors: list[str]):
    """Visit a page that should redirect to /login when unauthenticated."""
    js_errors.clear()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_url("**/login**", timeout=8000)
        results.passed(test_name)
    except PwTimeout:
        current = page.url
        if "/login" in current:
            results.passed(test_name)
        else:
            results.failed(test_name, f"Expected redirect to /login, got {current}")
    except Exception as exc:
        results.failed(test_name, str(exc)[:120])


# ---------------------------------------------------------------------------
# Test groups
# ---------------------------------------------------------------------------

def group1_static(page, base: str, results: SmokeResults, js_errors: list[str]):
    """Static pages — no auth needed."""
    print("\n--- Group 1: Static Pages ---")
    for path, text in [("/about", "HOAproxy"), ("/terms", "Terms"), ("/privacy", "Privacy")]:
        _check_page(page, f"{base}{path}", results, f"Static: {path}",
                     js_errors, expect_text=text)


def group2_public(page, base: str, results: SmokeResults, js_errors: list[str]):
    """Public pages — no auth needed."""
    print("\n--- Group 2: Public Pages ---")

    _check_page(page, base + "/", results, "Homepage", js_errors,
                expect_selector="#searchInput")

    _check_page(page, base + "/login", results, "Login page", js_errors,
                expect_selector="#loginBtn")

    _check_page(page, base + "/register", results, "Register page", js_errors,
                expect_selector="#registerBtn")

    _check_page(page, base + "/forgot-password", results, "Forgot password page",
                js_errors, expect_selector="#submitBtn")

    # Legal — wait for state dropdown to populate
    js_errors.clear()
    try:
        page.goto(base + "/legal", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        # Wait for JS to fetch /law/jurisdictions and populate the dropdown
        page.wait_for_function(
            "document.querySelectorAll('#stateSelect option').length > 2",
            timeout=INTERACTION_TIMEOUT,
        )
        opts = page.locator("#stateSelect option").count()
        if opts > 2:
            results.passed("Legal page + state dropdown populated")
        else:
            results.failed("Legal page + state dropdown populated",
                           f"Only {opts} options in dropdown")
    except PwTimeout:
        # Debug: what's actually in the dropdown?
        try:
            inner = page.evaluate("document.getElementById('stateSelect').innerHTML")
            results.failed("Legal page + state dropdown populated",
                           f"Dropdown did not populate. Contents: {inner[:200]}")
        except Exception:
            results.failed("Legal page + state dropdown populated", "Dropdown did not populate")
    except Exception as exc:
        results.failed("Legal page + state dropdown populated", str(exc)[:120])

    # Legal with ?state=NC
    js_errors.clear()
    try:
        page.goto(base + "/legal?state=NC", wait_until="domcontentloaded",
                   timeout=PAGE_TIMEOUT)
        page.wait_for_function(
            "document.querySelectorAll('#stateSelect option').length > 2",
            timeout=INTERACTION_TIMEOUT,
        )
        selected = page.evaluate("document.getElementById('stateSelect').value")
        if selected == "NC":
            results.passed("Legal ?state=NC auto-selects")
        else:
            results.warn("Legal ?state=NC auto-selects",
                         f"Expected NC, got '{selected}'")
    except Exception as exc:
        results.failed("Legal ?state=NC auto-selects", str(exc)[:120])

    # Pages that just need to load without crashing
    for path, name in [
        ("/hoa", "HOA page (empty)"),
        ("/participation", "Participation page"),
        ("/verify-email", "Verify email (no token)"),
        ("/reset-password", "Reset password (no token)"),
        ("/verify-proxy", "Verify proxy page"),
    ]:
        _check_page(page, base + path, results, name, js_errors)


def group3_auth_gates(page, base: str, results: SmokeResults, js_errors: list[str]):
    """Auth gate checks — verify redirect to /login when unauthenticated."""
    print("\n--- Group 3: Auth Gate Checks ---")
    gated_pages = [
        "/dashboard", "/my-proxies", "/assign-proxy",
        "/become-delegate", "/delegate-dashboard",
        "/proposals", "/add-participation", "/add-hoa",
        "/account",
    ]
    for path in gated_pages:
        _check_auth_gate(page, base + path, results, f"Auth gate: {path}", js_errors)


def group4_authenticated(page, base: str, results: SmokeResults, js_errors: list[str],
                          email: str, password: str):
    """Login + authenticated page checks."""
    print("\n--- Group 4: Login + Authenticated Pages ---")

    # Login via UI
    js_errors.clear()
    try:
        page.goto(base + "/login", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.fill("#email", email)
        page.fill("#password", password)
        page.click("#loginBtn")
        page.wait_for_url("**/dashboard**", timeout=INTERACTION_TIMEOUT)
        results.passed("Login via UI")
    except PwTimeout:
        # Check if login failed with error message
        try:
            status_text = page.text_content("#status")
            results.failed("Login via UI", f"Login failed: {status_text}")
        except Exception:
            results.failed("Login via UI", "Timeout waiting for redirect to dashboard")
        return  # Can't continue without login
    except Exception as exc:
        results.failed("Login via UI", str(exc)[:120])
        return

    # Dashboard checks
    js_errors.clear()
    try:
        page.wait_for_selector("#greeting", timeout=PAGE_TIMEOUT)
        greeting = page.text_content("#greeting")
        if greeting and "Dashboard" in greeting:
            results.passed("Dashboard greeting visible")
        else:
            results.warn("Dashboard greeting visible", f"Greeting text: '{greeting}'")

        # Q&A widget
        if page.locator("#qaInput").count() > 0:
            results.passed("Dashboard Q&A widget present")
        else:
            results.warn("Dashboard Q&A widget present", "Q&A textarea not found")

        # Checklist
        if page.locator("#checklist").count() > 0:
            results.passed("Dashboard checklist present")
        else:
            results.warn("Dashboard checklist present", "Checklist not found")

    except Exception as exc:
        results.failed("Dashboard checks", str(exc)[:120])

    # Visit each auth-gated page while logged in
    auth_pages = [
        ("/my-proxies", "My Proxies (authed)"),
        ("/proposals", "Proposals (authed)"),
        ("/become-delegate", "Become Delegate (authed)"),
        ("/delegate-dashboard", "Delegate Dashboard (authed)"),
        ("/assign-proxy", "Assign Proxy (authed)"),
        ("/add-participation", "Add Participation (authed)"),
        ("/add-hoa", "Add HOA (authed)"),
    ]
    for path, name in auth_pages:
        _check_page(page, base + path, results, name, js_errors)

    # Verify login/register redirect to dashboard when already logged in
    js_errors.clear()
    try:
        page.goto(base + "/login", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_url("**/dashboard**", timeout=8000)
        results.passed("Login redirects to dashboard when authed")
    except PwTimeout:
        if "/dashboard" in page.url:
            results.passed("Login redirects to dashboard when authed")
        else:
            results.warn("Login redirects to dashboard when authed",
                         f"Stayed on {page.url}")

    js_errors.clear()
    try:
        page.goto(base + "/register", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_url("**/dashboard**", timeout=8000)
        results.passed("Register redirects to dashboard when authed")
    except PwTimeout:
        if "/dashboard" in page.url:
            results.passed("Register redirects to dashboard when authed")
        else:
            results.warn("Register redirects to dashboard when authed",
                         f"Stayed on {page.url}")

    # ---- Account page interactive tests ----
    js_errors.clear()
    try:
        page.goto(base + "/account", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#firstName", timeout=PAGE_TIMEOUT)

        # Verify fields loaded
        first_name = page.input_value("#firstName")
        last_name = page.input_value("#lastName")
        email_val = page.input_value("#email")
        if email_val:
            results.passed("Account: profile fields loaded")
        else:
            results.warn("Account: profile fields loaded", "Email field empty")

        # Edit first name and save
        original_first = first_name
        test_name = f"SmokeTest{int(time.time())}"
        page.fill("#firstName", test_name)
        page.click("#saveProfileBtn")
        page.wait_for_function(
            "document.querySelector('#profileStatus')?.classList.contains('ok')",
            timeout=INTERACTION_TIMEOUT,
        )
        results.passed("Account: profile save")

        # Reload and verify persistence
        page.goto(base + "/account", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#firstName", timeout=PAGE_TIMEOUT)
        page.wait_for_function(
            "document.getElementById('firstName').value.length > 0",
            timeout=INTERACTION_TIMEOUT,
        )
        saved_name = page.input_value("#firstName")
        if saved_name == test_name:
            results.passed("Account: profile edit persisted")
        else:
            results.failed("Account: profile edit persisted",
                           f"Expected '{test_name}', got '{saved_name}'")

        # Restore original name
        page.fill("#firstName", original_first or "")
        page.click("#saveProfileBtn")
        page.wait_for_timeout(1000)
    except PwTimeout:
        results.failed("Account: profile tests", "Timeout")
    except Exception as exc:
        results.failed("Account: profile tests", str(exc)[:120])

    # ---- Account password change via UI ----
    js_errors.clear()
    try:
        page.goto(base + "/account", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#currentPassword", timeout=PAGE_TIMEOUT)

        # Try with wrong current password
        page.fill("#currentPassword", "definitelyWrong1!")
        page.fill("#newPassword", "TempNewPass123!")
        page.fill("#confirmPassword", "TempNewPass123!")
        page.click("#changePasswordBtn")
        page.wait_for_function(
            "document.querySelector('#passwordStatus')?.classList.contains('error')",
            timeout=INTERACTION_TIMEOUT,
        )
        results.passed("Account: wrong current password rejected (UI)")
    except PwTimeout:
        results.warn("Account: wrong current password rejected (UI)", "Timeout")
    except Exception as exc:
        results.failed("Account: wrong current password rejected (UI)", str(exc)[:120])

    # ---- Dashboard Q&A widget ----
    js_errors.clear()
    try:
        page.goto(base + "/dashboard", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#qaInput", timeout=PAGE_TIMEOUT)

        # Check if HOA dropdown has options (user has claimed HOAs)
        qa_select = page.locator("#qaHoaSelect, #hoaSelect")
        if qa_select.count() > 0:
            page.wait_for_timeout(1500)  # let HOA list populate
            page.fill("#qaInput", "What are the rules?")
            qa_btn = page.locator("#qaBtn, #qaSubmitBtn, button:has-text('Ask')")
            if qa_btn.count() > 0:
                qa_btn.first.click()
                try:
                    page.wait_for_function(
                        "(document.getElementById('qaAnswer') || document.getElementById('qaResult'))?.textContent.trim().length > 0",
                        timeout=30000,  # QA can be slow
                    )
                    results.passed("Dashboard: Q&A widget returns answer")
                except PwTimeout:
                    results.warn("Dashboard: Q&A widget returns answer",
                                 "Answer did not appear within 30s")
            else:
                results.warn("Dashboard: Q&A widget", "No QA submit button found")
        else:
            results.warn("Dashboard: Q&A widget", "No HOA selector — user may not have claimed HOAs")
    except Exception as exc:
        results.failed("Dashboard: Q&A widget", str(exc)[:120])

    # ---- HOA profile page (authed) ----
    hoas = []
    js_errors.clear()
    try:
        # Get the user's first HOA name from the summary API
        req = urllib.request.Request(f"{base}/hoas/summary?page=1&per_page=1")
        with urllib.request.urlopen(req, timeout=15) as resp:
            summary_data = json.loads(resp.read())
        hoas = _hoa_list_from_summary(summary_data)
        if hoas:
            hoa_name = hoas[0]["name"]
            hoa_slug = hoa_name  # URL uses the name directly
            page.goto(base + f"/hoa/{quote(hoa_slug)}", wait_until="domcontentloaded",
                       timeout=PAGE_TIMEOUT)
            page.wait_for_timeout(2000)  # let JS fetch data
            title_text = page.text_content("#hoaTitle, h1") or ""
            if title_text and title_text != "Loading...":
                results.passed(f"HOA profile: page loads ({hoa_name[:30]})")
            else:
                results.warn("HOA profile: page loads", f"Title still loading: '{title_text}'")

            real_errors = _clear_errors(js_errors)
            if real_errors:
                results.failed("HOA profile: no JS errors", f"{real_errors[:3]}")
            else:
                results.passed("HOA profile: no JS errors")
        else:
            results.skipped("HOA profile page", "No HOAs in summary")
    except Exception as exc:
        results.failed("HOA profile page", str(exc)[:120])

    # ---- Participation page ----
    js_errors.clear()
    try:
        if hoas:
            hoa_name = hoas[0]["name"]
            page.goto(base + f"/participation/{quote(hoa_name)}",
                       wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            page.wait_for_timeout(2000)
            title_text = page.text_content("#hoaTitle, h1") or ""
            if title_text and title_text != "Loading...":
                results.passed(f"Participation: page loads ({hoa_name[:30]})")
            else:
                results.warn("Participation: page loads", f"Title: '{title_text}'")
            real_errors = _clear_errors(js_errors)
            if real_errors:
                results.failed("Participation: no JS errors", f"{real_errors[:3]}")
        else:
            results.skipped("Participation page", "No HOAs")
    except Exception as exc:
        results.failed("Participation page", str(exc)[:120])

    # ---- My Proxies content check ----
    js_errors.clear()
    try:
        page.goto(base + "/my-proxies", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(2000)
        # Should have either proxy cards or empty state
        has_cards = page.locator(".card, .proxy-card").count() > 0
        has_empty = page.locator(".empty").count() > 0
        if has_cards or has_empty:
            results.passed("My Proxies: content renders")
        else:
            results.warn("My Proxies: content renders", "Neither cards nor empty state found")
    except Exception as exc:
        results.failed("My Proxies: content renders", str(exc)[:120])

    # ---- Delegate Dashboard content check ----
    js_errors.clear()
    try:
        page.goto(base + "/delegate-dashboard", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(2000)
        has_stats = page.locator(".stat, .stat-row").count() > 0
        has_empty = page.locator(".empty").count() > 0
        has_cards = page.locator(".card").count() > 0
        if has_stats or has_empty or has_cards:
            results.passed("Delegate Dashboard: content renders")
        else:
            results.warn("Delegate Dashboard: content renders", "No stats/cards/empty found")
    except Exception as exc:
        results.failed("Delegate Dashboard: content renders", str(exc)[:120])

    # ---- Logout flow ----
    js_errors.clear()
    try:
        page.goto(base + "/dashboard", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#greeting", timeout=PAGE_TIMEOUT)
        # Find and click logout
        logout_link = page.locator("a:has-text('Logout'), a:has-text('Log out'), button:has-text('Logout')")
        if logout_link.count() > 0:
            logout_link.first.click()
            page.wait_for_timeout(2000)
            # Should be redirected away from dashboard
            if "/dashboard" not in page.url:
                results.passed("Logout: redirected away from dashboard")
            else:
                results.warn("Logout: redirected away from dashboard",
                             f"Still on {page.url}")

            # Verify can't access dashboard anymore
            page.goto(base + "/dashboard", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            page.wait_for_timeout(2000)
            if "/login" in page.url:
                results.passed("Logout: dashboard redirects to login after logout")
            else:
                results.warn("Logout: dashboard redirects to login",
                             f"Ended up on {page.url}")

            # Re-login for any subsequent tests
            page.goto(base + "/login", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            page.fill("#email", email)
            page.fill("#password", password)
            page.click("#loginBtn")
            page.wait_for_url("**/dashboard**", timeout=INTERACTION_TIMEOUT)
        else:
            results.warn("Logout flow", "No logout link found")
    except Exception as exc:
        results.failed("Logout flow", str(exc)[:120])


def group5_interactive(page, base: str, results: SmokeResults, js_errors: list[str]):
    """Interactive feature tests."""
    print("\n--- Group 5: Interactive Features ---")

    # Homepage search
    js_errors.clear()
    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#searchInput", timeout=PAGE_TIMEOUT)
        page.fill("#searchInput", "Parkway")
        page.click("#searchSubmitBtn")
        # Wait for either results or a status message
        page.wait_for_timeout(3000)
        real_errors = _clear_errors(js_errors)
        if real_errors:
            results.failed("Homepage search", f"JS errors: {real_errors[:3]}")
        else:
            results.passed("Homepage search")
    except Exception as exc:
        results.failed("Homepage search", str(exc)[:120])

    # Homepage map check — map is hidden by default, revealed via "browse the HOA map" link
    js_errors.clear()
    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        toggle = page.locator("#toggleMapBtn")
        if toggle.count() > 0:
            toggle.click()
            page.wait_for_selector("#map .leaflet-tile-loaded", timeout=INTERACTION_TIMEOUT)
            results.passed("Homepage map renders with tiles")
        else:
            results.warn("Homepage map renders with tiles", "Map toggle link not found")
    except PwTimeout:
        # Map container may exist but tiles slow to load
        if page.locator("#map").count() > 0:
            results.warn("Homepage map renders with tiles",
                         "Map visible but tiles did not load in time")
        else:
            results.failed("Homepage map renders with tiles", "Map element not found")
    except Exception as exc:
        results.failed("Homepage map renders with tiles", str(exc)[:120])

    # Homepage state filter
    js_errors.clear()
    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#stateFilter", timeout=PAGE_TIMEOUT)
        pills = page.locator("#stateFilter button")
        if pills.count() > 0:
            pills.first.click()
            page.wait_for_timeout(1000)
            real_errors = _clear_errors(js_errors)
            if real_errors:
                results.failed("Homepage state filter", f"JS errors: {real_errors[:3]}")
            else:
                results.passed("Homepage state filter")
        else:
            results.warn("Homepage state filter", "No state filter pills found")
    except Exception as exc:
        results.failed("Homepage state filter", str(exc)[:120])

    # Legal lookup
    js_errors.clear()
    try:
        page.goto(base + "/legal", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_function(
            "document.querySelectorAll('#stateSelect option').length > 2",
            timeout=INTERACTION_TIMEOUT,
        )
        page.select_option("#stateSelect", "NC")
        page.click("#lookupBtn")
        # Wait for result values to change from placeholder
        page.wait_for_function(
            "document.getElementById('resElectronic').textContent.trim() !== '—'",
            timeout=INTERACTION_TIMEOUT,
        )
        results.passed("Legal lookup (NC)")
    except PwTimeout:
        results.failed("Legal lookup (NC)", "Result did not populate")
    except Exception as exc:
        results.failed("Legal lookup (NC)", str(exc)[:120])

    # Forgot password submit
    js_errors.clear()
    try:
        page.goto(base + "/forgot-password", wait_until="domcontentloaded",
                   timeout=PAGE_TIMEOUT)
        page.fill("#email", "smoketest@example.com")
        page.click("#submitBtn")
        page.wait_for_function(
            "document.getElementById('status').classList.contains('ok')",
            timeout=INTERACTION_TIMEOUT,
        )
        status_text = page.text_content("#status")
        if "registered" in (status_text or "").lower():
            results.passed("Forgot password submit")
        else:
            results.warn("Forgot password submit", f"Unexpected message: {status_text}")
    except Exception as exc:
        results.failed("Forgot password submit", str(exc)[:120])

    # Mobile nav hamburger (test on /legal which uses Auth.renderNav)
    js_errors.clear()
    try:
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(base + "/legal", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(500)
        hamburger = page.locator(".nav-hamburger")
        if hamburger.count() > 0:
            hamburger.first.click()
            page.wait_for_timeout(300)
            results.passed("Mobile nav hamburger toggle")
        else:
            results.warn("Mobile nav hamburger toggle", "Hamburger button not found")
        # Reset viewport
        page.set_viewport_size({"width": 1280, "height": 720})
    except Exception as exc:
        results.failed("Mobile nav hamburger toggle", str(exc)[:120])
        page.set_viewport_size({"width": 1280, "height": 720})

    # Password show/hide toggle
    js_errors.clear()
    try:
        page.goto(base + "/login", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        pw_input = page.locator("#password")
        toggle_btn = page.locator("#togglePassword")
        initial_type = pw_input.get_attribute("type")
        toggle_btn.click()
        new_type = pw_input.get_attribute("type")
        if initial_type == "password" and new_type == "text":
            results.passed("Password show/hide toggle")
        else:
            results.failed("Password show/hide toggle",
                           f"Type went from '{initial_type}' to '{new_type}'")
    except Exception as exc:
        results.failed("Password show/hide toggle", str(exc)[:120])

    # ---- Login with wrong password ----
    js_errors.clear()
    try:
        page.goto(base + "/login", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.fill("#email", "smoketest@example.com")
        page.fill("#password", "totallyWrongPassword1!")
        page.click("#loginBtn")
        page.wait_for_function(
            "document.getElementById('status')?.classList.contains('error')",
            timeout=INTERACTION_TIMEOUT,
        )
        results.passed("Login: wrong password shows error")
    except PwTimeout:
        # Check if we ended up on dashboard (meaning login unexpectedly succeeded)
        if "/dashboard" in page.url:
            results.failed("Login: wrong password shows error", "Login succeeded with wrong password!")
        else:
            status_text = page.text_content("#status") or ""
            if status_text:
                results.passed("Login: wrong password shows error")
            else:
                results.failed("Login: wrong password shows error", "No error message shown")
    except Exception as exc:
        results.failed("Login: wrong password shows error", str(exc)[:120])

    # ---- Register validation (client-side) ----
    js_errors.clear()
    try:
        page.goto(base + "/register", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#registerBtn", timeout=PAGE_TIMEOUT)

        # Try with short password
        page.fill("#email", "test@example.com")
        page.fill("#password", "short")
        # Check TOS if present
        tos = page.locator("#tos, #tosCheckbox, input[type='checkbox']")
        if tos.count() > 0:
            tos.first.check()
        page.click("#registerBtn")
        page.wait_for_timeout(1500)
        status_text = page.text_content("#status") or ""
        if "8" in status_text or "short" in status_text.lower() or "password" in status_text.lower():
            results.passed("Register: weak password validation")
        else:
            # May have hit server-side validation instead
            results.warn("Register: weak password validation",
                         f"Status: '{status_text[:80]}'")
    except Exception as exc:
        results.failed("Register: weak password validation", str(exc)[:120])

    # ---- Register password toggle ----
    js_errors.clear()
    try:
        page.goto(base + "/register", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        pw_input = page.locator("#password")
        toggle_btn = page.locator("#togglePassword")
        if toggle_btn.count() > 0:
            initial_type = pw_input.get_attribute("type")
            toggle_btn.click()
            new_type = pw_input.get_attribute("type")
            if initial_type == "password" and new_type == "text":
                results.passed("Register: password show/hide toggle")
            else:
                results.failed("Register: password show/hide toggle",
                               f"Type went from '{initial_type}' to '{new_type}'")
        else:
            results.warn("Register: password show/hide toggle", "Toggle button not found")
    except Exception as exc:
        results.failed("Register: password show/hide toggle", str(exc)[:120])

    # ---- Full registration flow ----
    js_errors.clear()
    try:
        page.goto(base + "/register", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_selector("#registerBtn", timeout=PAGE_TIMEOUT)
        reg_email = f"smoke-reg-{int(time.time())}@test.hoatest.invalid"
        reg_password = f"SmokeReg{int(time.time())}!"

        name_field = page.locator("#name, #displayName, #fullName")
        if name_field.count() > 0:
            name_field.first.fill("Smoke Register Test")
        page.fill("#email", reg_email)
        page.fill("#password", reg_password)
        tos = page.locator("#tos, #tosCheckbox, input[type='checkbox']")
        if tos.count() > 0:
            tos.first.check()
        page.click("#registerBtn")
        page.wait_for_url("**/dashboard**", timeout=INTERACTION_TIMEOUT)
        results.passed("Registration: full flow to dashboard")

        # Verify greeting is shown
        try:
            page.wait_for_selector("#greeting", timeout=PAGE_TIMEOUT)
            results.passed("Registration: dashboard greeting after register")
        except PwTimeout:
            results.warn("Registration: dashboard greeting", "Greeting not found")

        # Log out after registration test (clear localStorage)
        page.evaluate("localStorage.clear()")
        page.goto(base + "/login", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    except PwTimeout:
        results.failed("Registration: full flow",
                       f"Timeout — ended on {page.url}")
    except Exception as exc:
        results.failed("Registration: full flow", str(exc)[:120])

    # ---- HOA profile page (public) ----
    hoas_list = []
    js_errors.clear()
    try:
        req = urllib.request.Request(f"{base}/hoas/summary?page=1&per_page=1")
        with urllib.request.urlopen(req, timeout=15) as resp:
            summary_data = json.loads(resp.read())
        hoas_list = _hoa_list_from_summary(summary_data)
        if hoas_list:
            hoa_name = hoas_list[0]["name"]
            page.goto(base + f"/hoa/{quote(hoa_name)}", wait_until="domcontentloaded",
                       timeout=PAGE_TIMEOUT)
            page.wait_for_timeout(2500)
            title = page.text_content("#hoaTitle, h1") or ""
            if title and title != "Loading...":
                results.passed(f"HOA profile (public): loads ({hoa_name[:30]})")
            else:
                results.warn("HOA profile (public): loads", f"Title: '{title}'")
            real_errors = _clear_errors(js_errors)
            if real_errors:
                results.failed("HOA profile (public): JS errors", f"{real_errors[:3]}")
        else:
            results.skipped("HOA profile (public)", "No HOAs in summary")
    except Exception as exc:
        results.failed("HOA profile (public)", str(exc)[:120])

    # ---- Participation page (public) ----
    js_errors.clear()
    try:
        if hoas_list:
            hoa_name = hoas_list[0]["name"]
            page.goto(base + f"/participation/{quote(hoa_name)}",
                       wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            page.wait_for_timeout(2500)
            title = page.text_content("#hoaTitle, h1") or ""
            if title and title != "Loading...":
                results.passed(f"Participation (public): loads ({hoa_name[:30]})")
            else:
                results.warn("Participation (public): loads", f"Title: '{title}'")
            real_errors = _clear_errors(js_errors)
            if real_errors:
                results.failed("Participation (public): JS errors", f"{real_errors[:3]}")
        else:
            results.skipped("Participation (public)", "No HOAs")
    except Exception as exc:
        results.failed("Participation (public)", str(exc)[:120])

    # ---- Verify proxy page — invalid code ----
    js_errors.clear()
    try:
        page.goto(base + "/verify-proxy", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        code_input = page.locator("#code, #verifyCode, input[type='text']")
        if code_input.count() > 0:
            code_input.first.fill("INVALIDCODE999")
            verify_btn = page.locator("#verifyBtn, button:has-text('Verify'), button.primary")
            if verify_btn.count() > 0:
                verify_btn.first.click()
                page.wait_for_timeout(3000)
                body_text = page.text_content("body") or ""
                if "not found" in body_text.lower() or "invalid" in body_text.lower() or "error" in body_text.lower():
                    results.passed("Verify proxy: invalid code handled")
                else:
                    results.warn("Verify proxy: invalid code handled",
                                 f"No error message visible")
            else:
                results.warn("Verify proxy: invalid code", "No verify button found")
        else:
            results.warn("Verify proxy: invalid code", "No code input found")
    except Exception as exc:
        results.failed("Verify proxy: invalid code", str(exc)[:120])

    # ---- Reset password — no token ----
    js_errors.clear()
    try:
        page.goto(base + "/reset-password", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(1500)
        body_text = page.text_content("body") or ""
        if "token" in body_text.lower() or "invalid" in body_text.lower() or "expired" in body_text.lower() or "error" in body_text.lower():
            results.passed("Reset password: no token shows error")
        else:
            results.warn("Reset password: no token shows error",
                         "No clear error message found")
    except Exception as exc:
        results.failed("Reset password: no token", str(exc)[:120])

    # ---- Reset password — invalid token ----
    js_errors.clear()
    try:
        page.goto(base + "/reset-password?token=invalidtoken123",
                   wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(1000)
        pw_field = page.locator("#password, #newPassword")
        confirm_field = page.locator("#confirmPassword, #confirm")
        if pw_field.count() > 0 and confirm_field.count() > 0:
            pw_field.first.fill("NewPassword123!")
            confirm_field.first.fill("NewPassword123!")
            submit_btn = page.locator("#submitBtn, button.primary, button:has-text('Reset')")
            if submit_btn.count() > 0:
                submit_btn.first.click()
                page.wait_for_timeout(3000)
                body_text = page.text_content("body") or ""
                if "invalid" in body_text.lower() or "expired" in body_text.lower() or "error" in body_text.lower():
                    results.passed("Reset password: invalid token rejected")
                else:
                    results.warn("Reset password: invalid token rejected",
                                 "No clear error after submit")
            else:
                results.warn("Reset password: invalid token", "No submit button found")
        else:
            # Page may show error immediately for missing/invalid token
            results.passed("Reset password: invalid token — form not shown")
    except Exception as exc:
        results.failed("Reset password: invalid token", str(exc)[:120])


def group6_email(base: str, results: SmokeResults):
    """Email delivery tests via testmail.app."""
    print("\n--- Group 6: Email Delivery (testmail.app) ---")

    api_key = os.environ.get("TESTMAIL_API_KEY", "")
    namespace = os.environ.get("TESTMAIL_NAMESPACE", "")

    if not api_key or not namespace:
        results.skipped("Forgot password email delivery",
                        "TESTMAIL_API_KEY / TESTMAIL_NAMESPACE not set")
        results.skipped("Reset password end-to-end",
                        "TESTMAIL_API_KEY / TESTMAIL_NAMESPACE not set")
        return

    tag = f"smoke{int(time.time())}"
    test_email = f"{namespace}.{tag}@inbox.testmail.app"
    temp_password = f"SmokeSetup{int(time.time())}!"

    # Register a temporary account with the testmail address
    try:
        payload = json.dumps({
            "email": test_email,
            "password": temp_password,
            "display_name": "Smoke Email Test",
        }).encode()
        req = urllib.request.Request(
            f"{base}/auth/register",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                results.failed("Forgot password email delivery",
                               f"Could not register test account: HTTP {resp.status}")
                results.skipped("Reset password end-to-end", "No test account")
                return
    except Exception as exc:
        results.failed("Forgot password email delivery",
                       f"Could not register test account: {exc}")
        results.skipped("Reset password end-to-end", "No test account")
        return

    # Small delay to let registration complete
    time.sleep(1)

    # Record time after registration so we skip the verification email
    forgot_timestamp = int(time.time() * 1000)

    # Send forgot-password request
    try:
        payload = json.dumps({"email": test_email}).encode()
        req = urllib.request.Request(
            f"{base}/auth/forgot-password",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                results.failed("Forgot password email delivery",
                               f"API returned {resp.status}")
                return
    except Exception as exc:
        results.failed("Forgot password email delivery", f"API call failed: {exc}")
        return

    # Poll testmail for the email
    email_data = _testmail_fetch(api_key, namespace, tag, timeout=30,
                                 timestamp_from=forgot_timestamp)
    if not email_data:
        results.warn("Forgot password email delivery",
                     "Email not received within 30s — check EMAIL_PROVIDER config")
        results.skipped("Reset password end-to-end", "No email received")
        return

    results.passed("Forgot password email delivery")

    # Extract reset token
    html_body = email_data.get("html", "") or email_data.get("text", "")
    token = _extract_reset_token(html_body)
    if not token:
        results.failed("Reset password end-to-end",
                       "Could not extract reset token from email")
        return

    # Use the token to reset the password
    new_password = f"SmokeTmp{int(time.time())}!"
    try:
        payload = json.dumps({"token": token, "password": new_password}).encode()
        req = urllib.request.Request(
            f"{base}/auth/reset-password",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                results.passed("Reset password end-to-end")
            else:
                results.failed("Reset password end-to-end", f"Response: {data}")
    except Exception as exc:
        results.failed("Reset password end-to-end", f"Reset API failed: {exc}")


def group7_api(base: str, results: SmokeResults):
    """API health checks — direct HTTP, no browser needed."""
    print("\n--- Group 7: API Health Checks ---")

    endpoints = [
        ("/healthz", "Healthz"),
        ("/hoas/summary?page=1&per_page=1", "HOA summary"),
        ("/hoas/map-points", "HOA map points"),
        ("/law/jurisdictions", "Law jurisdictions"),
        ("/hoas/states", "HOA states"),
        ("/hoas/locations", "HOA locations"),
        ("/law/NC/proxy-electronic", "Law NC proxy-electronic"),
        ("/law/proxy-electronic/summary", "Law proxy-electronic summary"),
    ]
    for path, name in endpoints:
        try:
            req = urllib.request.Request(f"{base}{path}")
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    results.passed(f"API: {name}")
                else:
                    results.failed(f"API: {name}", f"HTTP {resp.status}")
        except Exception as exc:
            results.failed(f"API: {name}", str(exc)[:120])

    # Dynamic endpoints that require a known HOA name
    try:
        req = urllib.request.Request(f"{base}/hoas/summary?page=1&per_page=1")
        with urllib.request.urlopen(req, timeout=15) as resp:
            summary_data = json.loads(resp.read())
        hoas = _hoa_list_from_summary(summary_data)
        if hoas:
            hoa_name = hoas[0]["name"]
            slug = hoa_name.lower().replace(" ", "-").replace("/", "-")

            # HOA documents
            try:
                req = urllib.request.Request(f"{base}/hoas/{quote(hoa_name)}/documents")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        results.passed(f"API: HOA documents")
                    else:
                        results.failed("API: HOA documents", f"HTTP {resp.status}")
            except Exception as exc:
                results.failed("API: HOA documents", str(exc)[:120])

            # HOA resolve
            try:
                req = urllib.request.Request(f"{base}/hoas/resolve/{quote(slug)}")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        results.passed("API: HOA resolve slug")
                    else:
                        results.warn("API: HOA resolve slug", f"HTTP {resp.status}")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    results.warn("API: HOA resolve slug", "404 — slug format may differ")
                else:
                    results.failed("API: HOA resolve slug", f"HTTP {exc.code}")
            except Exception as exc:
                results.failed("API: HOA resolve slug", str(exc)[:120])

            # HOA location
            try:
                req = urllib.request.Request(f"{base}/hoas/{quote(hoa_name)}/location")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    results.passed("API: HOA location")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    results.passed("API: HOA location (none set — 404 expected)")
                else:
                    results.failed("API: HOA location", f"HTTP {exc.code}")
            except Exception as exc:
                results.failed("API: HOA location", str(exc)[:120])
        else:
            results.skipped("API: HOA dynamic endpoints", "No HOAs in summary")
    except Exception as exc:
        results.failed("API: HOA dynamic endpoints", str(exc)[:120])


# ---------------------------------------------------------------------------
# Multi-user API helpers
# ---------------------------------------------------------------------------

def _api_call(base: str, method: str, path: str, body: dict | None = None,
              token: str | None = None) -> tuple[int, dict]:
    """Make an API call. Returns (status_code, response_json)."""
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body_text = exc.read().decode()
            return exc.code, json.loads(body_text)
        except Exception:
            return exc.code, {"detail": str(exc)}


def _register_temp(base: str, suffix: str, display_name: str) -> tuple[str, int]:
    """Register a throwaway account, return (token, user_id)."""
    email = f"smoke-multi-{suffix}-{int(time.time())}@test.hoatest.invalid"
    status, data = _api_call(base, "POST", "/auth/register", {
        "email": email, "password": f"SmokeMulti{int(time.time())}!",
        "display_name": display_name,
    })
    if status != 200:
        raise RuntimeError(f"Register failed ({status}): {data}")
    return data["token"], data["user_id"]


def group8_multi_user_api(base: str, results: SmokeResults):
    """Multi-user workflows via API — proposal lifecycle + proxy delegation."""
    print("\n--- Group 8: Multi-User API Tests ---")

    # ---- Setup: 3 users + HOA + memberships ----
    try:
        t1, uid1 = _register_temp(base, "creator", "Alice Smoketest")
        t2, uid2 = _register_temp(base, "cosigner1", "Bob Smoketest")
        t3, uid3 = _register_temp(base, "cosigner2", "Carol Smoketest")
        results.passed("Multi-user: register 3 accounts")
    except Exception as exc:
        results.failed("Multi-user: register 3 accounts", str(exc)[:120])
        return

    # Claim membership in an existing HOA (use the first available one)
    try:
        status, summary = _api_call(base, "GET", "/hoas/summary?page=1&per_page=1")
        _hoas = _hoa_list_from_summary(summary) if status == 200 else []
        if not _hoas:
            results.failed("Multi-user: find HOA for testing", "No HOAs available")
            return
        hoa_id = _hoas[0]["id"]
        hoa_name = _hoas[0]["name"]
        for tok in (t1, t2, t3):
            s, _ = _api_call(base, "POST", f"/user/hoas/{hoa_id}/claim",
                             {"unit_number": "SMOKE"}, tok)
            if s not in (200, 409):  # 409 = already claimed
                results.failed("Multi-user: claim membership", f"HTTP {s}")
                return
        results.passed(f"Multi-user: claim membership ({hoa_name[:30]})")
    except Exception as exc:
        results.failed("Multi-user: claim membership", str(exc)[:120])
        return

    # ---- Proposal lifecycle ----
    # Step 1: Create proposal
    try:
        s, proposal = _api_call(base, "POST", "/proposals", {
            "hoa_id": hoa_id,
            "title": f"Smoke test proposal {int(time.time())}",
            "description": "Automated multi-user smoke test. Safe to ignore or archive.",
            "category": "Other",
        }, t1)
        if s != 200:
            results.failed("Multi-user: create proposal", f"HTTP {s}: {proposal.get('detail')}")
            return
        proposal_id = proposal["id"]
        share_code = proposal["share_code"]
        assert proposal["status"] == "private"
        results.passed("Multi-user: create proposal (private)")
    except Exception as exc:
        results.failed("Multi-user: create proposal", str(exc)[:120])
        return

    # Step 2: First co-signer
    try:
        s, resp = _api_call(base, "POST", f"/proposals/cosign/{share_code}", None, t2)
        if s != 200:
            results.failed("Multi-user: first co-sign", f"HTTP {s}: {resp.get('detail')}")
            return
        assert resp["cosigner_count"] == 1
        assert resp["status"] == "private"
        results.passed("Multi-user: first co-sign (still private)")
    except Exception as exc:
        results.failed("Multi-user: first co-sign", str(exc)[:120])
        return

    # Step 3: Second co-signer → publishes
    try:
        s, resp = _api_call(base, "POST", f"/proposals/cosign/{share_code}", None, t3)
        if s != 200:
            results.failed("Multi-user: second co-sign (publishes)", f"HTTP {s}: {resp.get('detail')}")
            return
        assert resp["cosigner_count"] == 2
        assert resp["status"] == "public"
        results.passed("Multi-user: second co-sign (now public)")
    except Exception as exc:
        results.failed("Multi-user: second co-sign", str(exc)[:120])
        return

    # Step 4: Upvote the public proposal (by cosigner 1)
    try:
        s, resp = _api_call(base, "POST", f"/proposals/{proposal_id}/upvote", None, t2)
        if s != 200:
            results.failed("Multi-user: upvote proposal", f"HTTP {s}: {resp.get('detail')}")
            return
        assert resp["upvote_count"] >= 1
        assert resp["user_upvoted"] is True
        results.passed("Multi-user: upvote public proposal")
    except Exception as exc:
        results.failed("Multi-user: upvote proposal", str(exc)[:120])
        return

    # Step 5: Named co-sign on public proposal (by cosigner 1, who already cosigned privately)
    # This should 409 since they already cosigned. Register a 4th user instead.
    try:
        t4, uid4 = _register_temp(base, "supporter", "Dave Smoketest")
        _api_call(base, "POST", f"/user/hoas/{hoa_id}/claim", {"unit_number": "SMOKE"}, t4)
        s, resp = _api_call(base, "POST", f"/proposals/{proposal_id}/cosign", None, t4)
        if s != 200:
            results.failed("Multi-user: public co-sign by name", f"HTTP {s}: {resp.get('detail')}")
            return
        assert resp["user_cosigned"] is True
        assert "Dave Smoketest" in resp.get("cosigners", [])
        results.passed("Multi-user: public co-sign (named supporter)")
    except Exception as exc:
        results.failed("Multi-user: public co-sign by name", str(exc)[:120])
        return

    # Step 6: Verify the proposal shows up in HOA feed
    try:
        s, feed = _api_call(base, "GET", f"/hoas/{hoa_id}/proposals", None, t2)
        if s != 200:
            results.failed("Multi-user: list HOA proposals", f"HTTP {s}")
            return
        found = [p for p in feed if p["id"] == proposal_id]
        if not found:
            results.failed("Multi-user: proposal in HOA feed", "Proposal not in feed")
            return
        results.passed("Multi-user: proposal visible in HOA feed")
    except Exception as exc:
        results.failed("Multi-user: proposal in HOA feed", str(exc)[:120])
        return

    # Step 7: Withdraw (archive) the proposal — cleanup
    try:
        s, _ = _api_call(base, "DELETE", f"/proposals/{proposal_id}", None, t1)
        if s != 200:
            results.warn("Multi-user: withdraw proposal", f"HTTP {s}")
        else:
            results.passed("Multi-user: withdraw proposal (cleanup)")
    except Exception as exc:
        results.warn("Multi-user: withdraw proposal", str(exc)[:120])

    # ---- Proxy delegation lifecycle ----
    # Step 1: Register user2 as delegate
    try:
        s, delegate = _api_call(base, "POST", "/delegates/register", {
            "hoa_id": hoa_id,
            "bio": "Smoke test delegate",
            "contact_email": "smoke-delegate@test.hoatest.invalid",
        }, t2)
        if s == 409:
            # Already a delegate from a previous run — find their delegate record
            s2, delegates = _api_call(base, "GET", f"/hoas/{hoa_id}/delegates", None, t1)
            delegate = next((d for d in delegates if d["user_id"] == uid2), None)
            if not delegate:
                results.failed("Multi-user: register delegate", "409 but delegate not found")
                return
            results.passed("Multi-user: delegate already registered")
        elif s != 200:
            results.failed("Multi-user: register delegate", f"HTTP {s}: {delegate.get('detail')}")
            return
        else:
            results.passed("Multi-user: register as delegate")
        delegate_user_id = uid2
    except Exception as exc:
        results.failed("Multi-user: register delegate", str(exc)[:120])
        return

    # Step 2: user1 creates proxy assignment to user2
    try:
        s, proxy = _api_call(base, "POST", "/proxies", {
            "hoa_id": hoa_id,
            "delegate_user_id": delegate_user_id,
        }, t1)
        if s == 409:
            results.warn("Multi-user: create proxy", "User already has active proxy — skipping sign/revoke")
            return
        if s != 200:
            results.failed("Multi-user: create proxy", f"HTTP {s}: {proxy.get('detail')}")
            return
        proxy_id = proxy["id"]
        assert proxy["status"] == "draft"
        results.passed("Multi-user: create proxy (draft)")
    except Exception as exc:
        results.failed("Multi-user: create proxy", str(exc)[:120])
        return

    # Step 3: Revoke the draft proxy (signing requires email verification, so we test revoke)
    try:
        s, resp = _api_call(base, "POST", f"/proxies/{proxy_id}/revoke", {"reason": "smoke test cleanup"}, t1)
        if s != 200:
            results.failed("Multi-user: revoke proxy", f"HTTP {s}: {resp.get('detail')}")
            return
        assert resp["status"] == "revoked"
        results.passed("Multi-user: revoke proxy")
    except Exception as exc:
        results.failed("Multi-user: revoke proxy", str(exc)[:120])

    # Step 4: Check proxy stats
    try:
        s, stats = _api_call(base, "GET", f"/hoas/{hoa_id}/proxy-stats", None, t1)
        if s != 200:
            results.failed("Multi-user: proxy stats", f"HTTP {s}")
            return
        results.passed("Multi-user: proxy stats endpoint")
    except Exception as exc:
        results.failed("Multi-user: proxy stats", str(exc)[:120])

    # ---- Account: password change ----
    # Register a fresh user, change password, verify old fails and new works
    try:
        suffix = f"pwchange-{int(time.time())}"
        old_pw = f"OldPass{int(time.time())}!"
        new_pw = f"NewPass{int(time.time())}!"
        email_pw = f"smoke-{suffix}@test.hoatest.invalid"
        s, reg = _api_call(base, "POST", "/auth/register", {
            "email": email_pw, "password": old_pw, "display_name": "PwChange Test",
        })
        if s != 200:
            results.failed("Account: password change", f"Register failed ({s})")
        else:
            pw_token = reg["token"]

            # Change password
            s, resp = _api_call(base, "PUT", "/auth/me",
                                {"current_password": old_pw, "new_password": new_pw},
                                pw_token)
            if s != 200:
                results.failed("Account: password change", f"PUT /auth/me returned {s}")
            else:
                # Old password must fail
                s_old, _ = _api_call(base, "POST", "/auth/login",
                                     {"email": email_pw, "password": old_pw})
                # New password must work
                s_new, _ = _api_call(base, "POST", "/auth/login",
                                     {"email": email_pw, "password": new_pw})
                if s_old == 401 and s_new == 200:
                    results.passed("Account: password change")
                else:
                    results.failed("Account: password change",
                                   f"old_login={s_old} (want 401), new_login={s_new} (want 200)")

            # Wrong current password must be rejected
            s_bad, _ = _api_call(base, "PUT", "/auth/me",
                                 {"current_password": "totallyWrong1!", "new_password": "Whatever123!"},
                                 pw_token)
            if s_bad == 403:
                results.passed("Account: wrong current password rejected")
            else:
                results.failed("Account: wrong current password rejected", f"HTTP {s_bad} (want 403)")

            # Missing current password must be rejected
            s_miss, _ = _api_call(base, "PUT", "/auth/me",
                                  {"new_password": "Whatever123!"},
                                  pw_token)
            if s_miss == 400:
                results.passed("Account: missing current password rejected")
            else:
                results.failed("Account: missing current password rejected", f"HTTP {s_miss} (want 400)")
    except Exception as exc:
        results.failed("Account: password change", str(exc)[:120])

    # ---- Full proxy sign + deliver + verify lifecycle ----
    try:
        t_grantor, uid_grantor = _register_temp(base, "proxy-lifecycle", "Eve Smoketest")
        t_delegate_user, uid_delegate = _register_temp(base, "proxy-delegate", "Frank Smoketest")

        # Both claim membership
        s, summary = _api_call(base, "GET", "/hoas/summary?page=1&per_page=1")
        _lc_hoas = _hoa_list_from_summary(summary) if s == 200 else []
        if _lc_hoas:
            lifecycle_hoa_id = _lc_hoas[0]["id"]
            lifecycle_hoa_name = _lc_hoas[0]["name"]
            for tok in (t_grantor, t_delegate_user):
                _api_call(base, "POST", f"/user/hoas/{lifecycle_hoa_id}/claim",
                          {"unit_number": "SMOKE"}, tok)

            # Register delegate
            s, delegate_resp = _api_call(base, "POST", "/delegates/register", {
                "hoa_id": lifecycle_hoa_id,
                "bio": "Lifecycle test delegate",
                "contact_email": "lifecycle-delegate@test.hoatest.invalid",
            }, t_delegate_user)
            delegate_id = delegate_resp.get("id") if s == 200 else None
            if s == 409:
                # Already registered, find delegate
                s2, delegates = _api_call(base, "GET",
                                          f"/hoas/{lifecycle_hoa_id}/delegates", None, t_grantor)
                delegate_id = next((d["id"] for d in delegates
                                    if d.get("user_id") == uid_delegate), None)

            if delegate_id:
                # Fetch delegate profile
                s, d_profile = _api_call(base, "GET", f"/delegates/{delegate_id}")
                if s == 200 and d_profile.get("id"):
                    results.passed("Proxy lifecycle: delegate profile fetch")
                else:
                    results.failed("Proxy lifecycle: delegate profile fetch", f"HTTP {s}")

            # Create proxy
            s, proxy = _api_call(base, "POST", "/proxies", {
                "hoa_id": lifecycle_hoa_id,
                "delegate_user_id": uid_delegate,
            }, t_grantor)
            if s == 200 and proxy.get("status") == "draft":
                proxy_id = proxy["id"]
                results.passed("Proxy lifecycle: create (draft)")

                # Sign proxy (requires email verification — 403 is expected for temp accounts)
                s, signed = _api_call(base, "POST", f"/proxies/{proxy_id}/sign",
                                      None, t_grantor)
                if s == 403 and "verification" in str(signed.get("detail", "")).lower():
                    results.passed("Proxy lifecycle: sign blocked without email verification (expected)")
                    # Still test /proxies/mine and /proxies/delegated with draft proxy
                    s, my_proxies = _api_call(base, "GET", "/proxies/mine", None, t_grantor)
                    if s == 200:
                        found = [p for p in my_proxies if p["id"] == proxy_id]
                        if found and found[0]["status"] == "draft":
                            results.passed("Proxy lifecycle: draft appears in /proxies/mine")
                        else:
                            results.warn("Proxy lifecycle: /proxies/mine", "Proxy not found")

                    s, delegated = _api_call(base, "GET", "/proxies/delegated",
                                             None, t_delegate_user)
                    if s == 200:
                        found = [p for p in delegated if p["id"] == proxy_id]
                        if found:
                            results.passed("Proxy lifecycle: draft appears in /proxies/delegated")
                        else:
                            results.warn("Proxy lifecycle: /proxies/delegated", "Not found")

                    # Revoke the draft for cleanup
                    _api_call(base, "POST", f"/proxies/{proxy_id}/revoke",
                              {"reason": "smoke test cleanup"}, t_grantor)

                elif s == 200 and signed.get("status") == "signed":
                    results.passed("Proxy lifecycle: sign")

                    # Verify via /proxies/mine
                    s, my_proxies = _api_call(base, "GET", "/proxies/mine", None, t_grantor)
                    if s == 200:
                        found = [p for p in my_proxies if p["id"] == proxy_id]
                        if found and found[0]["status"] == "signed":
                            results.passed("Proxy lifecycle: appears in /proxies/mine")
                        else:
                            results.warn("Proxy lifecycle: /proxies/mine",
                                         "Proxy not found or wrong status")

                    # Verify via /proxies/delegated
                    s, delegated = _api_call(base, "GET", "/proxies/delegated",
                                             None, t_delegate_user)
                    if s == 200:
                        found = [p for p in delegated if p["id"] == proxy_id]
                        if found:
                            results.passed("Proxy lifecycle: appears in /proxies/delegated")
                        else:
                            results.warn("Proxy lifecycle: /proxies/delegated",
                                         "Proxy not found in delegated list")

                    # Deliver proxy
                    s, delivered = _api_call(base, "POST",
                                             f"/proxies/{proxy_id}/deliver", None, t_grantor)
                    if s == 200 and delivered.get("status") == "delivered":
                        results.passed("Proxy lifecycle: deliver")

                        # Verify via verification code
                        vcode = delivered.get("verification_code")
                        if vcode:
                            s, verified = _api_call(base, "GET",
                                                     f"/proxies/verify/{vcode}")
                            if s == 200 and verified.get("valid"):
                                results.passed("Proxy lifecycle: verification code valid")
                            else:
                                results.warn("Proxy lifecycle: verification code",
                                             f"HTTP {s}, valid={verified.get('valid')}")
                        else:
                            results.warn("Proxy lifecycle: verification code",
                                         "No code in deliver response")
                    else:
                        results.failed("Proxy lifecycle: deliver",
                                       f"HTTP {s}, status={delivered.get('status')}")
                else:
                    results.failed("Proxy lifecycle: sign",
                                   f"HTTP {s}, status={signed.get('status')}")
            elif s == 409:
                results.warn("Proxy lifecycle", "User already has active proxy — skipping")
            else:
                results.failed("Proxy lifecycle: create",
                               f"HTTP {s}: {proxy.get('detail')}")

            # Check proxy status endpoint
            s, pstatus = _api_call(base, "GET",
                                    f"/hoas/{lifecycle_hoa_id}/proxy-status", None, t_grantor)
            if s == 200:
                results.passed("API: proxy status endpoint")
            else:
                results.warn("API: proxy status endpoint", f"HTTP {s}")

        else:
            results.skipped("Proxy lifecycle", "No HOAs available")
    except Exception as exc:
        results.failed("Proxy lifecycle", str(exc)[:120])

    # ---- Profile update via API ----
    try:
        s, me = _api_call(base, "GET", "/auth/me", None, t1)
        if s == 200:
            old_name = me.get("display_name", "")
            new_name = f"Updated{int(time.time())}"
            s, updated = _api_call(base, "PUT", "/auth/me",
                                    {"display_name": new_name}, t1)
            if s == 200:
                s, me2 = _api_call(base, "GET", "/auth/me", None, t1)
                if s == 200 and me2.get("display_name") == new_name:
                    results.passed("Profile update: display_name round-trip")
                else:
                    results.failed("Profile update: display_name",
                                   f"Expected '{new_name}', got '{me2.get('display_name')}'")
                # Restore
                _api_call(base, "PUT", "/auth/me", {"display_name": old_name}, t1)
            else:
                results.failed("Profile update: PUT /auth/me", f"HTTP {s}")
    except Exception as exc:
        results.failed("Profile update", str(exc)[:120])

    # ---- HOA documents endpoint ----
    try:
        s, docs = _api_call(base, "GET",
                             f"/hoas/{quote(hoa_name)}/documents")
        if s == 200:
            results.passed("API: HOA documents (authed)")
        else:
            results.warn("API: HOA documents (authed)", f"HTTP {s}")
    except Exception as exc:
        results.failed("API: HOA documents (authed)", str(exc)[:120])

    # ---- Participation submit + read ----
    try:
        part_data = {
            "meeting_date": "2024-01-15",
            "meeting_type": "annual",
            "total_units": 100,
            "votes_cast": 45,
            "quorum_required": True,
            "quorum_met": True,
            "notes": "Automated smoke test entry",
        }
        s, part = _api_call(base, "POST",
                             f"/hoas/{hoa_id}/participation", part_data, t1)
        if s == 200:
            results.passed("Participation: submit meeting data")

            # Read back
            s, history = _api_call(base, "GET",
                                    f"/hoas/{hoa_id}/participation", None, t1)
            if s == 200 and isinstance(history, list):
                results.passed("Participation: read history")
            else:
                results.warn("Participation: read history", f"HTTP {s}")

            # Magic number
            s, magic = _api_call(base, "GET",
                                  f"/hoas/{hoa_id}/magic-number", None, t1)
            if s == 200:
                results.passed("Participation: magic number endpoint")
            else:
                results.warn("Participation: magic number", f"HTTP {s}")
        elif s == 409:
            results.warn("Participation: submit", "Duplicate entry — skipping read tests")
        else:
            results.warn("Participation: submit", f"HTTP {s}: {part.get('detail', '')[:80]}")
    except Exception as exc:
        results.failed("Participation", str(exc)[:120])


def group9_multi_user_browser(page1, page2, page3, base: str,
                               results: SmokeResults, js_errors: list[list[str]],
                               creds: list[tuple[str, str]]):
    """Multi-user browser tests — full proposal + proxy flows through the UI.

    Requires 3 pre-existing accounts (SMOKE_TEST_EMAIL, _2, _3) that are
    members of at least one shared HOA.
    """
    print("\n--- Group 9: Multi-User Browser Tests ---")

    pages = [page1, page2, page3]
    emails = [c[0] for c in creds]
    passwords = [c[1] for c in creds]

    # ---- Login all 3 users ----
    for i, (page, email, pw) in enumerate(zip(pages, emails, passwords)):
        js_errors[i].clear()
        try:
            page.goto(f"{base}/login", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            page.fill("#email", email)
            page.fill("#password", pw)
            page.click("#loginBtn")
            page.wait_for_url("**/dashboard**", timeout=INTERACTION_TIMEOUT)
        except PwTimeout:
            if "/dashboard" not in page.url:
                results.failed(f"Multi-user browser: login user {i+1}", "Timeout")
                return
        except Exception as exc:
            results.failed(f"Multi-user browser: login user {i+1}", str(exc)[:120])
            return
    results.passed("Multi-user browser: login 3 users")

    # ---- User 1 creates a proposal ----
    try:
        page1.goto(f"{base}/proposals", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page1.wait_for_selector("#hoaSelect", timeout=PAGE_TIMEOUT)
        # Wait for HOA options to load
        page1.wait_for_function(
            "document.querySelectorAll('#hoaSelect option').length >= 1 && "
            "document.querySelector('#hoaSelect option').value !== ''",
            timeout=INTERACTION_TIMEOUT,
        )
        # Open the new proposal form
        page1.click("#newProposalDetails summary")
        page1.wait_for_timeout(300)
        title = f"Browser smoke test {int(time.time())}"
        page1.fill("#npTitle", title)
        page1.fill("#npDesc", "Automated browser multi-user smoke test. Safe to ignore or archive.")
        page1.click("#submitProposalBtn")
        page1.wait_for_selector("#shareCodeDisplay", timeout=INTERACTION_TIMEOUT)
        page1.wait_for_function(
            "document.getElementById('shareCodeDisplay').textContent.trim().length === 4",
            timeout=INTERACTION_TIMEOUT,
        )
        share_code = page1.text_content("#shareCodeDisplay").strip()
        if len(share_code) != 4:
            results.failed("Multi-user browser: create proposal", f"Bad share code: '{share_code}'")
            return
        results.passed(f"Multi-user browser: create proposal (code: {share_code})")
    except Exception as exc:
        results.failed("Multi-user browser: create proposal", str(exc)[:120])
        return

    # ---- User 2 co-signs via share code ----
    try:
        page2.goto(f"{base}/proposals", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page2.wait_for_selector("#hoaSelect", timeout=PAGE_TIMEOUT)
        page2.wait_for_timeout(500)
        # Open co-sign section
        page2.click("#cosignDetails summary")
        page2.wait_for_timeout(300)
        page2.fill("#cosignCodeInput", share_code)
        page2.click("#lookupCodeBtn")
        page2.wait_for_function(
            "document.getElementById('cosignLookupStatus').classList.contains('ok')",
            timeout=INTERACTION_TIMEOUT,
        )
        results.passed("Multi-user browser: user 2 co-signs")
    except Exception as exc:
        results.failed("Multi-user browser: user 2 co-signs", str(exc)[:120])
        return

    # ---- User 3 co-signs → publishes ----
    try:
        page3.goto(f"{base}/proposals", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page3.wait_for_selector("#hoaSelect", timeout=PAGE_TIMEOUT)
        page3.wait_for_timeout(500)
        page3.click("#cosignDetails summary")
        page3.wait_for_timeout(300)
        page3.fill("#cosignCodeInput", share_code)
        page3.click("#lookupCodeBtn")
        page3.wait_for_function(
            "document.getElementById('cosignLookupStatus').classList.contains('ok')",
            timeout=INTERACTION_TIMEOUT,
        )
        # Check the co-sign preview shows "public"
        preview_text = page3.text_content("#cosignPreview")
        if "public" in (preview_text or "").lower():
            results.passed("Multi-user browser: user 3 co-signs (now public)")
        else:
            results.warn("Multi-user browser: user 3 co-signs",
                         f"Status not 'public' in preview: {(preview_text or '')[:80]}")
    except Exception as exc:
        results.failed("Multi-user browser: user 3 co-signs", str(exc)[:120])
        return

    # ---- User 2 sees proposal in public feed and upvotes ----
    try:
        page2.goto(f"{base}/proposals", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page2.wait_for_selector("#hoaSelect", timeout=PAGE_TIMEOUT)
        page2.wait_for_function(
            "document.querySelectorAll('#hoaSelect option').length >= 1 && "
            "document.querySelector('#hoaSelect option').value !== ''",
            timeout=INTERACTION_TIMEOUT,
        )
        # Trigger load
        page2.evaluate("document.getElementById('hoaSelect').dispatchEvent(new Event('change'))")
        page2.wait_for_timeout(2000)
        # Find upvote button
        upvote_btns = page2.locator(".upvote-btn")
        if upvote_btns.count() > 0:
            upvote_btns.first.click()
            page2.wait_for_timeout(1500)
            results.passed("Multi-user browser: upvote proposal")
        else:
            results.warn("Multi-user browser: upvote proposal", "No upvote buttons found in feed")
    except Exception as exc:
        results.failed("Multi-user browser: upvote proposal", str(exc)[:120])

    # ---- User 3 sees proposal in feed and co-signs by name ----
    try:
        page3.goto(f"{base}/proposals", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page3.wait_for_selector("#hoaSelect", timeout=PAGE_TIMEOUT)
        page3.wait_for_function(
            "document.querySelectorAll('#hoaSelect option').length >= 1 && "
            "document.querySelector('#hoaSelect option').value !== ''",
            timeout=INTERACTION_TIMEOUT,
        )
        page3.evaluate("document.getElementById('hoaSelect').dispatchEvent(new Event('change'))")
        page3.wait_for_timeout(2000)
        cosign_btns = page3.locator(".cosign-btn")
        if cosign_btns.count() > 0:
            cosign_btns.first.click()
            page3.wait_for_timeout(1500)
            # Check the button changed to signed state
            first_btn = page3.locator(".cosign-btn").first
            if first_btn.count() > 0:
                btn_text = first_btn.text_content()
                if "Signed" in (btn_text or ""):
                    results.passed("Multi-user browser: named co-sign on public proposal")
                else:
                    # Button may have already been applied (already cosigned as original cosigner)
                    results.warn("Multi-user browser: named co-sign", f"Button text: {btn_text}")
            else:
                results.passed("Multi-user browser: named co-sign (button toggled)")
        else:
            results.warn("Multi-user browser: named co-sign", "No co-sign buttons found")
    except Exception as exc:
        results.failed("Multi-user browser: named co-sign", str(exc)[:120])

    # ---- User 1 withdraws the proposal (cleanup) ----
    try:
        page1.goto(f"{base}/proposals", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page1.wait_for_timeout(2000)
        withdraw_btns = page1.locator("[data-withdraw]")
        if withdraw_btns.count() > 0:
            page1.on("dialog", lambda d: d.accept())
            withdraw_btns.first.click()
            page1.wait_for_timeout(2000)
            results.passed("Multi-user browser: withdraw proposal (cleanup)")
        else:
            results.warn("Multi-user browser: withdraw proposal", "No withdraw button found")
    except Exception as exc:
        results.warn("Multi-user browser: withdraw proposal", str(exc)[:120])

    # ---- Proxy delegation flow ----
    # User 2 becomes a delegate
    try:
        page2.goto(f"{base}/become-delegate", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page2.wait_for_timeout(1000)
        # Check if already a delegate (button may say "already registered" or form may be hidden)
        page_text = page2.text_content("body") or ""
        if "already" in page_text.lower() or "registered" in page_text.lower():
            results.passed("Multi-user browser: user 2 already a delegate")
        else:
            # Try to fill form and register
            hoa_select = page2.locator("#hoaSelect, #delegateHoaSelect, select").first
            if hoa_select.count() > 0:
                page2.wait_for_timeout(500)
                bio_field = page2.locator("#bio, textarea").first
                if bio_field.count() > 0:
                    bio_field.fill("Smoke test delegate")
                register_btn = page2.locator("button.primary, #registerBtn").first
                if register_btn.count() > 0:
                    register_btn.click()
                    page2.wait_for_timeout(2000)
                    results.passed("Multi-user browser: register as delegate")
                else:
                    results.warn("Multi-user browser: register as delegate", "No register button")
            else:
                results.warn("Multi-user browser: register as delegate", "No HOA select found")
    except Exception as exc:
        results.warn("Multi-user browser: register as delegate", str(exc)[:120])

    # User 1 assigns proxy to user 2
    try:
        page1.goto(f"{base}/assign-proxy", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page1.wait_for_timeout(1500)
        page_text = page1.text_content("body") or ""
        if "already have" in page_text.lower():
            results.warn("Multi-user browser: assign proxy", "User already has active proxy")
        else:
            delegate_cards = page1.locator("[data-delegate-id], .delegate-card, .card")
            if delegate_cards.count() > 0:
                # Look for an assign/select button
                assign_btn = page1.locator("[data-assign], button:has-text('Assign'), button:has-text('Select')").first
                if assign_btn.count() > 0:
                    assign_btn.click()
                    page1.wait_for_timeout(2000)
                    results.passed("Multi-user browser: assign proxy")
                else:
                    results.warn("Multi-user browser: assign proxy", "No assign button found")
            else:
                results.warn("Multi-user browser: assign proxy",
                             "No delegate cards — may need delegates in this HOA")
    except Exception as exc:
        results.warn("Multi-user browser: assign proxy", str(exc)[:120])

    # ---- Proxy sign flow in browser ----
    # User 1 may now have a draft proxy from assigning above; try to sign it
    try:
        page1.goto(f"{base}/my-proxies", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page1.wait_for_timeout(2000)
        sign_link = page1.locator("a:has-text('Sign'), a[href*='proxy-sign']")
        if sign_link.count() > 0:
            sign_link.first.click()
            page1.wait_for_url("**/proxy-sign/**", timeout=INTERACTION_TIMEOUT)
            page1.wait_for_timeout(2000)

            # Verify form renders
            doc_mount = page1.locator(".doc-mount, .doc-shell")
            if doc_mount.count() > 0:
                results.passed("Multi-user browser: proxy sign page loads with form")
            else:
                results.warn("Multi-user browser: proxy sign page", "No doc-mount found")

            # Check consent and sign
            consent = page1.locator("#consent, input[type='checkbox']")
            sign_btn = page1.locator("#signBtn, button:has-text('Sign')")
            if consent.count() > 0 and sign_btn.count() > 0:
                consent.first.check()
                page1.wait_for_timeout(300)
                sign_btn.first.click()
                page1.wait_for_timeout(3000)
                body_text = page1.text_content("body") or ""
                if "signed" in body_text.lower():
                    results.passed("Multi-user browser: proxy signed via UI")
                else:
                    results.warn("Multi-user browser: proxy sign",
                                 "Could not confirm signed status")
            else:
                results.warn("Multi-user browser: proxy sign",
                             "Consent checkbox or sign button not found")
        else:
            results.warn("Multi-user browser: proxy sign flow",
                         "No sign link found in my-proxies")
    except Exception as exc:
        results.warn("Multi-user browser: proxy sign flow", str(exc)[:120])

    # ---- Delegate dashboard verification (user 2) ----
    try:
        page2.goto(f"{base}/delegate-dashboard", wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT)
        page2.wait_for_timeout(2000)
        stats = page2.locator(".stat, .stat-row")
        cards = page2.locator(".card")
        if stats.count() > 0 or cards.count() > 0:
            results.passed("Multi-user browser: delegate dashboard has content")
        else:
            empty = page2.locator(".empty")
            if empty.count() > 0:
                results.passed("Multi-user browser: delegate dashboard shows empty state")
            else:
                results.warn("Multi-user browser: delegate dashboard", "No content found")
    except Exception as exc:
        results.warn("Multi-user browser: delegate dashboard", str(exc)[:120])

    # ---- My Proxies content verification (user 1) ----
    try:
        page1.goto(f"{base}/my-proxies", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page1.wait_for_timeout(2000)
        badges = page1.locator(".badge")
        if badges.count() > 0:
            badge_text = badges.first.text_content() or ""
            results.passed(f"Multi-user browser: my-proxies shows badge ({badge_text.strip()})")
        else:
            empty = page1.locator(".empty")
            if empty.count() > 0:
                results.passed("Multi-user browser: my-proxies shows empty state")
            else:
                results.warn("Multi-user browser: my-proxies", "No badges or empty state")
    except Exception as exc:
        results.warn("Multi-user browser: my-proxies content", str(exc)[:120])

    # ---- Account page round-trip (user 1) ----
    try:
        page1.goto(f"{base}/account", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page1.wait_for_selector("#firstName", timeout=PAGE_TIMEOUT)
        page1.wait_for_function(
            "document.getElementById('email').value.length > 0",
            timeout=INTERACTION_TIMEOUT,
        )
        original_last = page1.input_value("#lastName")
        test_last = f"SmokeTest{int(time.time())}"
        page1.fill("#lastName", test_last)
        page1.click("#saveProfileBtn")
        page1.wait_for_function(
            "document.querySelector('#profileStatus')?.classList.contains('ok')",
            timeout=INTERACTION_TIMEOUT,
        )
        # Reload and verify
        page1.goto(f"{base}/account", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page1.wait_for_selector("#lastName", timeout=PAGE_TIMEOUT)
        page1.wait_for_function(
            "document.getElementById('lastName').value.length > 0",
            timeout=INTERACTION_TIMEOUT,
        )
        saved = page1.input_value("#lastName")
        if saved == test_last:
            results.passed("Multi-user browser: account edit round-trip")
        else:
            results.failed("Multi-user browser: account edit round-trip",
                           f"Expected '{test_last}', got '{saved}'")
        # Restore
        page1.fill("#lastName", original_last or "")
        page1.click("#saveProfileBtn")
        page1.wait_for_timeout(1000)
    except Exception as exc:
        results.warn("Multi-user browser: account edit round-trip", str(exc)[:120])


def group10_edge_cases(base: str, results: SmokeResults):
    """Edge cases and error handling — API-only, no browser needed."""
    print("\n--- Group 10: Edge Cases & Error Handling ---")

    # ---- Auth errors ----
    # Login wrong password
    s, _ = _api_call(base, "POST", "/auth/login",
                      {"email": "smoketest@example.com", "password": "totallyWrong1!"})
    if s == 401:
        results.passed("Edge: login wrong password → 401")
    else:
        results.failed("Edge: login wrong password", f"HTTP {s} (want 401)")

    # Login nonexistent email
    s, _ = _api_call(base, "POST", "/auth/login",
                      {"email": "nonexistent-smoke@test.hoatest.invalid",
                       "password": "Whatever123!"})
    if s == 401:
        results.passed("Edge: login nonexistent email → 401")
    else:
        results.failed("Edge: login nonexistent email", f"HTTP {s} (want 401)")

    # Register duplicate email
    reg_email = f"smoke-edge-{int(time.time())}@test.hoatest.invalid"
    reg_pw = f"EdgeTest{int(time.time())}!"
    s, _ = _api_call(base, "POST", "/auth/register",
                      {"email": reg_email, "password": reg_pw,
                       "display_name": "Edge Test"})
    if s == 200:
        s2, _ = _api_call(base, "POST", "/auth/register",
                           {"email": reg_email, "password": reg_pw,
                            "display_name": "Edge Test Dup"})
        if s2 == 409:
            results.passed("Edge: duplicate registration → 409")
        else:
            results.failed("Edge: duplicate registration", f"HTTP {s2} (want 409)")
    else:
        results.failed("Edge: first registration for dup test", f"HTTP {s}")

    # Register weak password
    s, resp = _api_call(base, "POST", "/auth/register",
                         {"email": f"smoke-weak-{int(time.time())}@test.hoatest.invalid",
                          "password": "short",
                          "display_name": "Weak Test"})
    if s in (400, 422):
        results.passed(f"Edge: weak password → {s}")
    else:
        results.failed("Edge: weak password", f"HTTP {s} (want 400/422)")

    # Register missing email
    s, _ = _api_call(base, "POST", "/auth/register",
                      {"password": "ValidPass123!",
                       "display_name": "No Email Test"})
    if s in (400, 422):
        results.passed(f"Edge: missing email → {s}")
    else:
        results.failed("Edge: missing email", f"HTTP {s} (want 400/422)")

    # ---- Auth token errors ----
    # No token
    s, _ = _api_call(base, "GET", "/auth/me")
    if s == 401:
        results.passed("Edge: /auth/me without token → 401")
    else:
        results.failed("Edge: /auth/me without token", f"HTTP {s} (want 401)")

    # Garbage token
    s, _ = _api_call(base, "GET", "/auth/me", None, "garbage.token.here")
    if s == 401:
        results.passed("Edge: /auth/me garbage token → 401")
    else:
        results.failed("Edge: /auth/me garbage token", f"HTTP {s} (want 401)")

    # ---- Resource not found ----
    # Invalid proxy
    try:
        t_edge, _ = _register_temp(base, "edge", "Edge Tester")
    except Exception as exc:
        results.failed("Edge: setup", str(exc)[:80])
        return

    s, _ = _api_call(base, "GET", "/proxies/99999", None, t_edge)
    if s == 404:
        results.passed("Edge: invalid proxy → 404")
    else:
        results.failed("Edge: invalid proxy", f"HTTP {s} (want 404)")

    # Invalid delegate
    s, _ = _api_call(base, "GET", "/delegates/99999")
    if s == 404:
        results.passed("Edge: invalid delegate → 404")
    else:
        results.failed("Edge: invalid delegate", f"HTTP {s} (want 404)")

    # Invalid verification code
    s, _ = _api_call(base, "GET", "/proxies/verify/INVALIDCODE999")
    if s == 404:
        results.passed("Edge: invalid verification code → 404")
    else:
        results.failed("Edge: invalid verification code", f"HTTP {s} (want 404)")

    # Invalid reset token
    s, resp = _api_call(base, "POST", "/auth/reset-password",
                         {"token": "invalidtoken123", "password": "NewPass123!"})
    if s in (400, 404):
        results.passed(f"Edge: invalid reset token → {s}")
    else:
        results.failed("Edge: invalid reset token", f"HTTP {s} (want 400/404)")

    # ---- Permission errors ----
    # Claim membership in nonexistent HOA
    s, _ = _api_call(base, "POST", "/user/hoas/99999/claim",
                      {"unit_number": "SMOKE"}, t_edge)
    if s in (400, 404):
        results.passed(f"Edge: claim nonexistent HOA → {s}")
    else:
        results.failed("Edge: claim nonexistent HOA", f"HTTP {s} (want 400/404)")

    # Create proposal without membership
    s, _ = _api_call(base, "POST", "/proposals", {
        "hoa_id": 99999,
        "title": "Edge test proposal",
        "description": "Should fail",
        "category": "Other",
    }, t_edge)
    if s in (400, 403, 404):
        results.passed(f"Edge: proposal without membership → {s}")
    else:
        results.failed("Edge: proposal without membership", f"HTTP {s} (want 400/403/404)")

    # Sign someone else's proxy
    # Create a proxy between two new users, then try to sign it as a third
    try:
        t_a, uid_a = _register_temp(base, "sign-owner", "SignOwner Test")
        t_b, uid_b = _register_temp(base, "sign-delegate", "SignDelegate Test")
        t_c, _ = _register_temp(base, "sign-intruder", "SignIntruder Test")

        s, summary = _api_call(base, "GET", "/hoas/summary?page=1&per_page=1")
        _edge_hoas = _hoa_list_from_summary(summary) if s == 200 else []
        if _edge_hoas:
            edge_hoa_id = _edge_hoas[0]["id"]
            for tok in (t_a, t_b, t_c):
                _api_call(base, "POST", f"/user/hoas/{edge_hoa_id}/claim",
                          {"unit_number": "SMOKE"}, tok)
            _api_call(base, "POST", "/delegates/register", {
                "hoa_id": edge_hoa_id, "bio": "Edge delegate",
                "contact_email": "edge-del@test.hoatest.invalid",
            }, t_b)
            s, proxy = _api_call(base, "POST", "/proxies", {
                "hoa_id": edge_hoa_id, "delegate_user_id": uid_b,
            }, t_a)
            if s == 200:
                proxy_id = proxy["id"]
                # Third user tries to sign
                s_intruder, _ = _api_call(base, "POST",
                                           f"/proxies/{proxy_id}/sign", None, t_c)
                if s_intruder in (403, 404):
                    results.passed(f"Edge: sign others proxy → {s_intruder}")
                else:
                    results.failed("Edge: sign others proxy",
                                   f"HTTP {s_intruder} (want 403/404)")
                # Clean up
                _api_call(base, "POST", f"/proxies/{proxy_id}/revoke", {"reason": "smoke test cleanup"}, t_a)
            else:
                results.warn("Edge: sign others proxy", f"Could not create proxy ({s})")
    except Exception as exc:
        results.failed("Edge: sign others proxy", str(exc)[:120])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="HOAproxy E2E smoke tests")
    parser.add_argument("--url", default="https://hoaproxy.org",
                        help="Base URL to test (default: https://hoaproxy.org)")
    parser.add_argument("--email", default=None,
                        help="Test account email (or set SMOKE_TEST_EMAIL)")
    parser.add_argument("--password", default=None,
                        help="Test account password (or set SMOKE_TEST_PASSWORD)")
    parser.add_argument("--headed", action="store_true",
                        help="Run browser in headed mode for debugging")
    parser.add_argument("--slow-mo", type=int, default=0,
                        help="Slow down actions by N ms")
    parser.add_argument("--group", type=int, default=0,
                        help="Run only a specific group (1-10), 0 = all")
    return parser.parse_args()


def main():
    _load_settings_env()
    args = parse_args()
    base = args.url.rstrip("/")

    email = args.email or os.environ.get("SMOKE_TEST_EMAIL", "")
    password = args.password or os.environ.get("SMOKE_TEST_PASSWORD", "")
    has_creds = bool(email and password)

    multi_browser = all([
        has_creds,
        os.environ.get("SMOKE_TEST_EMAIL_2"),
        os.environ.get("SMOKE_TEST_PASSWORD_2"),
        os.environ.get("SMOKE_TEST_EMAIL_3"),
        os.environ.get("SMOKE_TEST_PASSWORD_3"),
    ])

    print(f"\nHOAproxy Smoke Test")
    print(f"  Target: {base}")
    print(f"  Auth:   {'yes' if has_creds else 'no (auth tests will be skipped)'}")
    print(f"  Multi:  {'browser + API' if multi_browser else 'API only (set SMOKE_TEST_EMAIL_2/3 for browser)'}")
    print(f"  Mode:   {'headed' if args.headed else 'headless'}")

    results = SmokeResults()
    run_group = args.group

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed,
            slow_mo=args.slow_mo,
        )

        # Groups 1-3 and 5: unauthenticated context
        if run_group in (0, 1, 2, 3, 5):
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()
            js_errors: list[str] = []
            page.on("pageerror", lambda exc: js_errors.append(str(exc)))
            page.on("console",
                     lambda msg: js_errors.append(msg.text)
                     if msg.type == "error" else None)

            if run_group in (0, 1):
                group1_static(page, base, results, js_errors)
            if run_group in (0, 2):
                group2_public(page, base, results, js_errors)
            if run_group in (0, 3):
                group3_auth_gates(page, base, results, js_errors)
            if run_group in (0, 5):
                group5_interactive(page, base, results, js_errors)

            context.close()

        # Group 4: authenticated context (separate to avoid token leaking to group 3)
        if run_group in (0, 4):
            if has_creds:
                context = browser.new_context(viewport={"width": 1280, "height": 720})
                page = context.new_page()
                js_errors = []
                page.on("pageerror", lambda exc: js_errors.append(str(exc)))
                page.on("console",
                         lambda msg: js_errors.append(msg.text)
                         if msg.type == "error" else None)
                group4_authenticated(page, base, results, js_errors, email, password)
                context.close()
            else:
                print("\n--- Group 4: Login + Authenticated Pages ---")
                for name in ["Login via UI", "Dashboard greeting visible",
                             "Dashboard Q&A widget present", "Dashboard checklist present",
                             "My Proxies (authed)", "Proposals (authed)",
                             "Become Delegate (authed)", "Delegate Dashboard (authed)",
                             "Assign Proxy (authed)", "Add Participation (authed)",
                             "Add HOA (authed)",
                             "Login redirects to dashboard when authed",
                             "Register redirects to dashboard when authed"]:
                    results.skipped(name, "No credentials provided")

        # Group 6: email delivery (no browser needed)
        if run_group in (0, 6):
            group6_email(base, results)

        # Group 7: API health (no browser needed)
        if run_group in (0, 7):
            group7_api(base, results)

        # Group 8: multi-user API tests (no browser needed)
        if run_group in (0, 8):
            group8_multi_user_api(base, results)

        # Group 9: multi-user browser tests (requires 3 accounts)
        if run_group in (0, 9):
            email2 = os.environ.get("SMOKE_TEST_EMAIL_2", "")
            pw2 = os.environ.get("SMOKE_TEST_PASSWORD_2", "")
            email3 = os.environ.get("SMOKE_TEST_EMAIL_3", "")
            pw3 = os.environ.get("SMOKE_TEST_PASSWORD_3", "")
            has_multi_creds = all([has_creds, email2, pw2, email3, pw3])

            if has_multi_creds:
                contexts = []
                page_list = []
                js_err_lists: list[list[str]] = []
                for _ in range(3):
                    ctx = browser.new_context(viewport={"width": 1280, "height": 720})
                    pg = ctx.new_page()
                    errs: list[str] = []
                    pg.on("pageerror", lambda exc, e=errs: e.append(str(exc)))
                    pg.on("console",
                          lambda msg, e=errs: e.append(msg.text)
                          if msg.type == "error" else None)
                    contexts.append(ctx)
                    page_list.append(pg)
                    js_err_lists.append(errs)

                group9_multi_user_browser(
                    page_list[0], page_list[1], page_list[2],
                    base, results, js_err_lists,
                    [(email, password), (email2, pw2), (email3, pw3)],
                )

                for ctx in contexts:
                    ctx.close()
            else:
                print("\n--- Group 9: Multi-User Browser Tests ---")
                missing = []
                if not has_creds:
                    missing.append("SMOKE_TEST_EMAIL/PASSWORD")
                if not email2 or not pw2:
                    missing.append("SMOKE_TEST_EMAIL_2/PASSWORD_2")
                if not email3 or not pw3:
                    missing.append("SMOKE_TEST_EMAIL_3/PASSWORD_3")
                reason = f"Missing: {', '.join(missing)}"
                for name in [
                    "Multi-user browser: login 3 users",
                    "Multi-user browser: create proposal",
                    "Multi-user browser: user 2 co-signs",
                    "Multi-user browser: user 3 co-signs",
                    "Multi-user browser: upvote proposal",
                    "Multi-user browser: named co-sign",
                    "Multi-user browser: withdraw proposal",
                    "Multi-user browser: register as delegate",
                    "Multi-user browser: assign proxy",
                    "Multi-user browser: proxy sign flow",
                    "Multi-user browser: delegate dashboard",
                    "Multi-user browser: my-proxies content",
                    "Multi-user browser: account edit round-trip",
                ]:
                    results.skipped(name, reason)

        # Group 10: edge cases & error handling (no browser needed)
        if run_group in (0, 10):
            group10_edge_cases(base, results)

        browser.close()

    results.summary()
    sys.exit(0 if results.all_passed() else 1)


if __name__ == "__main__":
    main()
