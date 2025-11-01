# Networking Configuration for Google Cloud Run

This document explains the networking setup required for the Crypto Investment Bot when deployed on Google Cloud Run.

---

## The Need for a Static Outbound IP Address

### The Problem

By default, Google Cloud Run services do not have a static (fixed) outbound IP address. When the bot, running in a Cloud Run container, makes a request to an external API (like the Whale Alert API), the source IP address of that request is not predictable and can change over time.

Some external APIs, for security or rate-limiting purposes, may restrict access to a specific list of whitelisted IP addresses. More commonly, network-level issues can arise where the ephemeral IP addresses used by Cloud Run are unable to establish a stable connection, leading to errors like `RemoteDisconnected`. This was the case with our connection to the Whale Alert API.

### The Solution

To resolve this and ensure reliable outbound connectivity, we have implemented a networking setup that provides our Cloud Run service with a static, predictable outbound IP address. This setup consists of three main components:

1.  **Serverless VPC Access Connector (`crypto-bot-connector`):** This connector acts as a bridge, allowing our serverless Cloud Run service to send traffic into our project's Virtual Private Cloud (VPC) network.

2.  **Cloud Router (`crypto-bot-router`):** A Cloud Router is a required component for Cloud NAT. It manages the routes that allow traffic to flow from the VPC to the NAT gateway.

3.  **Cloud NAT Gateway (`crypto-bot-nat`):** This is the core of the solution. The NAT (Network Address Translation) gateway takes all the traffic routed to it from the VPC connector, replaces the internal source IP addresses with its own single, static external IP address, and then sends the traffic to the public internet.

### How It Works

1.  The `crypto-bot` Cloud Run service is configured to route all its outbound traffic through the `crypto-bot-connector`.
2.  The connector sends this traffic into our VPC network.
3.  The Cloud Router directs this traffic to the `crypto-bot-nat` gateway.
4.  The NAT gateway sends the traffic to the Whale Alert API (and any other external service) using its static IP address.

This ensures that all outbound connections from our bot originate from a single, reliable source, resolving the `RemoteDisconnected` errors and preparing us for any future services that may require IP whitelisting.

The deployment workflow in `.github/workflows/google-cloud-run.yml` has been updated to include the `--vpc-connector` flag, permanently associating our service with this networking configuration.
