"""Unit tests for src/analysis/gemini_calibration.py — pure functions only."""

import pytest

from src.analysis.gemini_calibration import (
    bucket_of, wilson_ci, bucketize, render_table, stats_to_db_rows,
    NO_ATTR, BELOW_RANGE,
)


# --- bucket_of ---

def test_bucket_of_boundaries():
    assert bucket_of(0.50) == "0.50-0.59"
    assert bucket_of(0.599999) == "0.50-0.59"
    assert bucket_of(0.60) == "0.60-0.69"
    assert bucket_of(0.70) == "0.70-0.79"
    assert bucket_of(0.80) == "0.80-0.89"
    assert bucket_of(0.90) == "0.90+"
    assert bucket_of(0.999) == "0.90+"
    assert bucket_of(1.0) == "0.90+"


def test_bucket_of_below_range():
    """Trades from sub-threshold conf signals get their own bucket — they
    are diagnostic of unusually-loose execution gates."""
    assert bucket_of(0.0) == BELOW_RANGE
    assert bucket_of(0.49) == BELOW_RANGE


def test_bucket_of_none_returns_no_attribution():
    assert bucket_of(None) == NO_ATTR


def test_bucket_of_garbage_returns_no_attribution():
    assert bucket_of("not-a-number") == NO_ATTR
    assert bucket_of("") == NO_ATTR


# --- wilson_ci ---

def test_wilson_ci_known_values():
    """5/10 wins → CI roughly [24%, 76%] at 95% confidence."""
    low, high = wilson_ci(5, 10)
    assert 0.20 < low < 0.28
    assert 0.72 < high < 0.80


def test_wilson_ci_zero_n():
    """Empty sample → (0, 0), not exception."""
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_ci_perfect_wins():
    """100/100 wins → upper bound near 1.0, lower bound below 1.0."""
    low, high = wilson_ci(100, 100)
    assert 0.90 < low < 1.0
    assert high == pytest.approx(1.0, abs=0.01)


def test_wilson_ci_clamped_to_unit_interval():
    low, high = wilson_ci(1, 1)
    assert low >= 0.0
    assert high <= 1.0


# --- bucketize ---

def _row(conf, pnl, **extras):
    return {"conf": conf, "pnl": pnl, **extras}


def test_bucketize_groups_into_correct_buckets():
    rows = [
        _row(0.55, 1.0),     # bucket 0.50-0.59
        _row(0.65, -2.0),    # bucket 0.60-0.69
        _row(0.85, 5.0),     # bucket 0.80-0.89
        _row(None, 3.0),     # no_attribution
    ]
    out = bucketize(rows)
    assert "overall" in out
    labels = [b.bucket for b in out["overall"]]
    assert "0.50-0.59" in labels
    assert "0.60-0.69" in labels
    assert "0.80-0.89" in labels
    assert NO_ATTR in labels


def test_bucketize_omits_empty_buckets():
    """Empty buckets are not emitted in stats_list (callers don't render zeros)."""
    rows = [_row(0.85, 1.0), _row(0.85, 2.0)]
    out = bucketize(rows)
    labels = [b.bucket for b in out["overall"]]
    assert labels == ["0.80-0.89"]


def test_bucketize_win_rate_and_avg_pnl_correct():
    rows = [
        _row(0.75, 5.0),    # win
        _row(0.75, -2.0),   # loss
        _row(0.75, 3.0),    # win
    ]
    stats = bucketize(rows)["overall"]
    bucket = next(b for b in stats if b.bucket == "0.70-0.79")
    assert bucket.n == 3
    assert bucket.wins == 2
    assert bucket.win_rate == pytest.approx(2 / 3, abs=0.001)
    assert bucket.avg_pnl == pytest.approx((5 - 2 + 3) / 3, abs=0.001)


def test_bucketize_stratified_by_direction():
    rows = [
        _row(0.85, 5.0, direction="bullish"),
        _row(0.85, -3.0, direction="bullish"),
        _row(0.85, 2.0, direction="bearish"),
    ]
    out = bucketize(rows, stratify_key="direction")
    assert set(out.keys()) == {"bullish", "bearish"}
    assert sum(b.n for b in out["bullish"]) == 2
    assert sum(b.n for b in out["bearish"]) == 1


def test_bucketize_explicit_win_field_overrides_pnl_sign():
    """If 'win' is set explicitly (e.g. caller has nuanced criteria), use it."""
    rows = [
        _row(0.75, -1.0, win=True),   # loss by PnL but flagged a win
        _row(0.75, 5.0, win=False),   # gain but flagged a loss
    ]
    bucket = next(b for b in bucketize(rows)["overall"] if b.bucket == "0.70-0.79")
    assert bucket.wins == 1


def test_bucketize_stratify_by_missing_key_yields_unknown():
    rows = [_row(0.75, 1.0), _row(0.75, 1.0, exit_reason="trailing_stop")]
    out = bucketize(rows, stratify_key="exit_reason")
    assert "unknown" in out
    assert "trailing_stop" in out


# --- render_table ---

def test_render_table_includes_n_and_pnl():
    stats = bucketize([_row(0.85, 5.0), _row(0.85, -3.0)])
    rendered = render_table(stats)
    assert "OVERALL" in rendered
    assert "0.80-0.89" in rendered
    assert "$" in rendered  # PnL formatting
    assert "%" in rendered  # win-rate formatting


def test_render_table_flags_small_n():
    stats = bucketize([_row(0.85, 1.0)])
    rendered = render_table(stats, small_n_threshold=10)
    assert "n<10" in rendered


def test_render_table_no_small_n_warning_when_above_threshold():
    rows = [_row(0.85, 1.0) for _ in range(15)]
    rendered = render_table(bucketize(rows), small_n_threshold=10)
    assert "n<10" not in rendered


# --- stats_to_db_rows ---

def test_stats_to_db_rows_shape():
    stats = bucketize([_row(0.85, 5.0), _row(0.85, -3.0)])
    rows = stats_to_db_rows(stats, "overall")
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "overall"      # stratify_by
    assert r[1] == "overall"      # stratify_value
    assert r[2] == "0.80-0.89"    # conf_bucket
    assert r[3] == 2              # n
    assert r[4] == 1              # wins
    assert 0.0 <= r[5] <= 1.0     # win_rate
    # avg_pnl
    assert r[6] == pytest.approx((5 - 3) / 2)
    # ci_low / ci_high
    assert 0.0 <= r[7] <= r[8] <= 1.0


# --- run_calibration (extracted for background loop) ---

def test_run_calibration_empty_trades_returns_skipped(monkeypatch):
    """Empty DB → skipped=True, no crash."""
    from unittest.mock import MagicMock
    from scripts.calibrate_gemini_confidence import run_calibration

    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.fetch_calibration_rows",
        lambda _conn: [])
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.get_db_connection",
        lambda: MagicMock())
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.release_db_connection",
        lambda _c: None)

    result = run_calibration(persist=True, print_tables=False)
    assert result['skipped'] is True
    assert result['rows'] == 0
    assert result['persisted'] == 0


def test_run_calibration_no_db_returns_error(monkeypatch):
    """DB connection failure surfaces as error dict instead of crashing."""
    from scripts.calibrate_gemini_confidence import run_calibration
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.get_db_connection",
        lambda: None)
    result = run_calibration(persist=True, print_tables=False)
    assert result.get('error') == 'no_db_connection'
    assert result['persisted'] == 0


def test_run_calibration_with_data_persists(monkeypatch):
    """Non-empty rows → persist called, returns n_written."""
    from unittest.mock import MagicMock
    from scripts.calibrate_gemini_confidence import run_calibration

    fake_rows = [
        {"conf": 0.85, "pnl": 10.0, "direction": "bullish",
         "trading_strategy": "auto", "exit_reason": "take_profit"},
        {"conf": 0.85, "pnl": -5.0, "direction": "bullish",
         "trading_strategy": "auto", "exit_reason": "stop_loss"},
        {"conf": 0.75, "pnl": 3.0, "direction": "bullish",
         "trading_strategy": "auto", "exit_reason": "trailing_stop"},
    ]
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.fetch_calibration_rows",
        lambda _conn: fake_rows)
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.get_db_connection",
        lambda: MagicMock())
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.release_db_connection",
        lambda _c: None)
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.persist_results",
        lambda _conn, _results: 4)

    result = run_calibration(persist=True, print_tables=False)
    assert result['skipped'] is False
    assert result['rows'] == 3
    assert result['with_conf'] == 3
    assert result['persisted'] == 4


def test_run_calibration_no_persist(monkeypatch):
    """persist=False skips DB write but still computes stats."""
    from unittest.mock import MagicMock
    from scripts.calibrate_gemini_confidence import run_calibration

    fake_rows = [{"conf": 0.85, "pnl": 10.0, "direction": "bullish",
                  "trading_strategy": "auto", "exit_reason": "take_profit"}]
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.fetch_calibration_rows",
        lambda _conn: fake_rows)
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.get_db_connection",
        lambda: MagicMock())
    monkeypatch.setattr(
        "scripts.calibrate_gemini_confidence.release_db_connection",
        lambda _c: None)

    result = run_calibration(persist=False, print_tables=False)
    assert result['persisted'] == 0
    assert result['rows'] == 1
