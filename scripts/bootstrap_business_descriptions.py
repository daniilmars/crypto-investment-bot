"""Bootstrap config/business_descriptions.yaml using Gemini.

For each ticker in watch_list.yaml that lacks a description, asks Gemini
for a 1-line summary in the format:
    <sector>: <core business>. <key economic exposure>

Idempotent: an existing description is never overwritten. Run this any
time after adding new tickers to the watch list. Hand-edits to existing
descriptions are preserved.

Usage:
    .venv/bin/python scripts/bootstrap_business_descriptions.py
    .venv/bin/python scripts/bootstrap_business_descriptions.py --dry-run
    .venv/bin/python scripts/bootstrap_business_descriptions.py --only HII,LHX,XOM
"""
import argparse
import os
import sys
import time

import yaml

# Ensure repo root on path so `src.*` imports work when run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.logger import log

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH_LIST = os.path.join(REPO_ROOT, "config", "watch_list.yaml")
DESCRIPTIONS = os.path.join(REPO_ROOT, "config", "business_descriptions.yaml")


def load_watch_list_tickers() -> list[str]:
    """All stock tickers from watch_list.yaml (US + EU + Asia + AI + macro)."""
    with open(WATCH_LIST, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    tickers: list[str] = []
    for key in ("stocks", "stocks_europe", "stocks_asia", "stocks_ai", "stocks_macro"):
        for t in data.get(key) or []:
            if t and t not in tickers:
                tickers.append(str(t))
    return tickers


def load_existing_descriptions() -> dict[str, str]:
    if not os.path.exists(DESCRIPTIONS):
        return {}
    with open(DESCRIPTIONS, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    descs = data.get("descriptions") or {}
    return {str(k): str(v) for k, v in descs.items() if v}


def write_descriptions(descs: dict[str, str]) -> None:
    """Write back as YAML, preserving the file header comment."""
    header = """\
# Business descriptions per ticker — used by Gemini for sector-aware ranking.
#
# Format:
#   <ticker>: "<sector>: <core business>. <key economic exposure>"
#
# Two roles:
#   1. Helps Gemini rank symbols by direct catalyst impact within a sector
#      (e.g. "oil price spike → XOM benefits, VLO is hurt").
#   2. Used as company-name aliases by the headline_validator so headlines
#      like "Merck reports Q1" still match for ticker MRK.
#
# Add new tickers via:  .venv/bin/python scripts/bootstrap_business_descriptions.py
# That script calls Gemini once per missing ticker and appends; idempotent.
# Hand-edit anything wrong; a present human entry is never overwritten.

"""
    sorted_descs = {k: descs[k] for k in sorted(descs.keys())}
    body = yaml.safe_dump(
        {"descriptions": sorted_descs},
        sort_keys=False, allow_unicode=True, width=120, default_flow_style=False)
    with open(DESCRIPTIONS, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(body)


def fetch_one(ticker: str, client) -> str | None:
    """Ask Gemini for one description. Returns None on failure."""
    prompt = (
        f"Ticker: {ticker}\n\n"
        "Write a single-line business description in EXACTLY this format:\n"
        '  "<sector>: <core business>. <key economic exposure or sensitivity>"\n\n'
        "Rules:\n"
        "- Maximum 100 characters total.\n"
        "- Be specific about WHAT they sell/do, not generic platitudes.\n"
        "- Mention the key sensitivity that determines if news is bullish or bearish.\n"
        "  Example sensitivities: 'benefits from oil price spikes', 'hurt by rising rates',\n"
        "  'directly exposed to defense budget changes', 'pure-play refiner — input cost = crude'.\n"
        "- Output ONLY the quoted description. No preamble, no quotes around it, no markdown.\n\n"
        "Examples:\n"
        "  HII: Naval shipbuilder: aircraft carriers, submarines — direct defense-budget exposure.\n"
        "  VLO: Pure-play oil refiner — hurt by crude price spikes (input cost), helped by crack spread expansion.\n"
        "  XOM: Integrated oil major: upstream production + downstream refining; benefits from oil price spikes.\n"
    )
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )
        text = (resp.text or "").strip()
        # Strip surrounding quotes / markdown
        text = text.strip('"').strip("'").strip("`").strip()
        # Drop a leading "TICKER: " if Gemini echoes it
        prefix = f"{ticker}:"
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
        if not text or len(text) > 200:
            log.warning(f"Skipping {ticker}: invalid output ({len(text)} chars)")
            return None
        return text
    except Exception as e:
        log.warning(f"Gemini call failed for {ticker}: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Only print which tickers would be fetched.")
    ap.add_argument("--only", default="",
                    help="Comma-separated tickers; restricts to these.")
    ap.add_argument("--limit", type=int, default=200,
                    help="Max tickers to fetch in one run.")
    args = ap.parse_args()

    tickers = load_watch_list_tickers()
    existing = load_existing_descriptions()

    if args.only:
        whitelist = {t.strip() for t in args.only.split(",") if t.strip()}
        tickers = [t for t in tickers if t in whitelist]

    missing = [t for t in tickers if t not in existing][:args.limit]
    log.info(f"Watchlist: {len(tickers)} tickers; existing descriptions: "
             f"{len(existing)}; missing: {len(missing)}")
    if not missing:
        log.info("Nothing to do.")
        return 0
    if args.dry_run:
        log.info(f"Would fetch: {missing}")
        return 0

    # Lazy import — only need genai client at call time
    from src.analysis.gemini_news_analyzer import _make_genai_client
    client = _make_genai_client()
    if client is None:
        log.error("Could not create Gemini client (no GEMINI_API_KEY / GCP_PROJECT_ID).")
        return 2

    descs = dict(existing)  # don't mutate existing in-place
    fetched = 0
    for i, ticker in enumerate(missing, 1):
        desc = fetch_one(ticker, client)
        if desc:
            descs[ticker] = desc
            fetched += 1
            log.info(f"  [{i}/{len(missing)}] {ticker}: {desc}")
        # Save periodically so we don't lose work on a crash
        if i % 20 == 0:
            write_descriptions(descs)
            log.info(f"  Checkpointed at {i}/{len(missing)}")
        time.sleep(0.6)  # gentle pacing within free-tier quotas

    write_descriptions(descs)
    log.info(f"Done. Fetched {fetched}/{len(missing)} descriptions; "
             f"total now {len(descs)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
