"""Email delivery abstraction.

Supports three modes set by EMAIL_PROVIDER env var:
- "stub" (default): log only, no real email sent
- "resend": use the Resend API (https://resend.com) — 100 emails/day free
- "smtp": use SMTP (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD)
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from hoaware import db
from hoaware.config import load_settings
from hoaware.cost_tracker import log_email_usage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal send helpers
# ---------------------------------------------------------------------------

def _send_via_resend(
    *,
    api_key: str,
    from_addr: str,
    to: list[str],
    subject: str,
    html: str,
) -> None:
    import resend  # type: ignore
    resend.api_key = api_key
    resend.Emails.send({
        "from": from_addr,
        "to": to,
        "subject": subject,
        "html": html,
    })


def _send_via_smtp(
    *,
    host: str,
    port: int,
    user: str | None,
    password: str | None,
    from_addr: str,
    to: list[str],
    subject: str,
    html: str,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port) as smtp:
        smtp.ehlo()
        if port != 25:
            smtp.starttls()
            smtp.ehlo()
        if user and password:
            smtp.login(user, password)
        smtp.sendmail(from_addr, to, msg.as_string())


def _send_email(*, to: list[str], subject: str, html: str) -> bool:
    """Send an email using the configured provider. Returns True on success."""
    settings = load_settings()
    provider = settings.email_provider

    if provider == "resend":
        if not settings.resend_api_key:
            logger.warning("EMAIL_PROVIDER=resend but RESEND_API_KEY not set; falling back to stub")
            provider = "stub"
        else:
            try:
                _send_via_resend(
                    api_key=settings.resend_api_key,
                    from_addr=settings.email_from,
                    to=to,
                    subject=subject,
                    html=html,
                )
                log_email_usage("resend", recipient_count=len(to))
                logger.info("Email sent via Resend to %s: %s", to, subject)
                return True
            except Exception as exc:
                logger.error("Resend delivery failed: %s", exc)
                return False

    if provider == "smtp":
        if not settings.smtp_host:
            logger.warning("EMAIL_PROVIDER=smtp but SMTP_HOST not set; falling back to stub")
            provider = "stub"
        else:
            try:
                _send_via_smtp(
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    user=settings.smtp_user,
                    password=settings.smtp_password,
                    from_addr=settings.email_from,
                    to=to,
                    subject=subject,
                    html=html,
                )
                log_email_usage("smtp", recipient_count=len(to))
                logger.info("Email sent via SMTP to %s: %s", to, subject)
                return True
            except Exception as exc:
                logger.error("SMTP delivery failed: %s", exc)
                return False

    # Stub: log and consider "sent"
    logger.info("EMAIL STUB to=%s subject=%r (set EMAIL_PROVIDER to send real email)", to, subject)
    return True


# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------

def _proxy_delivery_html(proxy: dict) -> str:
    grantor = proxy.get("grantor_name") or proxy.get("grantor_email", "A member")
    delegate = proxy.get("delegate_name") or proxy.get("delegate_email", "you")
    hoa = proxy.get("hoa_name", "your HOA")
    return f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:0 auto">
    <h2>Proxy Authorization — {hoa}</h2>
    <p><strong>{grantor}</strong> has signed a proxy authorization
    designating <strong>{delegate}</strong> as their proxy holder
    at <strong>{hoa}</strong>.</p>
    <p>Please present this proxy to the board secretary or other authorized recipient.
    The grantor may revoke this proxy at any time before it is exercised
    by logging into HOAproxy.</p>
    <hr>
    <p style="font-size:12px;color:#666">
    HOAproxy — This is not legal advice. Proxy validity is governed by your state's
    HOA statutes and your community's governing documents.
    </p>
    </body></html>
    """


def _proxy_status_html(proxy: dict, event: str) -> str:
    hoa = proxy.get("hoa_name", "your HOA")
    messages = {
        "signed": f"Your proxy for <strong>{hoa}</strong> has been signed successfully.",
        "delivered": f"Your proxy for <strong>{hoa}</strong> has been delivered to the board.",
        "revoked": f"Your proxy for <strong>{hoa}</strong> has been revoked.",
        "expired": f"Your proxy for <strong>{hoa}</strong> has expired.",
    }
    body = messages.get(event, f"Your proxy status has changed to: {event}")
    return f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:0 auto">
    <h2>Proxy Update — {hoa}</h2>
    <p>{body}</p>
    <p><a href="https://hoaproxy.org/my-proxies">View your proxies</a></p>
    <hr>
    <p style="font-size:12px;color:#666">HOAproxy — Not legal advice.</p>
    </body></html>
    """


def _delegate_notification_html(proxy: dict, event: str) -> str:
    hoa = proxy.get("hoa_name", "your HOA")
    grantor = proxy.get("grantor_name") or proxy.get("grantor_email", "A member")
    if event == "new_proxy":
        subject_line = f"New proxy from {grantor}"
        body = f"<strong>{grantor}</strong> has assigned you as their proxy holder for <strong>{hoa}</strong>."
    else:
        subject_line = f"Proxy revoked by {grantor}"
        body = f"<strong>{grantor}</strong> has revoked their proxy at <strong>{hoa}</strong>."
    return f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:0 auto">
    <h2>{subject_line}</h2>
    <p>{body}</p>
    <p><a href="https://hoaproxy.org/delegate-dashboard">View your delegate dashboard</a></p>
    <hr>
    <p style="font-size:12px;color:#666">HOAproxy — Not legal advice.</p>
    </body></html>
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deliver_proxy_to_board(proxy_id: int, actor_user_id: int | None = None) -> bool:
    """Send the signed proxy form to the board and mark it as delivered.

    If the HOA has a board_email set and EMAIL_PROVIDER != "stub", sends a real email.
    Always records an audit entry and updates status to 'delivered'.
    """
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
        if not proxy:
            return False
        if proxy["status"] not in ("signed",):
            return False

        board_email = proxy.get("hoa_board_email")
        email_sent = False

        if board_email:
            html = _proxy_delivery_html(proxy)
            grantor_name = proxy.get("grantor_name") or proxy.get("grantor_email", "member")
            hoa_name = proxy.get("hoa_name", "HOA")
            email_sent = _send_email(
                to=[board_email],
                subject=f"Proxy Authorization: {grantor_name} → {hoa_name}",
                html=html,
            )
        else:
            logger.info(
                "PROXY DELIVERY: proxy_id=%d — no board_email set for HOA %s; "
                "delegate must deliver manually",
                proxy_id,
                proxy.get("hoa_name"),
            )

        now = datetime.now(timezone.utc).isoformat()
        db.update_proxy_status(conn, proxy_id, "delivered", delivered_at=now)
        db.create_proxy_audit(
            conn,
            proxy_id=proxy_id,
            action="delivered",
            actor_user_id=actor_user_id,
            details={
                "method": "email" if email_sent else "manual",
                "board_email": board_email,
                "timestamp": now,
            },
        )
    return True


def notify_grantor(proxy_id: int, event: str) -> bool:
    """Send a status notification to the grantor (signed, delivered, revoked, expired)."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
    if not proxy:
        return False
    grantor_email = proxy.get("grantor_email")
    if not grantor_email:
        return False
    hoa_name = proxy.get("hoa_name", "your HOA")
    subjects = {
        "signed": f"Your proxy for {hoa_name} has been signed",
        "delivered": f"Your proxy for {hoa_name} has been delivered",
        "revoked": f"Your proxy for {hoa_name} has been revoked",
        "expired": f"Your proxy for {hoa_name} has expired",
    }
    subject = subjects.get(event, f"Proxy update — {hoa_name}")
    return _send_email(to=[grantor_email], subject=subject, html=_proxy_status_html(proxy, event))


def send_verification_email(*, email: str, token: str, base_url: str) -> bool:
    """Send an email verification link to a newly registered user."""
    verify_url = f"{base_url.rstrip('/')}/verify-email?token={token}"
    html = f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:0 auto">
    <h2>Verify your HOAproxy email address</h2>
    <p>Click the link below to verify your email address and activate your account:</p>
    <p><a href="{verify_url}" style="display:inline-block;padding:12px 24px;background:#1662f3;color:#fff;text-decoration:none;border-radius:8px;font-weight:bold">Verify Email Address</a></p>
    <p>Or copy and paste this link: {verify_url}</p>
    <p>This link expires in 24 hours.</p>
    <hr>
    <p style="font-size:12px;color:#666">If you didn't create a HOAproxy account, you can ignore this email.</p>
    </body></html>
    """
    return _send_email(
        to=[email],
        subject="Verify your HOAproxy email address",
        html=html,
    )


def send_password_reset_email(*, email: str, token: str, base_url: str) -> bool:
    """Send a password reset link to a user who requested it."""
    reset_url = f"{base_url.rstrip('/')}/reset-password?token={token}"
    html = f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:0 auto">
    <h2>Reset your HOAproxy password</h2>
    <p>We received a request to reset the password for your account.</p>
    <p><a href="{reset_url}" style="display:inline-block;padding:12px 24px;background:#1662f3;color:#fff;text-decoration:none;border-radius:8px;font-weight:bold">Reset Password</a></p>
    <p>Or copy and paste this link: {reset_url}</p>
    <p>This link expires in 1 hour. If you didn't request a password reset, you can ignore this email.</p>
    <hr>
    <p style="font-size:12px;color:#666">HOAproxy — your account security is important to us.</p>
    </body></html>
    """
    return _send_email(
        to=[email],
        subject="Reset your HOAproxy password",
        html=html,
    )


def notify_delegate(proxy_id: int, event: str) -> bool:
    """Send a notification to the delegate (new_proxy, revoked)."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
    if not proxy:
        return False
    delegate_email = proxy.get("delegate_email")
    if not delegate_email:
        return False
    hoa_name = proxy.get("hoa_name", "your HOA")
    grantor = proxy.get("grantor_name") or proxy.get("grantor_email", "A member")
    subjects = {
        "new_proxy": f"New proxy from {grantor} — {hoa_name}",
        "revoked": f"Proxy revoked by {grantor} — {hoa_name}",
    }
    subject = subjects.get(event, f"Proxy update — {hoa_name}")
    return _send_email(
        to=[delegate_email],
        subject=subject,
        html=_delegate_notification_html(proxy, event),
    )


# ---------------------------------------------------------------------------
# Weekly cost report
# ---------------------------------------------------------------------------

def _fetch_ga4_traffic(property_id: str) -> dict | None:
    """Pull 7-day traffic summary from GA4 Data API. Returns None on failure."""
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Metric, Dimension,
        )
    except ImportError:
        logger.debug("google-analytics-data not installed, skipping GA4")
        return None

    try:
        client = BetaAnalyticsDataClient()
        request = RunReportRequest(
            property=f"properties/{property_id}",
            date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
            metrics=[
                Metric(name="activeUsers"),
                Metric(name="sessions"),
                Metric(name="screenPageViews"),
                Metric(name="averageSessionDuration"),
            ],
        )
        response = client.run_report(request)
        if not response.rows:
            return None
        row = response.rows[0]
        return {
            "active_users": int(row.metric_values[0].value),
            "sessions": int(row.metric_values[1].value),
            "pageviews": int(row.metric_values[2].value),
            "avg_session_sec": float(row.metric_values[3].value),
        }
    except Exception:
        logger.debug("GA4 report fetch failed", exc_info=True)
        return None


def _fetch_site_stats(conn) -> dict:
    """Pull key site metrics from the DB."""
    def _count(sql):
        return conn.execute(sql).fetchone()[0]

    return {
        "hoas": _count("SELECT COUNT(*) FROM hoas"),
        "documents": _count("SELECT COUNT(*) FROM documents"),
        "chunks": _count("SELECT COUNT(*) FROM chunks"),
        "users": _count("SELECT COUNT(*) FROM users"),
        "proxies": _count("SELECT COUNT(*) FROM proxy_assignments"),
        "proposals_active": _count("SELECT COUNT(*) FROM proposals WHERE status != 'archived'"),
        "new_users_7d": _count("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-7 days')"),
        "new_proxies_7d": _count("SELECT COUNT(*) FROM proxy_assignments WHERE created_at >= datetime('now', '-7 days')"),
        "new_proposals_7d": _count("SELECT COUNT(*) FROM proposals WHERE created_at >= datetime('now', '-7 days')"),
    }


def send_cost_report(*, to_email: str, month: str | None = None) -> bool:
    """Build and send a cost summary email for the given month (default: current)."""
    settings = load_settings()
    if not month:
        now = datetime.now(timezone.utc)
        month = f"{now.year:04d}-{now.month:02d}"

    with db.get_connection(settings.db_path) as conn:
        metered = db.get_usage_summary(conn, month=month)
        fixed = db.list_fixed_costs(conn, active_only=True)
        site = _fetch_site_stats(conn)

    # Build metered rows
    total_metered = 0.0
    metered_rows = ""
    for row in metered:
        cost = row["total_est_cost_usd"] or 0
        total_metered += cost
        metered_rows += (
            f"<tr><td style='padding:6px 12px'>{row['service']}</td>"
            f"<td style='padding:6px 12px;text-align:right'>{row['total_units']:,.0f} {row['unit_type']}</td>"
            f"<td style='padding:6px 12px;text-align:right'>${cost:,.4f}</td></tr>"
        )
    if not metered_rows:
        metered_rows = "<tr><td colspan='3' style='padding:6px 12px;color:#888'>No metered usage this period</td></tr>"

    # Build fixed rows
    total_fixed = 0.0
    fixed_rows = ""
    for fc in fixed:
        total_fixed += fc["monthly_equiv"]
        fixed_rows += (
            f"<tr><td style='padding:6px 12px'>{fc['service']}</td>"
            f"<td style='padding:6px 12px'>{fc['description'] or ''}</td>"
            f"<td style='padding:6px 12px;text-align:right'>${fc['monthly_equiv']:,.2f}</td></tr>"
        )

    total = total_metered + total_fixed

    # --- Site stats section ---
    site_html = f"""
    <h3>Site Stats</h3>
    <table style="border-collapse:collapse;width:100%">
    <tr style="background:#eef5ff"><th style="padding:6px 12px;text-align:left">Metric</th>
    <th style="padding:6px 12px;text-align:right">Total</th>
    <th style="padding:6px 12px;text-align:right">Last 7 days</th></tr>
    <tr><td style="padding:6px 12px">Users</td>
        <td style="padding:6px 12px;text-align:right">{site['users']:,}</td>
        <td style="padding:6px 12px;text-align:right">+{site['new_users_7d']}</td></tr>
    <tr><td style="padding:6px 12px">Proxy assignments</td>
        <td style="padding:6px 12px;text-align:right">{site['proxies']:,}</td>
        <td style="padding:6px 12px;text-align:right">+{site['new_proxies_7d']}</td></tr>
    <tr><td style="padding:6px 12px">Active proposals</td>
        <td style="padding:6px 12px;text-align:right">{site['proposals_active']:,}</td>
        <td style="padding:6px 12px;text-align:right">+{site['new_proposals_7d']}</td></tr>
    <tr><td style="padding:6px 12px">HOAs indexed</td>
        <td style="padding:6px 12px;text-align:right">{site['hoas']:,}</td>
        <td style="padding:6px 12px;text-align:right">—</td></tr>
    <tr><td style="padding:6px 12px">Documents / chunks</td>
        <td style="padding:6px 12px;text-align:right">{site['documents']:,} / {site['chunks']:,}</td>
        <td style="padding:6px 12px;text-align:right">—</td></tr>
    </table>
    """

    # --- GA4 traffic section (optional) ---
    ga4_html = ""
    ga4 = _fetch_ga4_traffic(settings.ga4_property_id) if settings.ga4_property_id else None
    if ga4:
        avg_min = ga4["avg_session_sec"] / 60
        ga4_html = f"""
        <h3>Traffic (last 7 days)</h3>
        <table style="border-collapse:collapse;width:100%">
        <tr style="background:#eef5ff"><th style="padding:6px 12px;text-align:left">Metric</th>
        <th style="padding:6px 12px;text-align:right">Value</th></tr>
        <tr><td style="padding:6px 12px">Active users</td>
            <td style="padding:6px 12px;text-align:right">{ga4['active_users']:,}</td></tr>
        <tr><td style="padding:6px 12px">Sessions</td>
            <td style="padding:6px 12px;text-align:right">{ga4['sessions']:,}</td></tr>
        <tr><td style="padding:6px 12px">Pageviews</td>
            <td style="padding:6px 12px;text-align:right">{ga4['pageviews']:,}</td></tr>
        <tr><td style="padding:6px 12px">Avg session duration</td>
            <td style="padding:6px 12px;text-align:right">{avg_min:.1f} min</td></tr>
        </table>
        """

    html = f"""
    <html><body style="font-family:sans-serif;max-width:650px;margin:0 auto;color:#12233a">
    <h2 style="color:#1662f3">HOAproxy Weekly Report — {month}</h2>

    {site_html}
    {ga4_html}

    <h3>Fixed Costs</h3>
    <table style="border-collapse:collapse;width:100%">
    <tr style="background:#eef5ff"><th style="padding:6px 12px;text-align:left">Service</th>
    <th style="padding:6px 12px;text-align:left">Description</th>
    <th style="padding:6px 12px;text-align:right">$/mo</th></tr>
    {fixed_rows}
    <tr style="font-weight:bold;border-top:2px solid #1662f3">
    <td colspan="2" style="padding:6px 12px">Subtotal</td>
    <td style="padding:6px 12px;text-align:right">${total_fixed:,.2f}</td></tr>
    </table>

    <h3>Metered Usage (MTD)</h3>
    <table style="border-collapse:collapse;width:100%">
    <tr style="background:#eef5ff"><th style="padding:6px 12px;text-align:left">Service</th>
    <th style="padding:6px 12px;text-align:right">Units</th>
    <th style="padding:6px 12px;text-align:right">Est. Cost</th></tr>
    {metered_rows}
    <tr style="font-weight:bold;border-top:2px solid #1662f3">
    <td colspan="2" style="padding:6px 12px">Subtotal</td>
    <td style="padding:6px 12px;text-align:right">${total_metered:,.4f}</td></tr>
    </table>

    <h2 style="margin-top:20px;color:#1662f3">Total: ${total:,.2f}/mo</h2>

    <hr>
    <p style="font-size:12px;color:#666">
    Automated weekly report from HOAproxy cost tracker.
    <a href="{settings.app_base_url}">hoaproxy.org</a>
    </p>
    </body></html>
    """

    return _send_email(
        to=[to_email],
        subject=f"HOAproxy costs — {month} — ${total:,.2f}/mo",
        html=html,
    )
