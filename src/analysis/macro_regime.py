"""
Macro Regime Detector — classifies the market as RISK_ON, CAUTION, or RISK_OFF.

Fetches VIX, S&P 500, 10Y Treasury yield, and BTC via yfinance (free, no API key).
Result is cached for 14 minutes (just under the 15-min bot cycle).
"""

import time
from enum import Enum

import yfinance as yf

from src.config import app_config
from src.logger import log


class MacroRegime(str, Enum):
    RISK_ON = "RISK_ON"
    CAUTION = "CAUTION"
    RISK_OFF = "RISK_OFF"


# Module-level cache: {result: dict, fetched_at: float}
_regime_cache = {}
_CACHE_TTL_SECONDS = 14 * 60  # 14 minutes


def get_macro_regime(force_refresh=False) -> dict:
    """Returns the current macro regime classification.

    Returns dict with keys:
        regime, position_size_multiplier, suppress_buys,
        indicators, signals, score, classified_at
    """
    cfg = app_config.get('settings', {}).get('macro_regime', {})
    if not cfg.get('enabled', True):
        return _default_result(MacroRegime.CAUTION)

    now = time.time()
    if (not force_refresh
            and _regime_cache.get('result')
            and now - _regime_cache.get('fetched_at', 0) < _CACHE_TTL_SECONDS):
        return _regime_cache['result']

    indicators = _fetch_macro_indicators()
    signals = _compute_signals(indicators)
    regime, multiplier, suppress_buys = _classify_regime(signals, cfg)
    score = _compute_score(signals)

    result = {
        'regime': regime.value,
        'position_size_multiplier': multiplier,
        'suppress_buys': suppress_buys,
        'indicators': indicators,
        'signals': signals,
        'score': score,
        'classified_at': time.time(),
    }

    _regime_cache['result'] = result
    _regime_cache['fetched_at'] = now

    log.info(f"Macro regime: {regime.value} (score={score}, mult={multiplier}, "
             f"suppress_buys={suppress_buys})")
    return result


def _default_result(regime=MacroRegime.CAUTION):
    """Returns a safe default when disabled or all data fails."""
    cfg = app_config.get('settings', {}).get('macro_regime', {})
    mult_map = {
        MacroRegime.RISK_ON: cfg.get('risk_on_multiplier', 1.0),
        MacroRegime.CAUTION: cfg.get('caution_multiplier', 0.6),
        MacroRegime.RISK_OFF: cfg.get('risk_off_multiplier', 0.3),
    }
    return {
        'regime': regime.value,
        'position_size_multiplier': mult_map.get(regime, 0.6),
        'suppress_buys': False,
        'indicators': {},
        'signals': {},
        'score': 0,
        'classified_at': time.time(),
    }


def _fetch_macro_indicators() -> dict:
    """Fetches VIX, S&P 500, 10Y yield, and BTC data via yfinance."""
    tickers = {
        'vix': '^VIX',
        'sp500': '^GSPC',
        'yield_10y': '^TNX',
        'btc': 'BTC-USD',
    }
    indicators = {}

    for key, ticker_symbol in tickers.items():
        try:
            ticker = yf.Ticker(ticker_symbol)
            hist = ticker.history(period='1y')
            if hist.empty:
                log.warning(f"Macro: No data for {ticker_symbol}")
                indicators[key] = None
                continue

            current = float(hist['Close'].iloc[-1])
            indicators[key] = {
                'current': current,
                'history': hist['Close'],
            }

            # Pre-compute SMAs
            if key == 'vix':
                indicators[key]['sma20'] = float(hist['Close'].rolling(20).mean().iloc[-1])
            elif key == 'sp500':
                indicators[key]['sma200'] = float(hist['Close'].rolling(200).mean().iloc[-1])
            elif key == 'btc':
                indicators[key]['sma50'] = float(hist['Close'].rolling(50).mean().iloc[-1])
            elif key == 'yield_10y':
                # Yield direction: compare current to 20-day-ago value
                if len(hist) >= 20:
                    indicators[key]['prev_20d'] = float(hist['Close'].iloc[-20])
                else:
                    indicators[key]['prev_20d'] = current

        except Exception as e:
            log.warning(f"Macro: Failed to fetch {ticker_symbol}: {e}")
            indicators[key] = None

    return indicators


def _compute_signals(indicators: dict) -> dict:
    """Derives directional signals from raw indicators."""
    signals = {}
    cfg = app_config.get('settings', {}).get('macro_regime', {})
    vix_elevated = cfg.get('vix_elevated_threshold', 18)
    vix_high = cfg.get('vix_high_threshold', 25)
    vix_extreme = cfg.get('vix_extreme_threshold', 35)

    # --- VIX level signal ---
    vix = indicators.get('vix')
    if vix and vix.get('current') is not None:
        vix_val = vix['current']
        if vix_val > vix_extreme:
            signals['vix_signal'] = -2
        elif vix_val > vix_high:
            signals['vix_signal'] = -1
        elif vix_val > vix_elevated:
            signals['vix_signal'] = 0
        else:
            signals['vix_signal'] = 1
    else:
        signals['vix_signal'] = 0

    # --- VIX trend (current vs SMA20) ---
    if vix and vix.get('current') is not None and vix.get('sma20') is not None:
        ratio = vix['current'] / vix['sma20'] if vix['sma20'] > 0 else 1.0
        if ratio < 0.95:
            signals['vix_trend'] = 1   # falling → risk-on
        elif ratio > 1.05:
            signals['vix_trend'] = -1  # rising → risk-off
        else:
            signals['vix_trend'] = 0
    else:
        signals['vix_trend'] = 0

    # --- S&P 500 vs SMA200 ---
    sp = indicators.get('sp500')
    if sp and sp.get('current') is not None and sp.get('sma200') is not None:
        signals['sp500_trend'] = 1 if sp['current'] > sp['sma200'] else -1
    else:
        signals['sp500_trend'] = 0

    # --- 10Y Yield direction ---
    yld = indicators.get('yield_10y')
    if yld and yld.get('current') is not None and yld.get('prev_20d') is not None:
        change = yld['current'] - yld['prev_20d']
        if change > 0.3:
            signals['yield_direction'] = -1  # rising fast → risk-off
        elif change < -0.1:
            signals['yield_direction'] = 1   # falling → risk-on
        else:
            signals['yield_direction'] = 0
    else:
        signals['yield_direction'] = 0

    # --- BTC vs SMA50 ---
    btc = indicators.get('btc')
    if btc and btc.get('current') is not None and btc.get('sma50') is not None:
        signals['btc_trend'] = 1 if btc['current'] > btc['sma50'] else -1
    else:
        signals['btc_trend'] = 0

    return signals


_DEFAULT_WEIGHTS = {
    'vix_signal': 2.0,
    'vix_trend': 1.0,
    'sp500_trend': 1.5,
    'yield_direction': 1.5,
    'btc_trend': 1.0,
}


def _compute_score(signals: dict) -> float:
    """Weighted sum of signal values into a single regime score."""
    config = app_config.get('settings', {}).get('macro_regime', {})
    weights = config.get('weights', {})
    return sum(signals.get(k, 0) * weights.get(k, _DEFAULT_WEIGHTS[k])
               for k in _DEFAULT_WEIGHTS)


def _classify_regime(signals: dict, cfg: dict) -> tuple:
    """Scores signals and returns (MacroRegime, multiplier, suppress_buys)."""
    score = _compute_score(signals)

    risk_on_mult = cfg.get('risk_on_multiplier', 1.0)
    caution_mult = cfg.get('caution_multiplier', 0.6)
    risk_off_mult = cfg.get('risk_off_multiplier', 0.3)
    suppress_in_risk_off = cfg.get('suppress_buys_in_risk_off', True)
    risk_on_threshold = cfg.get('risk_on_threshold', 3.0)
    risk_off_threshold = cfg.get('risk_off_threshold', -3.0)

    if score >= risk_on_threshold:
        return MacroRegime.RISK_ON, risk_on_mult, False
    elif score <= risk_off_threshold:
        return MacroRegime.RISK_OFF, risk_off_mult, suppress_in_risk_off
    else:
        return MacroRegime.CAUTION, caution_mult, False


def get_regime_trajectory() -> dict:
    """Compute regime trajectory from recent history.

    Returns dict with days_in_regime, regime_direction (improving/worsening/stable),
    vix_trend (rising/falling/flat), score_trend, and a human-readable summary.
    """
    try:
        from src.database import get_macro_regime_history
        history = get_macro_regime_history(limit=100)
        if not history or len(history) < 2:
            return {'days_in_regime': 0, 'regime_direction': 'unknown',
                    'vix_trend': 'unknown', 'summary': ''}

        current_regime = history[0].get('regime', 'CAUTION')

        # Days in current regime (count consecutive same-regime rows)
        consecutive = 0
        for row in history:
            if row.get('regime') == current_regime:
                consecutive += 1
            else:
                break
        # Approximate days from 15-min cycle intervals
        days_in_regime = round(consecutive * 15 / 60 / 24, 1)

        # Score trend: compare avg of last 10 vs previous 10
        recent_scores = [r.get('score', 0) for r in history[:10] if r.get('score') is not None]
        older_scores = [r.get('score', 0) for r in history[10:20] if r.get('score') is not None]
        if recent_scores and older_scores:
            avg_recent = sum(recent_scores) / len(recent_scores)
            avg_older = sum(older_scores) / len(older_scores)
            score_delta = avg_recent - avg_older
            if score_delta > 0.5:
                regime_direction = 'improving'
            elif score_delta < -0.5:
                regime_direction = 'worsening'
            else:
                regime_direction = 'stable'
        else:
            avg_recent = recent_scores[0] if recent_scores else 0
            regime_direction = 'stable'

        # VIX trend: compare latest vs 24h ago (~96 rows at 15-min intervals)
        vix_now = history[0].get('vix_current')
        vix_rows_ago = min(96, len(history) - 1)
        vix_prev = history[vix_rows_ago].get('vix_current') if vix_rows_ago > 0 else None
        if vix_now is not None and vix_prev is not None:
            vix_delta = vix_now - vix_prev
            if vix_delta > 1.0:
                vix_trend = 'rising'
            elif vix_delta < -1.0:
                vix_trend = 'falling'
            else:
                vix_trend = 'flat'
            vix_str = f"VIX {vix_now:.1f} ({vix_trend} from {vix_prev:.1f})"
        else:
            vix_trend = 'unknown'
            vix_str = f"VIX {vix_now:.1f}" if vix_now else "VIX unknown"

        # Previous regime
        prev_regime = None
        for row in history[consecutive:]:
            prev_regime = row.get('regime')
            break

        summary = (f"{current_regime} for {days_in_regime}d ({regime_direction}). "
                   f"Score: {avg_recent:.1f}. {vix_str}.")

        return {
            'current_regime': current_regime,
            'days_in_regime': days_in_regime,
            'regime_direction': regime_direction,
            'vix_trend': vix_trend,
            'score_trend': f"{avg_recent:.1f}",
            'previous_regime': prev_regime,
            'summary': summary,
        }
    except Exception as e:
        log.warning(f"Failed to compute regime trajectory: {e}")
        return {'days_in_regime': 0, 'regime_direction': 'unknown',
                'vix_trend': 'unknown', 'summary': ''}


def clear_regime_cache():
    """Clears the cached regime result. Useful for tests."""
    _regime_cache.clear()
