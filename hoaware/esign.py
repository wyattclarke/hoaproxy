"""E-signature layer (click-to-sign).

Records timestamp, IP address, and user-agent as signature evidence and
immediately marks the proxy as 'signed'. Compliant with the ESIGN Act
(15 U.S.C. § 7001) and state UETA equivalents.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from hoaware import db
from hoaware.config import load_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML → PDF conversion (used for Documenso uploads)
# ---------------------------------------------------------------------------

def _html_to_pdf(html: str) -> bytes:
    """Convert proxy form HTML to a simple PDF using fpdf2.

    Strips HTML tags and renders the plain text into a PDF. This gives Documenso
    a readable, signable document. Future: use weasyprint for styled rendering.
    """
    from fpdf import FPDF  # type: ignore

    soup = BeautifulSoup(html or "", "html.parser")
    # Extract text block by block, preserving paragraph structure
    text = soup.get_text(separator="\n")
    # fpdf2 built-in fonts only support Latin-1; sanitize to avoid rendering errors
    text = text.encode("latin-1", errors="replace").decode("latin-1")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_left_margin(15)
    pdf.set_right_margin(15)
    cell_w = pdf.epw  # effective page width
    pdf.set_font("Helvetica", size=10)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            pdf.ln(3)
            continue
        # Headings heuristic: short ALL-CAPS lines
        if line.isupper() and len(line) < 80:
            pdf.set_font("Helvetica", style="B", size=11)
            pdf.multi_cell(cell_w, 6, text=line)
            pdf.set_font("Helvetica", size=10)
        else:
            pdf.multi_cell(cell_w, 5, text=line)
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Click-to-sign
# ---------------------------------------------------------------------------

def _compute_form_hash(form_html: str) -> str:
    """SHA-256 hash of the original form HTML (before stamp injection)."""
    return hashlib.sha256((form_html or "").encode()).hexdigest()


def _compute_verification_code(proxy_id: int, signed_at: str, form_hash: str) -> str:
    """Deterministic 20-char hex verification code stored on the proxy record."""
    raw = f"{proxy_id}:{signed_at}:{form_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _inject_esig_stamp(
    form_html: str,
    grantor_name: str,
    ip_address: str | None,
    signed_at: str,
    verification_code: str,
    base_url: str = "",
) -> str:
    """Inject an electronic signature stamp into the proxy form HTML signature block."""
    ip_line = f"IP Address: {ip_address}<br>" if ip_address else ""
    verify_url = f"{base_url.rstrip('/')}/verify-proxy?code={verification_code}"
    stamp_html = (
        f'<div class="esig-stamp">'
        f"<strong>&#10003; Electronically Signed via HOAproxy</strong><br>"
        f"Signed by: {grantor_name}<br>"
        f"Date/Time: {signed_at}<br>"
        f"{ip_line}"
        f"Method: Click-to-sign &#8212; ESIGN Act (15 U.S.C. &sect; 7001) compliant<br>"
        f"Verification code: <strong>{verification_code}</strong><br>"
        f'Verify at: <a href="{verify_url}" target="_blank" rel="noopener noreferrer">{verify_url}</a>'
        f"</div>"
    )
    soup = BeautifulSoup(form_html or "", "html.parser")
    sig_block = soup.find(class_="signature-block")
    if sig_block:
        sig_block.append(BeautifulSoup(stamp_html, "html.parser"))
        return str(soup)
    # Fallback: append before </body>
    return form_html.replace("</body>", stamp_html + "</body>") if form_html else form_html


def record_signature(
    proxy_id: int,
    user_id: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
    base_url: str | None = None,
) -> bool:
    """Record an e-signature for a proxy assignment (click-to-sign fallback).

    Updates proxy status to 'signed' immediately. Use this when Documenso
    is not configured or as a fallback.
    """
    settings = load_settings()
    resolved_base_url = (base_url or settings.app_base_url).rstrip("/")
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
        if not proxy:
            return False
        if proxy["grantor_user_id"] != user_id:
            return False
        if proxy["status"] != "draft":
            return False

        now = datetime.now(timezone.utc).isoformat()
        original_html = proxy.get("form_html") or ""
        form_hash = _compute_form_hash(original_html)
        verification_code = _compute_verification_code(proxy_id, now, form_hash)
        signed_form_html = _inject_esig_stamp(
            original_html,
            grantor_name=proxy.get("grantor_name") or proxy.get("grantor_email") or "Grantor",
            ip_address=ip_address,
            signed_at=now,
            verification_code=verification_code,
            base_url=resolved_base_url,
        )
        db.update_proxy_status(
            conn, proxy_id, "signed",
            signed_at=now,
            form_html=signed_form_html,
            verification_code=verification_code,
            form_hash=form_hash,
        )
        db.create_proxy_audit(
            conn,
            proxy_id=proxy_id,
            action="signed",
            actor_user_id=user_id,
            details={
                "method": "click_to_sign",
                "ip_address": ip_address,
                "user_agent": user_agent,
                "timestamp": now,
                "form_hash": form_hash,
                "verification_code": verification_code,
                "consent": (
                    "By clicking 'Sign,' I affirm my identity and intend this "
                    "to constitute my electronic signature under the ESIGN Act "
                    "(15 U.S.C. § 7001) and my state's UETA."
                ),
            },
        )
        logger.info("Proxy %d signed by user %d from IP %s", proxy_id, user_id, ip_address)
    return True
