# Automated Cloud Health Checks

This document describes the automated cloud health check workflow, its purpose, how to use it, and what to do if it fails.

---

## Purpose

The `cloud-health-check.yml` GitHub Actions workflow is designed to proactively monitor the health and configuration of the core Google Cloud Platform (GCP) resources required for this project.

It runs automatically on a schedule to ensure that our deployment environment remains stable and correctly configured, preventing deployment failures due to unexpected infrastructure changes.

The workflow verifies three critical areas:

1.  **Enabled APIs:** Checks that essential services like Cloud Run, Artifact Registry, Cloud Build, and Cloud SQL are enabled for the project.
2.  **Service Account Permissions:** Ensures that the `crypto-bot-deployer` service account has the necessary IAM roles to build and deploy the application.
3.  **Cloud SQL Status:** Confirms that the `crypto-bot-db` database instance is running and accessible.

---

## How to Use the Workflow

### Scheduled Runs

The workflow is scheduled to run automatically **every Monday at 9:00 AM UTC**. You do not need to take any action for these scheduled runs.

### Manual Runs

You can trigger the workflow manually at any time. This is useful for verifying the environment's health before a planned deployment or during a troubleshooting session.

To run the workflow manually:

1.  Navigate to the **Actions** tab in your GitHub repository.
2.  In the left sidebar, click on the **"Cloud Health Check"** workflow.
3.  You will see a message saying "This workflow has a `workflow_dispatch` event trigger." Click the **"Run workflow"** button on the right side of the screen.
4.  Leave the branch as `main` and click the green **"Run workflow"** button.

---

## Interpreting the Results

*   **Successful Run (Green Checkmark):** If the workflow completes with a green checkmark, it means all health checks passed. Your GCP environment is correctly configured.

*   **Failed Run (Red X):** If the workflow fails with a red "X", it means one or more of the health checks did not pass. GitHub will send a notification email to you (based on your account's notification settings).

---

## Troubleshooting Failures

If the workflow fails, click on the failed run in the GitHub Actions tab and then click on the `run-health-checks` job to view the logs. The logs will show which step failed and provide an error message.

### Common Failures and How to Fix Them

*   **"Error: Required API '...' is not enabled."**
    *   **Problem:** An essential Google Cloud API has been disabled.
    *   **Solution:** Go to the Google Cloud Console, navigate to "APIs & Services" > "Enabled APIs & services," and re-enable the missing API for your project.

*   **"Error: Service account is missing required role '...'"**
    *   **Problem:** A necessary IAM permission has been removed from the `crypto-bot-deployer` service account.
    *   **Solution:** Go to the Google Cloud Console, navigate to "IAM & Admin" > "IAM," find the `crypto-bot-deployer` service account, and add the missing role back.

*   **"Error: Cloud SQL instance '...' is not in a RUNNABLE state."**
    *   **Problem:** The PostgreSQL database is stopped, suspended, or in an error state.
    *   **Solution:** Go to the Google Cloud Console, navigate to "SQL," select your `crypto-bot-db` instance, and check its status. You may need to restart it or review its logs to diagnose the issue.

By regularly monitoring this workflow, you can ensure the long-term stability of your bot's deployment environment.
