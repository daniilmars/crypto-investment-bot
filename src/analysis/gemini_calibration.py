"""
Gemini confidence calibration: bins closed trades by raw confidence and
tells us what each bucket *actually* predicts about win rate and avg PnL.

Pure functions live here so they're easy to unit-test. The runnable script
in scripts/calibrate_gemini_confidence.py wires the DB join + this logic.

Bucket boundaries: half-open [low, high), capped to fit any conf in [0, 1].
A 'no_attribution' bucket catches trades where no Gemini conf could be
joined.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# 5 buckets + a NO-ATTRIBUTION sink. Boundaries are half-open [low, high).
BUCKETS: list[tuple[float, float, str]] = [
    (0.50, 0.60, "0.50-0.59"),
    (0.60, 0.70, "0.60-0.69"),
    (0.70, 0.80, "0.70-0.79"),
    (0.80, 0.90, "0.80-0.89"),
    (0.90, 1.01, "0.90+"),  # 1.01 to include exact 1.0
]
NO_ATTR = "no_attribution"
BELOW_RANGE = "below_0.50"  # signals that should not have triggered a trade


def bucket_of(conf: Optional[float]) -> str:
    """Map a raw confidence value to a bucket label."""
    if conf is None:
        return NO_ATTR
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return NO_ATTR
    if c < 0.50:
        return BELOW_RANGE
    for low, high, label in BUCKETS:
        if low <= c < high:
            return label
    return BUCKETS[-1][2]  # safety: anything ≥1.01 → top bucket


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson confidence interval for a binomial proportion.

    Returns (low, high) as floats in [0, 1]. Returns (0.0, 0.0) when n=0
    so the caller doesn't have to special-case empty buckets.
    """
    if n <= 0:
        return 0.0, 0.0
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return max(0.0, center - half), min(1.0, center + half)


@dataclass
class BucketStats:
    bucket: str
    n: int
    wins: int
    win_rate: Optional[float]
    avg_pnl: Optional[float]
    ci_low: float
    ci_high: float


def bucketize(rows: list[dict],
              stratify_key: Optional[str] = None) -> dict[str, list[BucketStats]]:
    """Group rows into buckets, optionally stratified.

    Each input row must have ``conf`` (float | None) and ``pnl`` (float).
    Optional: ``win`` (bool, derived from pnl > 0 if absent), and the
    column named by ``stratify_key`` if non-None.

    Returns ``{stratify_value: [BucketStats, ...]}``. With ``stratify_key=None``
    the dict has a single key ``'overall'``.

    Bucket order in each list follows BUCKETS + NO_ATTR + BELOW_RANGE so
    callers can render a deterministic table.
    """
    groups: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        sval = "overall" if not stratify_key else str(r.get(stratify_key) or "unknown")
        bkt = bucket_of(r.get("conf"))
        groups.setdefault(sval, {}).setdefault(bkt, []).append(r)

    bucket_order = [b[2] for b in BUCKETS] + [NO_ATTR, BELOW_RANGE]
    out: dict[str, list[BucketStats]] = {}
    for sval, by_bkt in groups.items():
        stats_list: list[BucketStats] = []
        for label in bucket_order:
            rs = by_bkt.get(label, [])
            n = len(rs)
            if n == 0:
                continue  # don't emit empty buckets
            wins = sum(
                1 for r in rs
                if (r.get("win") if "win" in r else (r.get("pnl") or 0) > 0)
            )
            avg_pnl = sum(float(r.get("pnl") or 0) for r in rs) / n
            ci_low, ci_high = wilson_ci(wins, n)
            stats_list.append(BucketStats(
                bucket=label,
                n=n,
                wins=wins,
                win_rate=(wins / n if n else None),
                avg_pnl=avg_pnl,
                ci_low=ci_low,
                ci_high=ci_high,
            ))
        out[sval] = stats_list
    return out


def render_table(stats_by_group: dict[str, list[BucketStats]],
                 stratify_label: str = "overall",
                 small_n_threshold: int = 10) -> str:
    """Format calibration tables for stdout. Pure function (no I/O)."""
    lines: list[str] = []
    for sval in sorted(stats_by_group.keys()):
        bucket_list = stats_by_group[sval]
        if not bucket_list:
            continue
        header = f"=== {stratify_label.upper()} = {sval} ===" \
            if stratify_label != "overall" else "=== OVERALL ==="
        lines.append(header)
        lines.append(f"{'bucket':<14} {'n':>4} {'wins':>5} {'WR':>6} "
                     f"{'CI95':<14} {'avg_pnl':>10}  note")
        for b in bucket_list:
            wr = f"{100*b.win_rate:>5.1f}%" if b.win_rate is not None else "  -  "
            ci = f"[{100*b.ci_low:.0f}%,{100*b.ci_high:.0f}%]"
            pnl = f"${b.avg_pnl:+.2f}" if b.avg_pnl is not None else "    -"
            note = "n<{}".format(small_n_threshold) if b.n < small_n_threshold else ""
            lines.append(f"  {b.bucket:<12} {b.n:>4} {b.wins:>5} {wr:>6} "
                         f"{ci:<14} {pnl:>10}  {note}")
        lines.append("")
    return "\n".join(lines)


def stats_to_db_rows(stats_by_group: dict[str, list[BucketStats]],
                     stratify_label: str) -> list[tuple]:
    """Flatten bucket stats into tuples ready for INSERT into
    gemini_calibration. Caller adds computed_at via DB DEFAULT."""
    rows = []
    for sval, bucket_list in stats_by_group.items():
        for b in bucket_list:
            rows.append((
                stratify_label, sval, b.bucket,
                b.n, b.wins,
                b.win_rate if b.win_rate is not None else None,
                b.avg_pnl if b.avg_pnl is not None else None,
                b.ci_low, b.ci_high,
            ))
    return rows
