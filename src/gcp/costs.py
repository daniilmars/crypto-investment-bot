# src/gcp/costs.py

import subprocess
import json
from src.logger import log
from src.config import app_config

def get_gcp_billing_summary():
    """
    Fetches the GCP billing summary by executing a gcloud command.

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
        # Construct the gcloud command
        command = [
            "gcloud", "beta", "billing", "budgets", "list",
            f"--billing-account={billing_account_id}",
            "--format=json"
        ]

        log.info(f"Executing gcloud command: {' '.join(command)}")
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        
        budgets = json.loads(result.stdout)
        
        if not budgets:
            return "No active budgets found for the configured billing account."

        # For simplicity, we'll report on the first budget found.
        budget = budgets[0]
        display_name = budget.get('displayName', 'N/A')
        budget_amount = budget['amount']['specifiedAmount']['units']
        
        # Extract current and forecasted spend
        current_spend = budget['budgetForecast']['creditEstimateAmount']['units']
        forecasted_spend = budget['budgetForecast']['forecastAmount']['units']

        # Calculate percentages
        current_percentage = (float(current_spend) / float(budget_amount)) * 100
        forecasted_percentage = (float(forecasted_spend) / float(budget_amount)) * 100

        # Format the output message
        summary = (
            f"**GCP Billing Summary for '{display_name}'**\n\n"
            f"**Budget:** ${float(budget_amount):,.2f}\n"
            f"**Current Spend:** ${float(current_spend):,.2f} ({current_percentage:.2f}%)\n"
            f"**Forecasted Spend:** ${float(forecasted_spend):,.2f} ({forecasted_percentage:.2f}%)"
        )
        return summary

    except FileNotFoundError:
        log.error("gcloud command not found. Make sure the Google Cloud SDK is installed and in the system's PATH.")
        return "Error: gcloud command not found."
    except subprocess.CalledProcessError as e:
        log.error(f"gcloud command failed with error: {e.stderr}")
        return "Error executing gcloud command. Check logs for details. Ensure the service account has 'Billing Account Viewer' permissions."
    except (KeyError, IndexError) as e:
        log.error(f"Failed to parse gcloud billing output. Error: {e}")
        return "Error parsing billing data. The format may have changed."
    except Exception as e:
        log.error(f"An unexpected error occurred in get_gcp_billing_summary: {e}")
        return "An unexpected error occurred while fetching billing data."
