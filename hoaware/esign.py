"""E-signature abstraction layer.

MVP: Local click-to-sign. Records timestamp, user_id, IP as signature evidence.
Future: Documenso integration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from hoaware import db
from hoaware.config import load_settings

logger = logging.getLogger(__name__)


def record_signature(
    proxy_id: int,
    user_id: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> bool:
    """Record an e-signature for a proxy assignment.

    MVP implementation: records timestamp + IP as signature evidence,
    updates proxy status to 'signed', and logs an audit entry.
    """
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
        if not proxy:
            return False
        if proxy["grantor_user_id"] != user_id:
            return False
        if proxy["status"] != "draft":
            return False

        now = datetime.now(timezone.utc).isoformat()
        db.update_proxy_status(conn, proxy_id, "signed", signed_at=now)
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
                "consent": (
                    "By clicking 'Sign,' I affirm my identity and intend this "
                    "to constitute my electronic signature under the ESIGN Act "
                    "(15 U.S.C. § 7001) and my state's UETA."
                ),
            },
        )
        logger.info("Proxy %d signed by user %d from IP %s", proxy_id, user_id, ip_address)
    return True
