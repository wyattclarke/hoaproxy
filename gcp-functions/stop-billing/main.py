"""Cloud Function to disable billing when budget is exceeded.

Triggered by Pub/Sub messages from a GCP Budget alert.
Disables billing on the project, effectively stopping all paid services.

To re-enable: gcloud billing projects link hoaware --billing-account=01FBA6-3384FF-C9BD1A
"""

import base64
import json
import os

from google.api_core.exceptions import PermissionDenied
from google.cloud import billing_v1


PROJECT_ID = os.environ.get("GCP_PROJECT", "hoaware")
PROJECT_NAME = f"projects/{PROJECT_ID}"


def stop_billing(event, context):
    """Triggered from a Pub/Sub message when budget threshold is exceeded."""
    pubsub_data = base64.b64decode(event["data"]).decode("utf-8")
    notification = json.loads(pubsub_data)

    cost_amount = notification.get("costAmount", 0)
    budget_amount = notification.get("budgetAmount", 0)

    if cost_amount <= budget_amount:
        print(f"No action needed: cost {cost_amount} <= budget {budget_amount}")
        return

    print(f"Budget exceeded: cost {cost_amount} > budget {budget_amount}")
    print(f"Disabling billing for {PROJECT_NAME}...")

    client = billing_v1.CloudBillingClient()
    project_billing_info = billing_v1.ProjectBillingInfo(
        name=PROJECT_NAME,
        billing_account_name="",  # empty string disables billing
    )
    try:
        client.update_project_billing_info(
            name=PROJECT_NAME,
            project_billing_info=project_billing_info,
        )
        print(f"Billing disabled for {PROJECT_NAME}")
    except PermissionDenied:
        # Billing already disabled — ACK the message so Pub/Sub stops redelivering.
        print(f"Billing already disabled for {PROJECT_NAME}; ACKing message.")
