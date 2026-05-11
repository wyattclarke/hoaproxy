"""Cloud Function — GCS egress runaway protection.

Triggered by Pub/Sub messages from a GCP Budget alert scoped to the
`Cloud Storage` service (or specifically the buckets that serve public
PDFs). Pages the operator at the soft threshold, hard-unlinks billing at
the hard threshold.

This is a sibling to `scripts/gcp_billing_cap/main.py` (which kills
billing on TOTAL project spend at $600/mo to bound DocAI runaway). The
egress cap is a separate budget so a scrape attack on the public PDFs
doesn't have to wait for total spend to hit $600 — it can hard-stop on
just the GCS bill.

Deploy:
    gcloud functions deploy stop-gcs-egress \\
        --runtime python311 \\
        --trigger-topic gcs-egress-budget-alerts \\
        --entry-point stop_gcs_egress \\
        --region us-central1 \\
        --source scripts/gcp_egress_cap

Wiring (operator, in GCP Console):
    1. Create a Pub/Sub topic `gcs-egress-budget-alerts`.
    2. Cloud Billing → Budgets → "Create budget"
       - Scope: Service = Cloud Storage
       - Amount: $50 (operator paging) + $200 (hard cap)
       - Notification channel: the Pub/Sub topic above.
       - Add two budget rules: one alert at $50 (logs + paging),
         one at $200 (Cloud Function disables billing).
    3. Deploy this function.

When the function unlinks billing, the entire `hoaware` project loses
access to billing-gated services: GCS, DocAI, etc. To re-enable:
    gcloud billing projects link hoaware --billing-account=01FBA6-3384FF-C9BD1A

See `docs/scrape-protection.md` for the full runbook.
"""

import base64
import json
import os

from google.cloud import billing_v1


PROJECT_ID = os.environ.get("GCP_PROJECT", "hoaware")
PROJECT_NAME = f"projects/{PROJECT_ID}"
# Soft alert — log + page (real paging is wired through a separate
# Notification Channel on the budget, but we also log loudly here so
# operators can grep).
SOFT_THRESHOLD_USD = float(os.environ.get("EGRESS_SOFT_THRESHOLD_USD", "50"))
# Hard alert — kill billing on the project.
HARD_THRESHOLD_USD = float(os.environ.get("EGRESS_HARD_THRESHOLD_USD", "200"))


def stop_gcs_egress(event, context):
    """Triggered by Pub/Sub when the egress budget threshold is crossed."""
    pubsub_data = base64.b64decode(event["data"]).decode("utf-8")
    notification = json.loads(pubsub_data)

    cost_amount = float(notification.get("costAmount", 0) or 0)
    budget_amount = float(notification.get("budgetAmount", 0) or 0)
    alert_threshold = float(notification.get("alertThresholdExceeded", 0) or 0)

    print(
        f"egress alert: cost={cost_amount} budget={budget_amount} "
        f"threshold_pct={alert_threshold} soft={SOFT_THRESHOLD_USD} hard={HARD_THRESHOLD_USD}"
    )

    if cost_amount >= HARD_THRESHOLD_USD:
        print(
            f"egress HARD cap exceeded: cost {cost_amount} >= ${HARD_THRESHOLD_USD}. "
            f"Disabling billing for {PROJECT_NAME}."
        )
        client = billing_v1.CloudBillingClient()
        client.update_project_billing_info(
            name=PROJECT_NAME,
            project_billing_info=billing_v1.ProjectBillingInfo(
                name=PROJECT_NAME,
                billing_account_name="",  # empty string unlinks
            ),
        )
        print(f"egress: billing disabled on {PROJECT_NAME}")
        return

    if cost_amount >= SOFT_THRESHOLD_USD:
        # Log a structured event the operator can grep for in Cloud Logging.
        # Real paging happens via a separate Budget notification channel;
        # this is the redundant safety net.
        print(
            f"EGRESS_SOFT_ALERT: cost={cost_amount} threshold_pct={alert_threshold} "
            f"PROJECT={PROJECT_ID}"
        )
        return

    print(f"egress: no action (cost {cost_amount} < soft cap {SOFT_THRESHOLD_USD})")
