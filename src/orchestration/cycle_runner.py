"""Cycle runner — orchestrates bot and stock trading cycles.

Relocated from main.py to reduce file size. No logic changes.
"""

import asyncio
import time

import pandas as pd

from src.analysis.signal_engine import generate_signal
from src.analysis.stock_signal_engine import generate_stock_signal
from src.analysis.technical_indicators import (
    calculate_atr, calculate_rsi, calculate_sma,
)
from src.analysis.dynamic_risk import compute_dynamic_sl_tp
from src.collectors.alpha_vantage_data import (get_company_overview,
                                               get_daily_prices,
                                               get_stock_price,
                                               get_batch_stock_prices,
                                               get_batch_daily_prices)
from src.collectors.binance_data import get_current_price, get_all_prices, get_klines
from src.config import app_config
from src.analysis.macro_regime import get_macro_regime
from src.analysis.event_calendar import get_upcoming_macro_events
from src.database import (get_historical_prices,
                          get_trade_history_stats,
                          save_signal, save_macro_regime)
from src.execution.binance_trader import (get_account_balance,
                                          get_open_positions,
                                          _is_live_trading, _get_trading_mode)
from src.execution.circuit_breaker import (check_circuit_breaker, get_daily_pnl,
                                           get_circuit_breaker_status,
                                           get_recent_closed_trades,
                                           get_unrealized_pnl,
                                           update_session_peak)
from src.execution.stock_trader import (
    get_stock_positions, get_stock_balance,
    _is_market_open, _check_pdt_rule,
)
from src.logger import log
from src.notify.telegram_bot import send_telegram_alert
from src.notify.telegram_alerts_enhanced import (
    check_realtime_alerts, send_realtime_alerts,
)
from src.orchestration import bot_state
from src.state import bot_is_running
from src.orchestration.position_monitor import monitor_position
from src.orchestration.position_analyst import run_position_analyst
from src.orchestration.trade_executor import process_trade_signal
from src.orchestration.news_pipeline import (
    collect_and_analyze_news, run_proactive_market_alerts,
)
from src.analysis.strategy_weights import compute_effective_strength
from src.config import get_strategy_configs

# --- Daily kline cache (Plan 2: Daily SMA + Plan 1: ATR) ---
_daily_kline_cache: dict[str, list[dict]] = {}
_daily_kline_cache_ts: float = 0


async def _fetch_daily_klines_batch(
    symbols: list[str], cache_minutes: int = 60
) -> dict[str, list[dict]]:
    """Fetch daily klines for all crypto symbols. Cache for `cache_minutes`."""
    global _daily_kline_cache, _daily_kline_cache_ts

    now = time.time()
    if _daily_kline_cache and (now - _daily_kline_cache_ts) < cache_minutes * 60:
        log.debug("Using cached daily klines (still fresh).")
        return _daily_kline_cache

    result: dict[str, list[dict]] = {}
    for sym in symbols:
        api_sym = sym if sym.endswith("USDT") else f"{sym}USDT"
        try:
            klines = await asyncio.to_thread(get_klines, api_sym, '1d', 210)
            if klines:
                result[sym] = klines
        except Exception as e:
            log.warning(f"Failed to fetch daily klines for {sym}: {e}")

    _daily_kline_cache = result
    _daily_kline_cache_ts = now
    log.info(f"Fetched daily klines for {len(result)}/{len(symbols)} crypto symbols.")
    return result


# Reference to the Telegram application — set by main.py at startup
_application = None


def set_application(app):
    """Set the Telegram application reference (called from main.py startup)."""
    global _application
    _application = app


async def run_bot_cycle():
    """
    Executes one full cycle of the bot's logic.
    """
    log.info("--- Starting new bot cycle ---")
    settings = app_config.get('settings', {})

    # Load all settings
    watch_list = settings.get('watch_list', ['BTC'])

    # Merge chat watchlist additions (crypto)
    try:
        from src.database import get_active_watchlist
        chat_watchlist = get_active_watchlist(asset_type='crypto')
        for item in chat_watchlist:
            sym = item['symbol']
            if sym not in watch_list:
                watch_list.append(sym)
                log.debug(f"Watchlist: added {sym} from chat")
    except Exception as e:
        log.warning(f"Failed to load chat watchlist: {e}")

    sma_period = settings.get('sma_period', 20)
    rsi_period = settings.get('rsi_period', 14)
    rsi_overbought_threshold = settings.get('rsi_overbought_threshold', 70)
    rsi_oversold_threshold = settings.get('rsi_oversold_threshold', 30)
    stoploss_cooldown_hours = settings.get('stoploss_cooldown_hours', 6)
    signal_cooldown_hours = settings.get('signal_cooldown_hours', 4)

    signal_mode = settings.get('signal_mode', 'scoring')
    sentiment_signal_cfg = settings.get('sentiment_signal', {})
    sector_review_cfg = settings.get('sector_review', {})
    sentiment_config = {
        'min_gemini_confidence': sentiment_signal_cfg.get('min_gemini_confidence', 0.7),
        'rsi_buy_veto_threshold': sentiment_signal_cfg.get('rsi_buy_veto_threshold', 75),
        'rsi_sell_veto_threshold': sentiment_signal_cfg.get('rsi_sell_veto_threshold', 25),
        'conviction_influence_pct': sector_review_cfg.get('conviction_influence_pct', 0.10),
    }

    paper_trading = settings.get('paper_trading', True)
    paper_trading_initial_capital = settings.get('paper_trading_initial_capital', 10000.0)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    max_concurrent_positions = settings.get('max_concurrent_positions', 3)
    trailing_stop_enabled = settings.get('trailing_stop_enabled', True)
    trailing_stop_activation = settings.get('trailing_stop_activation', 0.02)
    trailing_stop_distance = settings.get('trailing_stop_distance', 0.015)

    # --- Dynamic Position Sizing (Kelly Criterion) ---
    trade_stats = await get_trade_history_stats()
    kelly_fraction = trade_stats.get('kelly_fraction', 0.0)
    if kelly_fraction > 0 and trade_stats.get('total_trades', 0) >= 10:
        effective_risk_pct = kelly_fraction
        log.info(f"Using Kelly-based position sizing: {effective_risk_pct:.4f} "
                 f"(based on {trade_stats['total_trades']} trades, "
                 f"win rate {trade_stats['win_rate']:.1%})")
    else:
        effective_risk_pct = trade_risk_percentage
        log.info(f"Using fixed position sizing: {effective_risk_pct:.4f} "
                 f"({trade_stats.get('total_trades', 0)} trades, need 10+ for Kelly)")

    # --- Macro Regime Detection ---
    macro_regime_result = await asyncio.to_thread(get_macro_regime)
    macro_multiplier = macro_regime_result['position_size_multiplier']
    suppress_buys = macro_regime_result.get('suppress_buys', False)
    log.info(f"Macro regime: {macro_regime_result['regime']} "
             f"(mult={macro_multiplier}, suppress_buys={suppress_buys})")

    try:
        await save_macro_regime(macro_regime_result)
    except Exception as e:
        log.warning(f"Failed to save macro regime: {e}")

    # --- Real-time Market Alerts (regime change, VIX spike) ---
    try:
        rt_alerts = check_realtime_alerts(macro_regime_result)
        if rt_alerts and _application:
            await send_realtime_alerts(_application, rt_alerts)
    except Exception as e:
        log.warning(f"Realtime alerts check failed: {e}")

    # 1. Collect news data
    log.info("Fetching data from all sources...")

    stock_settings = settings.get('stock_trading', {})
    stock_watch_list = stock_settings.get('watch_list', []) if stock_settings.get('enabled', False) else []
    all_symbols = list(set(watch_list + stock_watch_list))

    # Build current prices dict via batch API call
    current_prices_dict = {}
    all_binance_prices = await asyncio.to_thread(get_all_prices)
    for sym in all_symbols:
        api_sym = sym if sym.endswith("USDT") else f"{sym}USDT"
        price = all_binance_prices.get(api_sym)
        if price:
            current_prices_dict[sym] = price

    gemini_assessments, news_per_symbol = await collect_and_analyze_news(
        all_symbols, current_prices_dict, settings,
        macro_regime_result=macro_regime_result)

    # Persist Gemini assessments for backtesting
    if gemini_assessments:
        try:
            from src.database import save_gemini_assessments
            await asyncio.to_thread(save_gemini_assessments, gemini_assessments)
        except Exception as e:
            log.debug(f"Assessment persistence skipped: {e}")

    # --- Proactive Market Event Alerts ---
    all_watch_symbols = watch_list + settings.get('stock_trading', {}).get('watch_list', [])
    await run_proactive_market_alerts(
        all_watch_symbols, settings, gemini_assessments, news_per_symbol)

    # Cache open positions once per cycle
    is_live = _is_live_trading()
    _cached_crypto_positions = await asyncio.to_thread(get_open_positions, trading_strategy='manual') if (paper_trading or is_live) else []

    # Load all configured strategies and cache their positions
    auto_cfg = settings.get('auto_trading', {})
    auto_enabled = auto_cfg.get('enabled', False)
    strategy_configs = get_strategy_configs(settings)
    _cached_strategy_positions: dict[str, list] = {}
    for strat_name, strat_cfg in strategy_configs.items():
        if strat_cfg.get('enabled', False):
            _cached_strategy_positions[strat_name] = await asyncio.to_thread(
                get_open_positions, trading_strategy=strat_name)

    _cached_auto_positions = _cached_strategy_positions.get('auto', [])  # used by dashboard

    # Risk management config for position monitor
    risk_cfg = dict(
        stop_loss_pct=stop_loss_percentage,
        take_profit_pct=take_profit_percentage,
        trailing_stop_enabled=trailing_stop_enabled,
        trailing_stop_activation=trailing_stop_activation,
        trailing_stop_distance=trailing_stop_distance,
        stoploss_cooldown_hours=stoploss_cooldown_hours,
    )

    # RISK_OFF: tighten trailing stops to accelerate exits
    if macro_regime_result['regime'] == 'RISK_OFF':
        risk_off_cfg = settings.get('macro_regime', {}).get('risk_off_exit_acceleration', {})
        activation_mult = risk_off_cfg.get('trailing_activation_multiplier', 0.5)
        distance_mult = risk_off_cfg.get('trailing_distance_multiplier', 0.7)
        risk_cfg['trailing_stop_activation'] *= activation_mult
        risk_cfg['trailing_stop_distance'] *= distance_mult
        log.info(f"RISK_OFF: tightened trailing stop "
                 f"(activation={risk_cfg['trailing_stop_activation']:.3f}, "
                 f"distance={risk_cfg['trailing_stop_distance']:.3f})")

    trading_mode = _get_trading_mode()
    news_config = settings.get('news_analysis', {})

    # Circuit breaker check — once per cycle, crypto only
    live_config = settings.get('live_trading', {})
    cb_tripped = False
    if is_live:
        cb_balance = (await asyncio.to_thread(get_account_balance, asset_type='crypto')).get('USDT', 0)
        await asyncio.to_thread(update_session_peak, cb_balance, 'crypto')
        cb_daily_pnl = await asyncio.to_thread(get_daily_pnl, asset_type='crypto')
        cb_unrealized = await asyncio.to_thread(get_unrealized_pnl, current_prices_dict, 'crypto')
        cb_effective_pnl = cb_daily_pnl + cb_unrealized
        cb_recent_trades = await asyncio.to_thread(
            get_recent_closed_trades,
            limit=live_config.get('max_consecutive_losses', 3), asset_type='crypto')
        cb_tripped, cb_reason = await asyncio.to_thread(
            check_circuit_breaker,
            cb_balance, cb_effective_pnl, cb_recent_trades, asset_type='crypto',
            current_prices=current_prices_dict)
        if cb_tripped:
            log.warning(f"Circuit breaker active for this cycle: {cb_reason}")
            await send_telegram_alert({
                "signal": "CIRCUIT_BREAKER", "symbol": "ALL",
                "reason": f"Circuit breaker (crypto): {cb_reason}. Skipping all crypto trading this cycle."
            })

    # --- Fetch daily klines for crypto (SMA trend filter + ATR dynamic risk) ---
    trend_cfg = settings.get('trend_filter', {})
    use_daily_klines = trend_cfg.get('use_daily_klines', True)
    daily_sma_period = trend_cfg.get('daily_sma_period', sma_period)
    kline_cache_min = trend_cfg.get('kline_cache_minutes', 60)

    daily_klines_batch: dict[str, list[dict]] = {}
    if use_daily_klines:
        daily_klines_batch = await _fetch_daily_klines_batch(
            watch_list, cache_minutes=kline_cache_min)

    # --- Dynamic risk config ---
    dyn_risk_cfg = settings.get('dynamic_risk', {})
    dyn_risk_enabled = dyn_risk_cfg.get('enabled', False)
    atr_period = dyn_risk_cfg.get('atr_period', 14)

    # --- Check pending limit orders ---
    limit_cfg = settings.get('limit_orders', {})
    if limit_cfg.get('enabled', False):
        await _check_pending_limit_orders(
            current_prices_dict, settings,
            risk_cfg=risk_cfg, trading_mode=trading_mode)

    # Process each symbol in the watch list
    for symbol in watch_list:
        signal = None
        log.info(f"--- Processing symbol: {symbol} ---")

        # Use batch price from all_binance_prices, fall back to individual call
        api_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
        current_price = all_binance_prices.get(api_symbol)
        if current_price:
            from src.collectors.binance_data import save_price_data
            await asyncio.to_thread(save_price_data, {'symbol': symbol, 'price': current_price})
        else:
            price_data = await asyncio.to_thread(get_current_price, api_symbol)
            if not price_data or not price_data.get('price'):
                log.warning(f"Could not fetch current price for {api_symbol}. Skipping analysis.")
                continue
            current_price = float(price_data.get('price'))

        if not current_price:
            log.warning(f"Could not fetch current price for {api_symbol}. Skipping analysis.")
            continue
        log.info(f"Current price for {symbol}: ${current_price:,.2f}")

        # --- Position Monitoring: SL/TP/Trailing Stop ---
        if paper_trading or is_live:
            for position in _cached_crypto_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    # Use per-position dynamic SL/TP if stored, else global
                    pos_risk = dict(risk_cfg)
                    if position.get('dynamic_sl_pct') is not None:
                        pos_risk['stop_loss_pct'] = position['dynamic_sl_pct']
                    if position.get('dynamic_tp_pct') is not None:
                        pos_risk['take_profit_pct'] = position['dynamic_tp_pct']
                    result = await monitor_position(
                        position, current_price, **pos_risk,
                        mode_label=trading_mode.upper())
                    if result != 'none':
                        _cached_crypto_positions = await asyncio.to_thread(
                            get_open_positions, trading_strategy='manual')

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new signal generation and trading.")
            continue

        # 2. Compute technical indicators (SMA from daily klines, RSI from snapshots)
        log.info(f"Analyzing data for {symbol}...")

        market_price_data = {'current_price': current_price, 'sma': None, 'rsi': None,
                             'sma50': None, 'sma200': None}

        # Daily SMA from klines (trend filter) — falls back to 15-min snapshots
        daily_klines = daily_klines_batch.get(symbol)
        if daily_klines and len(daily_klines) >= daily_sma_period:
            daily_closes = [k['close'] for k in daily_klines]
            market_price_data['sma'] = calculate_sma(daily_closes, period=daily_sma_period)
            # Multi-timeframe SMAs for trend alignment
            if len(daily_closes) >= 50:
                market_price_data['sma50'] = sum(daily_closes[-50:]) / 50
            if len(daily_closes) >= 200:
                market_price_data['sma200'] = sum(daily_closes[-200:]) / 200
        else:
            # Fallback: 15-min snapshot SMA (old behavior)
            price_limit = max(sma_period, rsi_period, 26) + 1
            fallback_prices = await get_historical_prices(symbol, price_limit)
            if len(fallback_prices) >= sma_period:
                price_series = pd.Series(fallback_prices)
                market_price_data['sma'] = price_series.rolling(window=sma_period).mean().iloc[-1]

        # RSI from 15-min snapshots (momentum/timing — fast data is fine)
        rsi_prices = await get_historical_prices(symbol, rsi_period + 1)
        market_price_data['rsi'] = calculate_rsi(rsi_prices, period=rsi_period)

        # ATR from daily klines → dynamic SL/TP
        symbol_dynamic_sl = None
        symbol_dynamic_tp = None
        if dyn_risk_enabled and daily_klines and len(daily_klines) >= atr_period + 1:
            highs = [k['high'] for k in daily_klines]
            lows = [k['low'] for k in daily_klines]
            closes = [k['close'] for k in daily_klines]
            atr_val = calculate_atr(highs, lows, closes, period=atr_period)
            atr_pct = atr_val / current_price if atr_val and current_price else None
            symbol_dynamic_sl, symbol_dynamic_tp = compute_dynamic_sl_tp(
                atr_pct, stop_loss_percentage, take_profit_percentage,
                sl_atr_mult=dyn_risk_cfg.get('sl_atr_multiplier', 1.5),
                tp_atr_mult=dyn_risk_cfg.get('tp_atr_multiplier', 3.0),
                sl_floor=dyn_risk_cfg.get('sl_floor', 0.02),
                sl_ceiling=dyn_risk_cfg.get('sl_ceiling', 0.07),
                tp_floor=dyn_risk_cfg.get('tp_floor', 0.04),
                tp_ceiling=dyn_risk_cfg.get('tp_ceiling', 0.15),
            )
            log.info(f"Dynamic risk for {symbol}: SL={symbol_dynamic_sl:.2%}, TP={symbol_dynamic_tp:.2%} (ATR%={atr_pct:.4f})")

        log.info(f"Technical Indicators for {symbol}: SMA={market_price_data['sma']}, RSI={market_price_data['rsi']}")

        # --- Position Analyst (tri-state: HOLD / INCREASE / SELL) ---
        if paper_trading or is_live:
            for position in _cached_crypto_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    await run_position_analyst(
                        position, current_price, market_price_data, settings,
                        news_per_symbol, trailing_stop_activation=trailing_stop_activation)

        # Skip signal generation for symbols with open positions
        if (paper_trading or is_live) and any(
            p['symbol'] == symbol and p['status'] == 'OPEN'
            for p in _cached_crypto_positions
        ):
            log.debug(f"Skipping signal generation for {symbol}: position open (managed by position monitor).")
            continue

        # 3. Generate a signal
        log.info(f"Generating signal for {symbol}...")

        symbol_news_data = None
        ga = gemini_assessments.get('symbol_assessments', {}).get(symbol) if gemini_assessments else None

        if ga:
            symbol_news_data = {
                'gemini_assessment': ga,
            }

        # Inject sector conviction into per-symbol config copy
        sym_sentiment_config = dict(sentiment_config) if sentiment_config else {}
        from src.analysis.sector_limits import get_symbol_group
        from src.analysis.sector_review import get_sector_conviction
        _sym_group = get_symbol_group(symbol)
        if _sym_group:
            _conviction = get_sector_conviction(_sym_group)
            if _conviction != 0.0:
                sym_sentiment_config['sector_conviction'] = _conviction

        signal = generate_signal(
            symbol=symbol,
            market_data=market_price_data,
            news_sentiment_data=symbol_news_data,
            signal_mode=signal_mode,
            sentiment_config=sym_sentiment_config,
            rsi_overbought_threshold=rsi_overbought_threshold,
            rsi_oversold_threshold=rsi_oversold_threshold,
        )
        log.info(f"Generated Signal for {symbol}: {signal}")

        # Enrich signal with Gemini metadata for decision tracking +
        # post-order attribution linkage.
        if ga and signal.get('signal') != 'HOLD':
            signal['gemini_confidence'] = ga.get('confidence')
            signal['gemini_direction'] = ga.get('direction')
            signal['catalyst_type'] = ga.get('catalyst_type')
            signal['catalyst_freshness'] = ga.get('catalyst_freshness')

        await save_signal(signal)

        # --- 4. Trade Execution (Paper & Live) with Dynamic Sizing ---
        # Preserve original signal before manual path can mutate it
        # (process_trade_signal sets signal['signal']='HOLD' on cooldown/regime block)
        original_signal = dict(signal) if signal else None
        can_trade = paper_trading or is_live

        if can_trade:
            # Circuit breaker — already checked once before the loop
            if cb_tripped:
                continue

            log.info(f"Processing signal for {trading_mode} trading...")

            # Balance and position limits
            if is_live:
                current_balance = (await asyncio.to_thread(get_account_balance, asset_type='crypto')).get('USDT', live_config.get('initial_capital', 100.0))
                active_max_positions = live_config.get('max_concurrent_positions', max_concurrent_positions)
            else:
                current_balance = (await asyncio.to_thread(get_account_balance, asset_type='crypto')).get('total_usd', paper_trading_initial_capital)
                active_max_positions = max_concurrent_positions

            await process_trade_signal(
                symbol, signal, current_price, _cached_crypto_positions, current_balance,
                effective_risk_pct, signal_cooldown_hours, active_max_positions,
                suppress_buys, macro_multiplier, label=trading_mode,
                current_prices=current_prices_dict,
                dynamic_sl_pct=symbol_dynamic_sl,
                dynamic_tp_pct=symbol_dynamic_tp)

        # --- Strategy Bots: Position Monitoring + Signal Execution ---
        for strat_name, strat_cfg in strategy_configs.items():
            if not strat_cfg.get('enabled', False):
                continue
            strat_positions = _cached_strategy_positions.get(strat_name, [])
            strat_label = strat_name.upper()

            # Build per-strategy risk config (fall back to global defaults)
            strat_risk_params = strat_cfg.get('risk_params', {})
            strat_risk = dict(
                stop_loss_pct=strat_risk_params.get('stop_loss_percentage', stop_loss_percentage),
                take_profit_pct=strat_risk_params.get('take_profit_percentage', take_profit_percentage),
                trailing_stop_enabled=strat_risk_params.get('trailing_stop_enabled', trailing_stop_enabled),
                trailing_stop_activation=strat_risk_params.get('trailing_stop_activation', trailing_stop_activation),
                trailing_stop_distance=strat_risk_params.get('trailing_stop_distance', trailing_stop_distance),
                stoploss_cooldown_hours=stoploss_cooldown_hours,
            )
            # RISK_OFF exit acceleration applies to all strategies
            if macro_regime_result['regime'] == 'RISK_OFF':
                risk_off_cfg = settings.get('macro_regime', {}).get('risk_off_exit_acceleration', {})
                strat_risk['trailing_stop_activation'] *= risk_off_cfg.get('trailing_activation_multiplier', 0.5)
                strat_risk['trailing_stop_distance'] *= risk_off_cfg.get('trailing_distance_multiplier', 0.7)

            # Position Monitoring
            for position in strat_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    pos_risk = dict(strat_risk)
                    if position.get('dynamic_sl_pct') is not None:
                        pos_risk['stop_loss_pct'] = position['dynamic_sl_pct']
                    if position.get('dynamic_tp_pct') is not None:
                        pos_risk['take_profit_pct'] = position['dynamic_tp_pct']
                    result = await monitor_position(
                        position, current_price, **pos_risk,
                        trading_strategy=strat_name, mode_label=strat_label)
                    if result != 'none':
                        _cached_strategy_positions[strat_name] = await asyncio.to_thread(
                            get_open_positions, trading_strategy=strat_name)
                        strat_positions = _cached_strategy_positions[strat_name]
                    elif market_price_data:
                        await run_position_analyst(
                            position, current_price, market_price_data,
                            settings, news_per_symbol,
                            trailing_stop_activation=strat_risk['trailing_stop_activation'],
                            trading_strategy=strat_name)

            # Signal Execution
            # Per-strategy regime behavior
            strat_regime = strat_cfg.get('regime_behavior', {})
            ignore_risk_off = strat_regime.get('ignore_risk_off', False)
            # VIX gate: only allow RISK_OFF bypass when VIX is below threshold
            max_vix = strat_regime.get('max_vix_for_risk_off_bypass')
            if ignore_risk_off and max_vix is not None:
                vix_data = macro_regime_result.get('indicators', {}).get('vix')
                current_vix = vix_data.get('current') if isinstance(vix_data, dict) else None
                if current_vix is not None and current_vix >= max_vix:
                    ignore_risk_off = False  # VIX too high, respect regime
            strat_suppress = suppress_buys and not ignore_risk_off
            strat_macro_mult = macro_multiplier
            strat_cfg_override = strat_cfg  # may be replaced with transition override

            # Regime transition trading: override suppression when RISK_OFF is improving
            transition = macro_regime_result.get('transition', {})
            if (strat_suppress and transition.get('transition_active')
                    and strat_regime.get('allow_transition_trading', False)):
                strat_suppress = False
                strat_macro_mult = transition.get('transition_multiplier', 0.3)
                strat_cfg_override = dict(strat_cfg)
                strat_cfg_override['_transition_active'] = True
                strat_cfg_override['_transition_min_signal_strength'] = strat_regime.get(
                    'transition_min_signal_strength', 0.80)
                log.info(f"[{strat_label}] Transition trading active for {symbol}: "
                         f"mult={strat_macro_mult}, min_str="
                         f"{strat_cfg_override['_transition_min_signal_strength']}")

            if not cb_tripped and bot_is_running.is_set() and original_signal is not None:
                strat_open = [p for p in strat_positions
                              if p.get('asset_type', 'crypto') == 'crypto' and p['status'] == 'OPEN']
                strat_max = strat_cfg.get('max_concurrent_positions', max_concurrent_positions)
                strat_balance = await asyncio.to_thread(
                    get_account_balance, asset_type='crypto', trading_strategy=strat_name)
                strat_available = strat_balance.get('USDT', 0)

                strat_signal = dict(original_signal)
                # Longterm strategy: only BUY thesis stocks
                if strat_name == 'longterm' and strat_signal.get('signal') == 'BUY':
                    from src.analysis.thesis_generator import get_thesis_symbols
                    if symbol not in get_thesis_symbols():
                        continue

                # Apply strategy-specific weighting to signal strength
                if ga and strat_signal.get('signal') in ('BUY', 'SELL'):
                    base_str = strat_signal.get('signal_strength', 0)
                    trend_align = {
                        'price_below_sma50': (market_price_data.get('sma50') is not None
                                              and current_price < market_price_data['sma50']),
                        'price_below_sma200': (market_price_data.get('sma200') is not None
                                               and current_price < market_price_data['sma200']),
                    }
                    eff_str = compute_effective_strength(
                        base_str, ga, strat_cfg.get('weights', {}),
                        trend_alignment=trend_align,
                        signal_direction=strat_signal.get('signal', 'BUY'))
                    strat_signal['signal_strength'] = eff_str
                    # Persist for backtesting (non-blocking)
                    try:
                        from src.database import save_strategy_score
                        await asyncio.to_thread(
                            save_strategy_score, symbol, strat_name,
                            strat_signal['signal'], base_str, eff_str, ga)
                    except Exception:
                        pass
                await process_trade_signal(
                    symbol, strat_signal, current_price, strat_open, strat_available,
                    effective_risk_pct, signal_cooldown_hours, strat_max,
                    strat_suppress, strat_macro_mult,
                    trading_strategy=strat_name, label=strat_label, is_auto=True,
                    current_prices=current_prices_dict,
                    dynamic_sl_pct=symbol_dynamic_sl,
                    dynamic_tp_pct=symbol_dynamic_tp,
                    strategy_config=strat_cfg_override)

        # Update backward compat alias after strategy loop
        _cached_auto_positions = _cached_strategy_positions.get('auto', [])

    # --- Run Stock Trading Cycle ---
    stock_prices = await run_stock_cycle(
        settings, news_per_symbol=news_per_symbol,
        news_config=news_config,
        gemini_assessments=gemini_assessments,
        signal_mode=signal_mode,
        sentiment_config=sentiment_config,
        macro_multiplier=macro_multiplier,
        suppress_buys=suppress_buys)

    # Merge stock prices into current_prices_dict for dashboard
    if stock_prices:
        current_prices_dict.update(stock_prices)

    # --- Update Live Dashboard ---
    try:
        from src.notify.telegram_live_dashboard import update_live_dashboard
        from src.database import get_trade_summary
        stock_positions = await asyncio.to_thread(
            get_open_positions, asset_type='stock')
        auto_summary_data = await get_trade_summary(
            24, 'auto') if auto_enabled else {}
        # Per-strategy summaries for dashboard
        strategy_summaries = {}
        for sn, sc in strategy_configs.items():
            if sc.get('enabled', False):
                strategy_summaries[sn] = await get_trade_summary(24, sn)
        cycle_data = {
            'crypto_positions': _cached_crypto_positions,
            'stock_positions': stock_positions,
            'auto_positions': _cached_auto_positions,
            'strategy_positions': _cached_strategy_positions,
            'strategy_summaries': strategy_summaries,
            'crypto_balance': await asyncio.to_thread(
                get_account_balance, asset_type='crypto'),
            'stock_balance': await asyncio.to_thread(
                get_account_balance, asset_type='stock'),
            'daily_pnl': await asyncio.to_thread(get_daily_pnl),
            'regime': macro_regime_result,
            'cb_status': await asyncio.to_thread(
                get_circuit_breaker_status),
            'events': await asyncio.to_thread(
                get_upcoming_macro_events, 7),
            'prices': current_prices_dict,
            'last_signals': [],
            'auto_summary': auto_summary_data,
        }
        if _application:
            await update_live_dashboard(_application, cycle_data)
    except Exception as e:
        log.warning(f"Dashboard update failed: {e}")

    # Record cycle completion for hung-task detection
    from datetime import datetime as _dt, timezone as _tz
    bot_state.set_last_cycle_at(_dt.now(_tz.utc))


async def _check_pending_limit_orders(
    current_prices: dict,
    settings: dict,
    *,
    risk_cfg: dict,
    trading_mode: str,
):
    """Check all PENDING limit orders — fill if price reached, expire if TTL elapsed."""
    from src.database import get_pending_orders, fill_pending_order, cancel_pending_order
    from datetime import datetime, timezone

    all_strategies = ['manual'] + list(get_strategy_configs(settings).keys())
    for strategy in all_strategies:
        pending = get_pending_orders(asset_type='crypto', trading_strategy=strategy)
        for order in pending:
            symbol = order['symbol']
            limit_price = order.get('limit_price')
            expires_at = order.get('limit_expires_at')

            current_price = current_prices.get(symbol)
            if not current_price or not limit_price:
                continue

            # Check expiry
            now = datetime.now(timezone.utc)
            if expires_at:
                # Handle both string and datetime objects
                if isinstance(expires_at, str):
                    from datetime import datetime as dt
                    try:
                        expires_at = dt.fromisoformat(expires_at.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        expires_at = None
                if expires_at and now >= expires_at:
                    cancel_pending_order(order['order_id'], reason='expired')
                    log.info(f"Limit order {order['order_id']} for {symbol} expired (TTL elapsed).")
                    continue

            # Check if price hit the limit
            if current_price <= limit_price:
                fill_pending_order(order['order_id'], current_price)
                log.info(f"Limit order {order['order_id']} filled: {symbol} at ${current_price:.4f} "
                         f"(limit was ${limit_price:.4f})")

                # Place OCO bracket if live trading
                if _is_live_trading() and strategy != 'auto':
                    from src.execution.binance_trader import _place_oco_with_retry
                    sl_pct = order.get('dynamic_sl_pct')
                    tp_pct = order.get('dynamic_tp_pct')
                    _place_oco_with_retry(
                        symbol if symbol.endswith("USDT") else f"{symbol}USDT",
                        current_price, order.get('quantity', 0),
                        sl_pct=sl_pct, tp_pct=tp_pct)

                await send_telegram_alert({
                    'signal': 'BUY', 'symbol': symbol,
                    'current_price': current_price,
                    'reason': f"Limit order filled at ${current_price:.4f} "
                              f"(pullback from ${limit_price / (1 - settings.get('limit_orders', {}).get('pullback_pct', 0.005)):.4f})",
                })


async def run_stock_cycle(settings, news_per_symbol=None, news_config=None,
                          gemini_assessments=None, signal_mode="scoring",
                          sentiment_config=None,
                          macro_multiplier=1.0, suppress_buys=False):
    """
    Executes one cycle of stock trading analysis for all configured stock symbols.
    """
    if news_per_symbol is None:
        news_per_symbol = {}
    if news_config is None:
        news_config = {}
    stock_settings = settings.get('stock_trading', {})
    if not stock_settings.get('enabled', False):
        log.info("Stock trading is disabled. Skipping stock cycle.")
        return {}

    # IPO watchlist promotion
    ipo_cfg = settings.get('ipo_tracking', {})
    if ipo_cfg.get('enabled', False) and ipo_cfg.get('auto_add_to_watchlist', True):
        try:
            from src.collectors.ipo_watchlist_promoter import promote_new_listings
            new_tickers = promote_new_listings(settings)
            if new_tickers:
                log.info(f"[IPO] Promoted {len(new_tickers)} new tickers to watchlist: {new_tickers}")
                for ticker in new_tickers:
                    try:
                        await send_telegram_alert({
                            'signal': 'INFO', 'symbol': ticker, 'current_price': 0,
                            'reason': f'IPO Watchlist Update: Added {ticker} to stock watchlist. Now being tracked for signals.',
                        })
                    except Exception as e:
                        log.debug(f"IPO alert send failed for {ticker}: {e}")
        except Exception as e:
            log.warning(f"[IPO] Watchlist promotion failed: {e}")

    watch_list = stock_settings.get('watch_list', [])

    # Merge chat watchlist additions (stock)
    try:
        from src.database import get_active_watchlist
        chat_watchlist = get_active_watchlist(asset_type='stock')
        for item in chat_watchlist:
            sym = item['symbol']
            if sym not in watch_list:
                watch_list.append(sym)
                log.debug(f"Watchlist: added stock {sym} from chat")
    except Exception as e:
        log.warning(f"Failed to load stock chat watchlist: {e}")

    if not watch_list:
        log.info("Stock watch list is empty. Skipping stock cycle.")
        return {}

    broker = stock_settings.get('broker', 'paper_only')
    use_alpaca_data = broker == 'alpaca'

    if not _is_market_open():
        log.info("NYSE is closed. Skipping stock cycle.")
        return {}

    log.info(f"--- Starting stock trading cycle for {len(watch_list)} symbols (broker={broker}) ---")

    # Load stock-specific settings
    sma_period = stock_settings.get('sma_period', settings.get('sma_period', 20))
    rsi_period = stock_settings.get('rsi_period', settings.get('rsi_period', 14))
    rsi_overbought = stock_settings.get('rsi_overbought_threshold', settings.get('rsi_overbought_threshold', 70))
    rsi_oversold = stock_settings.get('rsi_oversold_threshold', settings.get('rsi_oversold_threshold', 30))
    pe_buy = stock_settings.get('pe_ratio_buy_threshold', 25)
    pe_sell = stock_settings.get('pe_ratio_sell_threshold', 40)
    earnings_sell = stock_settings.get('earnings_growth_sell_threshold', -10)
    vol_multiplier = stock_settings.get('volume_spike_multiplier', 1.5)
    signal_threshold = settings.get('signal_threshold', 3)
    stoploss_cooldown_hours = settings.get('stoploss_cooldown_hours', 6)
    signal_cooldown_hours = settings.get('signal_cooldown_hours', 4)

    paper_trading = settings.get('paper_trading', True)
    is_live = _is_live_trading()
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    max_concurrent_positions = stock_settings.get('max_concurrent_positions', settings.get('max_concurrent_positions', 3))
    paper_trading_initial_capital = stock_settings.get('paper_trading_initial_capital', settings.get('paper_trading_initial_capital', 10000.0))
    trailing_stop_enabled = settings.get('trailing_stop_enabled', True)
    trailing_stop_activation = settings.get('trailing_stop_activation', 0.02)
    trailing_stop_distance = settings.get('trailing_stop_distance', 0.015)

    risk_cfg = dict(
        stop_loss_pct=stop_loss_percentage,
        take_profit_pct=take_profit_percentage,
        trailing_stop_enabled=trailing_stop_enabled,
        trailing_stop_activation=trailing_stop_activation,
        trailing_stop_distance=trailing_stop_distance,
        stoploss_cooldown_hours=stoploss_cooldown_hours,
    )

    # Batch-fetch stock prices and daily data (heavy HTTP — run off event loop)
    stock_batch_prices = await asyncio.to_thread(get_batch_stock_prices, watch_list) if not use_alpaca_data else {}
    stock_batch_daily = await asyncio.to_thread(get_batch_daily_prices, watch_list) if not use_alpaca_data else {}

    # Cache open stock positions once per cycle
    _cached_stock_positions = await asyncio.to_thread(get_open_positions, asset_type='stock', trading_strategy='manual') if (paper_trading or is_live) else []
    _cached_alpaca_positions = await asyncio.to_thread(get_stock_positions) if broker == 'alpaca' else []

    strategy_configs = get_strategy_configs(settings)
    _cached_strategy_stock_positions: dict[str, list] = {}
    for strat_name, strat_cfg in strategy_configs.items():
        if strat_cfg.get('enabled', False):
            _cached_strategy_stock_positions[strat_name] = await asyncio.to_thread(
                get_open_positions, asset_type='stock', trading_strategy=strat_name)

    # Stock circuit breaker check — once per cycle
    live_config = settings.get('live_trading', {})
    stock_cb_tripped = False
    if is_live or paper_trading:
        stock_cb_balance = (await asyncio.to_thread(get_account_balance, asset_type='stock')).get('total_usd', paper_trading_initial_capital)
        await asyncio.to_thread(update_session_peak, stock_cb_balance, 'stock')
        stock_cb_daily_pnl = await asyncio.to_thread(get_daily_pnl, asset_type='stock')
        # Include stock unrealized PnL in circuit breaker check
        stock_prices_dict = {sym: stock_batch_prices[sym]['price']
                             for sym in stock_batch_prices
                             if stock_batch_prices.get(sym, {}).get('price')}
        stock_cb_unrealized = await asyncio.to_thread(get_unrealized_pnl, stock_prices_dict, 'stock')
        stock_cb_effective_pnl = stock_cb_daily_pnl + stock_cb_unrealized
        stock_cb_recent = await asyncio.to_thread(
            get_recent_closed_trades,
            limit=live_config.get('max_consecutive_losses', 3), asset_type='stock')
        stock_cb_tripped, stock_cb_reason = await asyncio.to_thread(
            check_circuit_breaker,
            stock_cb_balance, stock_cb_effective_pnl, stock_cb_recent, asset_type='stock',
            current_prices=stock_prices_dict)
        if stock_cb_tripped:
            log.warning(f"Circuit breaker active for stocks: {stock_cb_reason}")
            await send_telegram_alert({
                "signal": "CIRCUIT_BREAKER", "symbol": "ALL_STOCKS",
                "reason": f"Circuit breaker (stock): {stock_cb_reason}. Skipping all stock trading this cycle.",
                "asset_type": "stock",
            })

    for symbol in watch_list:
        signal = None
        log.info(f"--- Processing stock: {symbol} ---")

        # Use batch price first, fall back to per-symbol call
        if use_alpaca_data:
            from src.collectors.alpaca_data import get_stock_price_alpaca
            price_data = await asyncio.to_thread(get_stock_price_alpaca, symbol)
        elif symbol in stock_batch_prices:
            price_data = stock_batch_prices[symbol]
        else:
            price_data = await asyncio.to_thread(get_stock_price, symbol)
        if not price_data or not price_data.get('price'):
            log.warning(f"Could not fetch current price for stock {symbol}. Skipping.")
            continue

        current_price = price_data['price']
        log.info(f"Current stock price for {symbol}: ${current_price:,.2f}")

        # --- Position Monitoring with Trailing Stop ---
        if paper_trading or is_live:
            trading_mode_label = 'PAPER TRADE' if paper_trading else _get_trading_mode().upper()
            for position in _cached_stock_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    result = await monitor_position(
                        position, current_price, **risk_cfg,
                        asset_type='stock', mode_label=trading_mode_label)

                    if result != 'none':
                        _cached_stock_positions = await asyncio.to_thread(
                            get_open_positions, asset_type='stock', trading_strategy='manual')

                    # Run position analyst only if position wasn't closed
                    if result == 'none':
                        await run_position_analyst(
                            position, current_price,
                            {'current_price': current_price, 'sma': None, 'rsi': None},
                            settings, news_per_symbol,
                            trailing_stop_activation=trailing_stop_activation,
                            asset_type='stock')

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new stock signal generation.")
            continue

        # Skip signal generation for stocks with open positions
        if (paper_trading or is_live) and any(
            p['symbol'] == symbol and p['status'] == 'OPEN'
            for p in _cached_stock_positions
        ):
            log.debug(f"Skipping signal generation for stock {symbol}: position open.")
            continue
        if broker == 'alpaca' and any(
            p['symbol'] == symbol for p in _cached_alpaca_positions
        ):
            log.debug(f"Skipping signal generation for stock {symbol}: Alpaca position open.")
            continue

        # Fetch daily prices for technical analysis
        if use_alpaca_data:
            from src.collectors.alpaca_data import get_daily_prices_alpaca
            daily_data = await asyncio.to_thread(get_daily_prices_alpaca, symbol)
        elif symbol in stock_batch_daily:
            daily_data = stock_batch_daily[symbol]
        else:
            daily_data = await asyncio.to_thread(get_daily_prices, symbol)
        if not daily_data or not daily_data.get('prices'):
            log.warning(f"Could not fetch daily prices for {symbol}. Skipping analysis.")
            continue

        prices = daily_data['prices']
        volumes = daily_data.get('volumes', [])

        sma_value = calculate_sma(prices, period=sma_period)
        rsi_value = calculate_rsi(prices, period=rsi_period)

        market_data = {'current_price': current_price, 'sma': sma_value, 'rsi': rsi_value}

        volume_data = {}
        if volumes:
            current_volume = volumes[-1] if volumes else None
            avg_volume = sum(volumes) / len(volumes) if volumes else None
            volume_data = {
                'current_volume': current_volume,
                'avg_volume': avg_volume,
                'price_change_percent': price_data.get('change_percent', 0)
            }

        is_international = '.' in symbol and not symbol.startswith('BRK')
        fundamental_data = (await asyncio.to_thread(get_company_overview, symbol)) if not is_international else {}

        stock_news_data = None
        ga = gemini_assessments.get('symbol_assessments', {}).get(symbol) if gemini_assessments else None

        if ga:
            stock_news_data = {
                'gemini_assessment': ga,
            }

        stock_sentiment_config = dict(sentiment_config or {})
        stock_sentiment_config['pe_buy_veto_threshold'] = pe_sell

        # Inject sector conviction for stock
        from src.analysis.sector_limits import get_symbol_group
        from src.analysis.sector_review import get_sector_conviction
        _sym_group = get_symbol_group(symbol)
        if _sym_group:
            _conviction = get_sector_conviction(_sym_group)
            if _conviction != 0.0:
                stock_sentiment_config['sector_conviction'] = _conviction

        signal = generate_stock_signal(
            symbol=symbol,
            market_data=market_data,
            volume_data=volume_data,
            fundamental_data=fundamental_data,
            rsi_overbought_threshold=rsi_overbought,
            rsi_oversold_threshold=rsi_oversold,
            pe_ratio_buy_threshold=pe_buy,
            pe_ratio_sell_threshold=pe_sell,
            earnings_growth_sell_threshold=earnings_sell,
            volume_spike_multiplier=vol_multiplier,
            news_sentiment_data=stock_news_data,
            historical_prices=prices,
            signal_threshold=signal_threshold,
            signal_mode=signal_mode,
            sentiment_config=stock_sentiment_config,
        )
        signal['asset_type'] = 'stock'
        log.info(f"Generated Stock Signal for {symbol}: {signal}")

        # Enrich signal with Gemini metadata for decision tracking +
        # post-order attribution linkage.
        if ga and signal.get('signal') != 'HOLD':
            signal['gemini_confidence'] = ga.get('confidence')
            signal['gemini_direction'] = ga.get('direction')
            signal['catalyst_type'] = ga.get('catalyst_type')
            signal['catalyst_freshness'] = ga.get('catalyst_freshness')

        await save_signal(signal)

        # --- Trade Execution (broker-aware, unified pipeline) ---
        if stock_cb_tripped:
            log.info(f"Skipping trade execution for stock {symbol}: circuit breaker active.")
            signal['signal'] = 'HOLD'

        stock_trade_stats = await get_trade_history_stats()
        stock_kelly = stock_trade_stats.get('kelly_fraction', 0.0)
        stock_risk_pct = stock_kelly if (stock_kelly > 0 and stock_trade_stats.get('total_trades', 0) >= 10) else trade_risk_percentage

        # Preserve original signal before manual path can mutate it
        original_stock_signal = dict(signal) if signal else None

        if broker == 'alpaca':
            pdt_status = await asyncio.to_thread(_check_pdt_rule)
            balance = await asyncio.to_thread(get_stock_balance)
            buying_power = balance.get('buying_power', 0)
            await process_trade_signal(
                symbol, signal, current_price, _cached_alpaca_positions, buying_power,
                stock_risk_pct, signal_cooldown_hours, max_concurrent_positions,
                suppress_buys, macro_multiplier,
                asset_type='stock', broker='alpaca', pdt_status=pdt_status,
                current_prices=stock_prices_dict)
        else:
            current_balance = (await asyncio.to_thread(get_account_balance, asset_type='stock')).get('total_usd', paper_trading_initial_capital)
            await process_trade_signal(
                symbol, signal, current_price, _cached_stock_positions, current_balance,
                stock_risk_pct, signal_cooldown_hours, max_concurrent_positions,
                suppress_buys, macro_multiplier, asset_type='stock',
                current_prices=stock_prices_dict)

        # --- Strategy Bots: Stock Position Monitoring + Signal Execution ---
        for strat_name, strat_cfg in strategy_configs.items():
            if not strat_cfg.get('enabled', False):
                continue
            strat_stock_positions = _cached_strategy_stock_positions.get(strat_name, [])
            strat_label = strat_name.upper()

            # Position Monitoring
            for position in strat_stock_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    result = await monitor_position(
                        position, current_price, **risk_cfg,
                        asset_type='stock', trading_strategy=strat_name, mode_label=strat_label)
                    if result != 'none':
                        _cached_strategy_stock_positions[strat_name] = await asyncio.to_thread(
                            get_open_positions, asset_type='stock', trading_strategy=strat_name)
                        strat_stock_positions = _cached_strategy_stock_positions[strat_name]
                    else:
                        await run_position_analyst(
                            position, current_price,
                            {'current_price': current_price, 'sma': None, 'rsi': None},
                            settings, news_per_symbol,
                            trailing_stop_activation=trailing_stop_activation,
                            asset_type='stock', trading_strategy=strat_name)

            # Signal Execution
            strat_regime = strat_cfg.get('regime_behavior', {})
            ignore_risk_off = strat_regime.get('ignore_risk_off', False)
            max_vix = strat_regime.get('max_vix_for_risk_off_bypass')
            if ignore_risk_off and max_vix is not None:
                try:
                    from src.analysis.macro_regime import get_macro_regime
                    regime_data = get_macro_regime()
                    vix_data = regime_data.get('indicators', {}).get('vix')
                    current_vix = vix_data.get('current') if isinstance(vix_data, dict) else None
                    if current_vix is not None and current_vix >= max_vix:
                        ignore_risk_off = False
                except Exception:
                    pass
            strat_suppress = suppress_buys and not ignore_risk_off

            if not stock_cb_tripped and bot_is_running.is_set() and original_stock_signal is not None:
                strat_open = [p for p in strat_stock_positions if p['status'] == 'OPEN']
                strat_max = strat_cfg.get('max_concurrent_positions', max_concurrent_positions)
                strat_balance = await asyncio.to_thread(
                    get_account_balance, asset_type='stock', trading_strategy=strat_name)
                strat_available = strat_balance.get('USDT', 0)

                strat_signal = dict(original_stock_signal)
                # Longterm strategy: only BUY thesis stocks
                if strat_name == 'longterm' and strat_signal.get('signal') == 'BUY':
                    from src.analysis.thesis_generator import get_thesis_symbols
                    if symbol not in get_thesis_symbols():
                        continue

                # Apply strategy-specific weighting to signal strength
                stock_ga = gemini_assessments.get('symbol_assessments', {}).get(symbol) if gemini_assessments else None
                if stock_ga and strat_signal.get('signal') in ('BUY', 'SELL'):
                    base_str = strat_signal.get('signal_strength', 0)
                    eff_str = compute_effective_strength(base_str,
                        stock_ga, strat_cfg.get('weights', {}))
                    strat_signal['signal_strength'] = eff_str
                    try:
                        from src.database import save_strategy_score
                        await asyncio.to_thread(
                            save_strategy_score, symbol, strat_name,
                            strat_signal['signal'], base_str, eff_str, stock_ga)
                    except Exception:
                        pass
                await process_trade_signal(
                    symbol, strat_signal, current_price, strat_open, strat_available,
                    trade_risk_percentage, signal_cooldown_hours, strat_max,
                    strat_suppress, macro_multiplier,
                    asset_type='stock', trading_strategy=strat_name, label=strat_label, is_auto=True,
                    current_prices=stock_prices_dict,
                    strategy_config=strat_cfg)

    log.info("--- Stock trading cycle complete ---")

    # Return stock prices for dashboard enrichment
    return {sym: stock_batch_prices[sym]['price']
            for sym in stock_batch_prices
            if stock_batch_prices.get(sym, {}).get('price')}
