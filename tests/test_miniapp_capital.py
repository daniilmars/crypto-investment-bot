"""Unit tests for the capital breakdown helper in miniapp_queries."""

import pytest

from src.api.miniapp_queries import (
    compute_capital_breakdown,
    _starting_capital_for_strategy,
    _DEFAULT_STARTING_CAPITAL,
)


# ---- _starting_capital_for_strategy --------------------------------------

def test_starting_capital_auto_pool(monkeypatch):
    monkeypatch.setattr(
        "src.api.miniapp_queries.app_config",
        {"settings": {
            "auto_trading": {"paper_trading_initial_capital": 7500.0},
            "paper_trading_initial_capital": 10000.0,
        }})
    assert _starting_capital_for_strategy("auto") == 7500.0


def test_starting_capital_shared_pool(monkeypatch):
    monkeypatch.setattr(
        "src.api.miniapp_queries.app_config",
        {"settings": {"paper_trading_initial_capital": 12345.0}})
    assert _starting_capital_for_strategy("conservative") == 12345.0
    assert _starting_capital_for_strategy("longterm") == 12345.0
    assert _starting_capital_for_strategy("manual") == 12345.0


def test_starting_capital_unknown_strategy_falls_back(monkeypatch):
    monkeypatch.setattr(
        "src.api.miniapp_queries.app_config",
        {"settings": {}})
    monkeypatch.setattr("src.api.miniapp_queries._warned_strategies", set())
    assert _starting_capital_for_strategy("experiment") == _DEFAULT_STARTING_CAPITAL


# ---- compute_capital_breakdown -------------------------------------------

@pytest.fixture
def cfg(monkeypatch):
    """Pin starting capital so tests are deterministic."""
    monkeypatch.setattr(
        "src.api.miniapp_queries.app_config",
        {"settings": {
            "auto_trading": {"paper_trading_initial_capital": 10000.0},
            "paper_trading_initial_capital": 10000.0,
        }})


def test_breakdown_empty_strategy_omitted_by_default(cfg):
    out = compute_capital_breakdown(
        realized_by_strategy={},
        deployed_by_strategy={},
        unrealized_by_strategy={},
        open_count_by_strategy={},
    )
    # No active strategies → empty list
    assert out["by_strategy"] == []
    assert out["total_value_usd"] == 0
    assert out["cash_locked_usd"] == 0
    assert out["cash_free_usd"] == 0
    assert out["utilization_pct"] is None


def test_breakdown_basic_aggregation(cfg):
    """auto: starting 10k, realized +50, deployed 4500, unrealized +5
       free = 10000 + 50 - 4500 = 5550
       total = 5550 + 4500 + 5 = 10055
       utilization = 100*4500/(4500+5550) = 44.78%
    """
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 50.0},
        deployed_by_strategy={"auto": 4500.0},
        unrealized_by_strategy={"auto": 5.0},
        open_count_by_strategy={"auto": 2},
    )
    assert len(out["by_strategy"]) == 1
    s = out["by_strategy"][0]
    assert s["name"] == "auto"
    assert s["starting_usd"] == 10000.0
    assert s["realized_usd"] == 50.0
    assert s["deployed_usd"] == 4500.0
    assert s["unrealized_usd"] == 5.0
    assert s["free_usd"] == 5550.0
    assert s["total_usd"] == 10055.0
    assert s["utilization_pct"] == pytest.approx(44.78, abs=0.05)
    assert s["open_count"] == 2


def test_breakdown_aggregate_invariant(cfg):
    """capital.total_value_usd must equal sum of per-strategy totals."""
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 100.0, "conservative": 80.0, "longterm": 17.0},
        deployed_by_strategy={"auto": 3898.0, "conservative": 5290.0, "longterm": 485.0},
        unrealized_by_strategy={"auto": 140.0, "conservative": 112.0, "longterm": 7.0},
        open_count_by_strategy={"auto": 13, "conservative": 10, "longterm": 1},
    )
    by_sum = sum(s["total_usd"] for s in out["by_strategy"])
    assert out["total_value_usd"] == pytest.approx(by_sum, abs=0.01)
    assert out["cash_locked_usd"] == pytest.approx(
        sum(s["deployed_usd"] for s in out["by_strategy"]), abs=0.01)
    assert out["cash_free_usd"] == pytest.approx(
        sum(s["free_usd"] for s in out["by_strategy"]), abs=0.01)


def test_breakdown_utilization_zero_when_nothing_deployed(cfg):
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 0.0},
        deployed_by_strategy={"auto": 0.0},
        unrealized_by_strategy={"auto": 0.0},
        open_count_by_strategy={"auto": 0},
    )
    # No active strategies → empty (default include_empty=False)
    assert out["by_strategy"] == []


def test_breakdown_utilization_clamped_to_100(cfg):
    """Pathological: deployed > free shouldn't break clamping."""
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": -9000.0},  # huge loss
        deployed_by_strategy={"auto": 5000.0},
        unrealized_by_strategy={"auto": 0.0},
        open_count_by_strategy={"auto": 1},
    )
    s = out["by_strategy"][0]
    # free = 10000 + (-9000) - 5000 = -4000 → denom = 5000 + (-4000) = 1000
    # util = 5000 / 1000 = 500% → clamped to 100
    assert 0.0 <= s["utilization_pct"] <= 100.0
    assert s["utilization_pct"] == 100.0


def test_breakdown_manual_omitted_when_empty(cfg):
    """Manual strategy with no positions and no realized PnL shouldn't appear."""
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 50.0},
        deployed_by_strategy={"auto": 1000.0},
        unrealized_by_strategy={"auto": 5.0},
        open_count_by_strategy={"auto": 1},
    )
    names = [s["name"] for s in out["by_strategy"]]
    assert "manual" not in names
    assert names == ["auto"]


def test_breakdown_manual_included_when_active(cfg):
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 0.0, "manual": 25.0},
        deployed_by_strategy={"auto": 0.0, "manual": 500.0},
        unrealized_by_strategy={"auto": 0.0, "manual": 12.0},
        open_count_by_strategy={"auto": 0, "manual": 1},
    )
    names = [s["name"] for s in out["by_strategy"]]
    assert "manual" in names


def test_breakdown_unknown_strategy_appended(cfg):
    """A strategy name not in _KNOWN_STRATEGIES (e.g., experiment) still gets
    a row if it has activity."""
    out = compute_capital_breakdown(
        realized_by_strategy={"experiment": 5.0},
        deployed_by_strategy={"experiment": 100.0},
        unrealized_by_strategy={"experiment": 1.0},
        open_count_by_strategy={"experiment": 1},
    )
    names = [s["name"] for s in out["by_strategy"]]
    assert "experiment" in names


def test_breakdown_includes_known_strategies_when_include_empty(cfg):
    """include_empty=True surfaces auto/conservative/longterm even with 0 activity
    (manual stays gated)."""
    out = compute_capital_breakdown(
        realized_by_strategy={},
        deployed_by_strategy={},
        unrealized_by_strategy={},
        open_count_by_strategy={},
        include_empty=True,
    )
    names = [s["name"] for s in out["by_strategy"]]
    assert "auto" in names
    assert "conservative" in names
    assert "longterm" in names
    assert "manual" not in names  # always omitted when empty
    # All start at $10k, $0 deployed, $0 unrealized
    for s in out["by_strategy"]:
        assert s["total_usd"] == 10000.0
        assert s["free_usd"] == 10000.0
        assert s["deployed_usd"] == 0.0


def test_breakdown_response_shape(cfg):
    """Lock the public shape of the capital object (frontend depends on it)."""
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 50.0},
        deployed_by_strategy={"auto": 1000.0},
        unrealized_by_strategy={"auto": 5.0},
        open_count_by_strategy={"auto": 1},
    )
    assert set(out.keys()) >= {
        "total_value_usd", "cash_locked_usd", "cash_free_usd",
        "utilization_pct", "return_pct", "realized_return_pct",
        "by_strategy", "fx_method", "as_of_ts",
    }
    assert set(out["by_strategy"][0].keys()) >= {
        "name", "starting_usd", "realized_usd", "deployed_usd",
        "unrealized_usd", "free_usd", "total_usd", "open_count",
        "utilization_pct", "return_pct", "realized_return_pct",
    }


# ---- return_pct / realized_return_pct ------------------------------------

def test_return_pct_winning_strategy(cfg):
    """Starting $10k, realized +$50, unrealized +$5 → total $10,055
    return_pct = 55/10000 = 0.55%; realized_return_pct = 0.50%."""
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 50.0},
        deployed_by_strategy={"auto": 1000.0},
        unrealized_by_strategy={"auto": 5.0},
        open_count_by_strategy={"auto": 1},
    )
    s = out["by_strategy"][0]
    assert s["return_pct"] == pytest.approx(0.55, abs=0.01)
    assert s["realized_return_pct"] == pytest.approx(0.50, abs=0.01)


def test_return_pct_losing_strategy(cfg):
    """Starting $10k, realized −$200, unrealized −$50 → total $9,750 → −2.50%."""
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": -200.0},
        deployed_by_strategy={"auto": 1000.0},
        unrealized_by_strategy={"auto": -50.0},
        open_count_by_strategy={"auto": 1},
    )
    s = out["by_strategy"][0]
    assert s["return_pct"] == pytest.approx(-2.50, abs=0.01)
    assert s["realized_return_pct"] == pytest.approx(-2.00, abs=0.01)


def test_return_pct_zero_starting_is_none(monkeypatch):
    """If starting capital is 0 (shouldn't happen, but defensive), return
    fields must be None so the UI can show '—' instead of div-by-zero."""
    monkeypatch.setattr(
        "src.api.miniapp_queries.app_config",
        {"settings": {
            "auto_trading": {"paper_trading_initial_capital": 0.0},
            "paper_trading_initial_capital": 0.0,
        }})
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 50.0},
        deployed_by_strategy={"auto": 500.0},
        unrealized_by_strategy={"auto": 5.0},
        open_count_by_strategy={"auto": 1},
    )
    s = out["by_strategy"][0]
    assert s["return_pct"] is None
    assert s["realized_return_pct"] is None
    # Aggregate also none when no non-zero starting
    assert out["return_pct"] is None
    assert out["realized_return_pct"] is None


def test_aggregate_return_pct_invariant(cfg):
    """Top-level return_pct must match (total_value - Σ starting) / Σ starting."""
    out = compute_capital_breakdown(
        realized_by_strategy={"auto": 100.0, "conservative": 80.0, "longterm": 17.0},
        deployed_by_strategy={"auto": 3898.0, "conservative": 5290.0, "longterm": 485.0},
        unrealized_by_strategy={"auto": 140.0, "conservative": 112.0, "longterm": 7.0},
        open_count_by_strategy={"auto": 13, "conservative": 10, "longterm": 1},
    )
    total = out["total_value_usd"]
    starting = sum(s["starting_usd"] for s in out["by_strategy"])
    expected = (total - starting) / starting * 100.0
    assert out["return_pct"] == pytest.approx(expected, abs=0.01)
    # And realized-only variant should be strictly less than total when
    # unrealized > 0
    assert out["realized_return_pct"] < out["return_pct"]
