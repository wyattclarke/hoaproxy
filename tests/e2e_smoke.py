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
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

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
                        help="Run only a specific group (1-7), 0 = all")
    return parser.parse_args()


def main():
    _load_settings_env()
    args = parse_args()
    base = args.url.rstrip("/")

    email = args.email or os.environ.get("SMOKE_TEST_EMAIL", "")
    password = args.password or os.environ.get("SMOKE_TEST_PASSWORD", "")
    has_creds = bool(email and password)

    print(f"\nHOAproxy Smoke Test")
    print(f"  Target: {base}")
    print(f"  Auth:   {'yes' if has_creds else 'no (auth tests will be skipped)'}")
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

        browser.close()

    results.summary()
    sys.exit(0 if results.all_passed() else 1)


if __name__ == "__main__":
    main()
