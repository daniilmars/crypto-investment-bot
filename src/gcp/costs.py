# src/gcp/costs.py

from src.logger import log
from src.config import app_config


def get_gcp_billing_summary():
    """
    Fetches the GCP billing budget summary using the Cloud Billing Budgets API.

    Returns:
        str: A formatted string with the billing summary or an error message.
    """
    billing_config = app_config.get('gcp_billing', {})
    if not billing_config.get('enabled'):
        return "GCP billing summary is disabled in the configuration."

    billing_account_id = billing_config.get('billing_account_id')
    if not billing_account_id:
        log.error("GCP billing_account_id is not configured.")
        return "Error: GCP billing account ID is not configured."

    try:
        from google.cloud.billing.budgets_v1 import BudgetServiceClient

        client = BudgetServiceClient()
        parent = f"billingAccounts/{billing_account_id}"

        log.info(f"Fetching budgets for {parent}")
        budgets = list(client.list_budgets(parent=parent))

        if not budgets:
            return "No active budgets found for the configured billing account."

        budget = budgets[0]
        display_name = budget.display_name or "N/A"

        # Budget amount
        specified = budget.amount.specified_amount
        budget_amount = float(specified.units) + float(specified.nanos or 0) / 1e9

        if budget_amount == 0:
            return (
                f"*GCP Budget: '{display_name}'*\n\n"
                f"Budget amount is $0.00 — no spend data available."
            )

        # Current spend from threshold rules (the API doesn't expose spend directly;
        # we report the budget configuration instead)
        rules = budget.threshold_rules
        threshold_info = ", ".join(
            f"{int(r.threshold_percent * 100)}%" for r in rules
        ) if rules else "none"

        summary = (
            f"*GCP Budget: '{display_name}'*\n\n"
            f"*Budget:* ${budget_amount:,.2f}\n"
            f"*Alert thresholds:* {threshold_info}\n"
            f"*Billing account:* {billing_account_id}"
        )
        return summary

    except ImportError:
        log.error("google-cloud-billing-budgets not installed.")
        return "Error: billing library not installed."
    except Exception as e:
        log.error(f"Failed to fetch billing summary: {e}")
        return f"Error fetching billing data: {e}"
