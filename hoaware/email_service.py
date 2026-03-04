"""Email delivery abstraction.

MVP: Log to console + store as "delivered."
Future: SMTP/SendGrid/Postmark integration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from hoaware import db
from hoaware.config import load_settings

logger = logging.getLogger(__name__)


def deliver_proxy_to_board(proxy_id: int, actor_user_id: int | None = None) -> bool:
    """MVP: Log the delivery event. Do not actually send email.

    The delegate prints/forwards the form manually.
    """
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
        if not proxy:
            return False
        if proxy["status"] not in ("signed",):
            return False

        now = datetime.now(timezone.utc).isoformat()
        db.update_proxy_status(conn, proxy_id, "delivered", delivered_at=now)
        db.create_proxy_audit(
            conn,
            proxy_id=proxy_id,
            action="delivered",
            actor_user_id=actor_user_id,
            details={
                "method": "stub_log",
                "timestamp": now,
                "note": "Email delivery stub. Delegate should deliver form manually.",
            },
        )
        logger.info(
            "PROXY DELIVERY: proxy_id=%d, grantor=%s, delegate=%s, hoa=%s",
            proxy_id,
            proxy.get("grantor_email"),
            proxy.get("delegate_email"),
            proxy.get("hoa_name"),
        )
    return True
