#!/usr/bin/env python3
# --- Main Application File ---
# This script orchestrates the entire bot's workflow.
# Force redeploy 2025-11-02_v2
# Force redeploy 2025-11-02
# Force redeploy 2025-10-16

import argparse
import asyncio
import os

import pandas as pd
import uvicorn
from fastapi import FastAPI, Request
from telegram import Update

from src.analysis.gemini_news_analyzer import (
    analyze_news_impact, analyze_news_with_search, analyze_position_health,
    analyze_position_investment,
)
from src.analysis.news_velocity import compute_news_velocity
from src.analysis.signal_engine import generate_signal
from src.analysis.stock_signal_engine import generate_stock_signal
from src.analysis.technical_indicators import calculate_rsi, calculate_sma
from src.collectors.alpha_vantage_data import (get_company_overview,
                                               get_daily_prices,
                                               get_stock_price,
                                               get_batch_stock_prices,
                                               get_batch_daily_prices)
from src.collectors.binance_data import get_current_price, get_all_prices
from src.collectors.news_data import collect_news_sentiment
from src.config import app_config
from src.analysis.macro_regime import get_macro_regime, clear_regime_cache
from src.analysis.sector_limits import check_sector_limit
from src.analysis.event_calendar import (
    check_event_gate, get_event_warnings_for_positions,
)
from src.database import (get_historical_prices, get_recent_articles,
                          get_trade_history_stats, get_trade_summary,
                          initialize_database,
                          load_trailing_stop_peaks, save_signal,
                          save_trailing_stop_peak,
                          save_stoploss_cooldown, load_stoploss_cooldowns,
                          clear_stoploss_cooldown,
                          get_position_additions,
                          save_macro_regime)
from src.execution.binance_trader import (get_account_balance,
                                          get_open_positions, place_order,
                                          add_to_position,
                                          _is_live_trading, _get_trading_mode)
from src.execution.circuit_breaker import (check_circuit_breaker, get_daily_pnl,
                                           get_recent_closed_trades,
                                           get_unrealized_pnl)
from src.execution.stock_trader import (
    place_stock_order, get_stock_positions, get_stock_balance,
    _is_market_open, _check_pdt_rule,
)
from src.logger import log
from src.notify.telegram_bot import (send_news_alert, send_position_health_alert,
                                     send_telegram_alert, start_bot,
                                     send_signal_for_confirmation,
                                     is_confirmation_required,
                                     register_execute_callback,
                                     cleanup_expired_signals,
                                     send_auto_bot_summary)
from src.state import bot_is_running

# Initialize the database at the start of the application
try:
    initialize_database()
except Exception as e:
    log.error(f"Failed to initialize database: {e}", exc_info=True)
    log.warning("Continuing startup — database may be unavailable.")

# --- FastAPI App Initialization ---
app = FastAPI()
application = None
_background_tasks = []

# --- Trailing Stop-Loss State ---
# Tracks the highest price seen since each position was opened.
# Key: order_id, Value: highest price observed
_trailing_stop_peaks = {}

# --- Stop-Loss Cooldown State ---
# Prevents re-entry into a symbol for N hours after a stop-loss exit.
# Key: symbol, Value: timestamp when cooldown expires
_stoploss_cooldowns = {}

# --- Position Analyst Cooldown ---
# Prevents excessive Gemini calls for position analysis.
# Key: order_id, Value: datetime of last analyst check
_analyst_last_run = {}

# --- Auto-Trading Shadow Bot State ---
# Separate state dicts so auto-bot doesn't interfere with manual bot.
_auto_trailing_stop_peaks = {}
_auto_stoploss_cooldowns = {}
_auto_analyst_last_run = {}


def _update_trailing_stop(order_id: str, current_price: float) -> float:
    """Updates and returns the peak price for a position (used for trailing stop)."""
    prev_peak = _trailing_stop_peaks.get(order_id, current_price)
    new_peak = max(prev_peak, current_price)
    if new_peak > prev_peak:
        _trailing_stop_peaks[order_id] = new_peak
        try:
            save_trailing_stop_peak(order_id, new_peak)
        except Exception as e:
            log.warning(f"Failed to persist trailing stop peak for {order_id}: {e}")
    elif order_id not in _trailing_stop_peaks:
        _trailing_stop_peaks[order_id] = new_peak
    return new_peak


def _clear_trailing_stop(order_id: str):
    """Removes tracking data for a closed position."""
    _trailing_stop_peaks.pop(order_id, None)


def _auto_update_trailing_stop(order_id: str, current_price: float) -> float:
    """Updates and returns the peak price for an auto-bot position (not persisted to DB)."""
    prev_peak = _auto_trailing_stop_peaks.get(order_id, current_price)
    new_peak = max(prev_peak, current_price)
    _auto_trailing_stop_peaks[order_id] = new_peak
    return new_peak


def _auto_clear_trailing_stop(order_id: str):
    """Removes auto-bot tracking data for a closed position."""
    _auto_trailing_stop_peaks.pop(order_id, None)


async def execute_confirmed_signal(signal: dict) -> dict:
    """Executes a trade after user confirmation via Telegram.
    Called by the telegram_bot callback handler when user taps Approve."""
    signal_type = signal.get('signal')
    symbol = signal.get('symbol')
    current_price = signal.get('current_price', 0)
    asset_type = signal.get('asset_type', 'crypto')
    quantity = signal.get('quantity', 0)
    position = signal.get('position')  # for SELL signals
    order_result = None

    trading_mode = _get_trading_mode()
    log.info(f"Executing confirmed {signal_type} for {symbol} ({asset_type}, {trading_mode})")

    if asset_type == 'stock':
        settings = app_config.get('settings', {})
        stock_settings = settings.get('stock_trading', {})
        broker = stock_settings.get('broker', 'paper_only')

        if broker == 'alpaca':
            order_result = place_stock_order(symbol, signal_type, quantity, current_price)
        else:
            if signal_type == "BUY":
                order_result = place_order(symbol, "BUY", quantity, current_price, asset_type='stock')
            elif signal_type == "SELL" and position:
                order_result = place_order(symbol, "SELL", quantity, current_price,
                                           existing_order_id=position.get('order_id'),
                                           asset_type='stock')
                _clear_trailing_stop(position.get('order_id', ''))
                _analyst_last_run.pop(position.get('order_id', ''), None)
            elif signal_type == "INCREASE" and position:
                order_result = add_to_position(
                    position.get('order_id'), symbol, quantity, current_price,
                    reason=signal.get('reason', ''), asset_type=asset_type,
                )
    else:
        # Crypto
        if signal_type == "BUY":
            order_result = place_order(symbol, "BUY", quantity, current_price)
        elif signal_type == "SELL" and position:
            order_result = place_order(symbol, "SELL", quantity, current_price,
                                       existing_order_id=position.get('order_id'))
            _clear_trailing_stop(position.get('order_id', ''))
            _analyst_last_run.pop(position.get('order_id', ''), None)
        elif signal_type == "INCREASE" and position:
            order_result = add_to_position(
                position.get('order_id'), symbol, quantity, current_price,
                reason=signal.get('reason', ''), asset_type=asset_type,
            )

    return order_result or {}


async def run_bot_cycle():
    """
    Executes one full cycle of the bot's logic.
    """
    log.info("--- Starting new bot cycle ---")
    settings = app_config.get('settings', {})

    # Load all settings
    watch_list = settings.get('watch_list', ['BTC'])  # Default to BTC if not configured
    sma_period = settings.get('sma_period', 20)
    rsi_period = settings.get('rsi_period', 14)
    rsi_overbought_threshold = settings.get('rsi_overbought_threshold', 70)
    rsi_oversold_threshold = settings.get('rsi_oversold_threshold', 30)
    stoploss_cooldown_hours = settings.get('stoploss_cooldown_hours', 6)

    # Signal mode and sentiment config
    signal_mode = settings.get('signal_mode', 'scoring')
    sentiment_signal_cfg = settings.get('sentiment_signal', {})
    sentiment_config = {
        'min_gemini_confidence': sentiment_signal_cfg.get('min_gemini_confidence', 0.7),
        'min_vader_score': sentiment_signal_cfg.get('min_vader_score', 0.3),
        'rsi_buy_veto_threshold': sentiment_signal_cfg.get('rsi_buy_veto_threshold', 75),
        'rsi_sell_veto_threshold': sentiment_signal_cfg.get('rsi_sell_veto_threshold', 25),
    }

    # Paper trading and risk management settings
    paper_trading = settings.get('paper_trading', True)
    paper_trading_initial_capital = settings.get('paper_trading_initial_capital', 10000.0)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    max_concurrent_positions = settings.get('max_concurrent_positions', 3)
    trailing_stop_enabled = settings.get('trailing_stop_enabled', True)
    trailing_stop_activation = settings.get('trailing_stop_activation', 0.02)  # activate after 2% gain
    trailing_stop_distance = settings.get('trailing_stop_distance', 0.015)     # trail 1.5% from peak

    # --- Dynamic Position Sizing (Kelly Criterion) ---
    trade_stats = get_trade_history_stats()
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
    macro_regime_result = get_macro_regime()
    macro_multiplier = macro_regime_result['position_size_multiplier']
    suppress_buys = macro_regime_result.get('suppress_buys', False)
    log.info(f"Macro regime: {macro_regime_result['regime']} "
             f"(mult={macro_multiplier}, suppress_buys={suppress_buys})")

    # Save regime to DB and alert on change
    try:
        save_macro_regime(macro_regime_result)
    except Exception as e:
        log.warning(f"Failed to save macro regime: {e}")

    # 1. Collect news data
    log.info("Fetching data from all sources...")

    # Collect news for all symbols (crypto + stock combined)
    stock_settings = settings.get('stock_trading', {})
    stock_watch_list = stock_settings.get('watch_list', []) if stock_settings.get('enabled', False) else []
    all_symbols = list(set(watch_list + stock_watch_list))

    news_config = settings.get('news_analysis', {})
    gemini_assessments = None
    news_per_symbol = {}
    use_grounded_search = news_config.get('use_grounded_search', False)

    # Build current prices dict for all crypto symbols via batch API call
    current_prices_dict = {}
    all_binance_prices = get_all_prices()  # 1 API call for all ~2000 pairs
    for sym in all_symbols:
        api_sym = sym if "USDT" in sym else f"{sym}USDT"
        price = all_binance_prices.get(api_sym)
        if price:
            current_prices_dict[sym] = price

    # --- Optional: Gemini with Google Search grounding (expensive, $0.035/call) ---
    if news_config.get('enabled', False) and use_grounded_search:
        cache_ttl = news_config.get('cache_ttl_minutes', 30)
        gemini_assessments = analyze_news_with_search(all_symbols, current_prices_dict,
                                                       cache_ttl_minutes=cache_ttl)

    # --- Primary path: RSS + web scraping + VADER + plain Gemini (cheap) ---
    if gemini_assessments is None:
        if use_grounded_search:
            log.info("Grounded search unavailable — falling back to RSS+scraping pipeline.")
        news_result = collect_news_sentiment(all_symbols)
        news_per_symbol = news_result.get('per_symbol', {})
        triggered_symbols = news_result.get('triggered_symbols', [])

        # Send all symbols with news to Gemini for analysis (not just triggered ones)
        symbols_with_news = [sym for sym in all_symbols if sym in news_per_symbol]
        if symbols_with_news and news_config.get('enabled', False):
            headlines_by_symbol = {}
            current_prices_for_news = {}
            for sym in symbols_with_news:
                sym_data = news_per_symbol.get(sym, {})
                headlines_by_symbol[sym] = sym_data.get('headlines', [])
                current_prices_for_news[sym] = current_prices_dict.get(sym, sym_data.get('current_price', 0))

            # Enrich Gemini prompt with archived articles from DB
            archived_articles_by_symbol = {}
            for sym in symbols_with_news:
                try:
                    archived = get_recent_articles(sym, hours=24)
                    if archived:
                        archived_articles_by_symbol[sym] = archived
                except Exception as e:
                    log.warning(f"Failed to fetch archived articles for {sym}: {e}")

            # Build news stats per symbol for Gemini context
            news_stats_by_symbol = {}
            for sym in symbols_with_news:
                sym_data = news_per_symbol.get(sym, {})
                if sym_data:
                    scores = sym_data.get('sentiment_scores', [])
                    volume = sym_data.get('news_volume', len(sym_data.get('headlines', [])))
                    positive = sum(1 for s in scores if s > 0.05) if scores else 0
                    negative = sum(1 for s in scores if s < -0.05) if scores else 0
                    total = max(len(scores), 1)
                    # Sentiment volatility = std dev of scores
                    if len(scores) >= 2:
                        mean_s = sum(scores) / len(scores)
                        variance = sum((s - mean_s) ** 2 for s in scores) / len(scores)
                        sent_vol = variance ** 0.5
                    else:
                        sent_vol = 0.0
                    news_stats_by_symbol[sym] = {
                        'news_volume': volume,
                        'positive_ratio': positive / total,
                        'negative_ratio': negative / total,
                        'sentiment_volatility': sent_vol,
                    }

            gemini_assessments = analyze_news_impact(
                headlines_by_symbol, current_prices_for_news,
                archived_articles_by_symbol=archived_articles_by_symbol or None,
                news_stats_by_symbol=news_stats_by_symbol or None,
            )
            if triggered_symbols:
                await send_news_alert(triggered_symbols, news_per_symbol, gemini_assessments=gemini_assessments)

    # Cache open positions once per cycle (avoid repeated DB queries)
    is_live = _is_live_trading()
    _cached_crypto_positions = get_open_positions(trading_strategy='manual') if (paper_trading or is_live) else []

    # Auto-trading shadow bot setup
    auto_cfg = settings.get('auto_trading', {})
    auto_enabled = auto_cfg.get('enabled', False)
    _cached_auto_positions = get_open_positions(trading_strategy='auto') if auto_enabled else []

    # Process each symbol in the watch list
    for symbol in watch_list:
        signal = None  # reset per symbol; set below if signal generation runs
        log.info(f"--- Processing symbol: {symbol} ---")

        # Use batch price from all_binance_prices, fall back to individual call
        api_symbol = symbol if "USDT" in symbol else f"{symbol}USDT"
        current_price = all_binance_prices.get(api_symbol)
        if current_price:
            from src.collectors.binance_data import save_price_data
            save_price_data({'symbol': api_symbol, 'price': current_price})
        else:
            price_data = get_current_price(api_symbol)
            if not price_data or not price_data.get('price'):
                log.warning(f"Could not fetch current price for {api_symbol}. Skipping analysis.")
                continue
            current_price = float(price_data.get('price'))

        if not current_price:
            log.warning(f"Could not fetch current price for {api_symbol}. Skipping analysis.")
            continue
        log.info(f"Current price for {symbol}: ${current_price:,.2f}")

        # --- Position Monitoring: SL/TP/Trailing Stop ---
        # For live trading, OCO brackets handle SL/TP server-side, but we still
        # monitor and log. For paper trading, this is the primary protection.
        trading_mode = _get_trading_mode()
        if paper_trading or is_live:
            for position in _cached_crypto_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    entry_price = position['entry_price']
                    pnl_percentage = (current_price - entry_price) / entry_price
                    order_id = position['order_id']
                    mode_label = trading_mode.upper()

                    # Update trailing stop peak tracker
                    peak_price = _update_trailing_stop(order_id, current_price)
                    drawdown_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0

                    # Trailing stop: activates once position is up by trailing_stop_activation,
                    # then closes if price drops trailing_stop_distance from the peak
                    if trailing_stop_enabled and pnl_percentage >= trailing_stop_activation:
                        if drawdown_from_peak >= trailing_stop_distance:
                            locked_gain = (peak_price - entry_price) / entry_price
                            log.info(f"[{mode_label}] Trailing stop triggered for {symbol}. "
                                     f"Peak: ${peak_price:,.2f}, Current: ${current_price:,.2f}")
                            place_order(symbol, "SELL", position['quantity'], current_price,
                                        existing_order_id=order_id)
                            _clear_trailing_stop(order_id)
                            _analyst_last_run.pop(order_id, None)
                            await send_telegram_alert({"signal": "SELL", "symbol": symbol,
                                                       "current_price": current_price,
                                                       "reason": f"Trailing stop hit (peak ${peak_price:,.2f}, "
                                                                 f"locked ~{locked_gain * 100:.1f}% gain)."})
                            continue

                    # Fixed stop-loss (always active as a floor)
                    if pnl_percentage <= -stop_loss_percentage:
                        log.info(f"[{mode_label}] Stop-loss hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id)
                        _clear_trailing_stop(order_id)
                        _analyst_last_run.pop(order_id, None)
                        if stoploss_cooldown_hours > 0:
                            from datetime import datetime, timedelta, timezone
                            _stoploss_cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(hours=stoploss_cooldown_hours)
                            save_stoploss_cooldown(symbol, _stoploss_cooldowns[symbol])
                            log.info(f"[{symbol}] Stop-loss cooldown set for {stoploss_cooldown_hours}h")
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol, "current_price": current_price,
                                                   "reason": f"Stop-loss hit ({stop_loss_percentage * 100:.2f}% loss)."})

                    # Take profit (as ultimate cap)
                    elif pnl_percentage >= take_profit_percentage:
                        log.info(f"[{mode_label}] Take-profit hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id)
                        _clear_trailing_stop(order_id)
                        _analyst_last_run.pop(order_id, None)
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol, "current_price": current_price,
                                                   "reason": f"Take-profit hit ({take_profit_percentage * 100:.2f}% gain)."})

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new signal generation and trading.")
            continue

        # 2. Compute technical indicators (SMA/RSI)
        log.info(f"Analyzing data for {symbol}...")

        price_limit = max(sma_period, rsi_period) + 1
        historical_prices = get_historical_prices(symbol, limit=price_limit)

        market_price_data = {'current_price': current_price, 'sma': None, 'rsi': None}
        if len(historical_prices) >= sma_period:
            price_series = pd.Series(historical_prices)
            market_price_data['sma'] = price_series.rolling(window=sma_period).mean().iloc[-1]
        market_price_data['rsi'] = calculate_rsi(historical_prices, period=rsi_period)
        log.info(f"Technical Indicators for {symbol}: SMA={market_price_data['sma']}, RSI={market_price_data['rsi']}")

        # --- Position Analyst (tri-state: HOLD / INCREASE / SELL) ---
        # Replaces binary health monitor when position_analyst is enabled.
        # Falls back to old position_monitor when position_analyst is disabled.
        if paper_trading or is_live:
            for position in _cached_crypto_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    entry_price = position['entry_price']
                    pnl_percentage = (current_price - entry_price) / entry_price
                    order_id = position['order_id']
                    mode_label = trading_mode.upper()

                    analyst_cfg = settings.get('position_analyst', {})
                    if analyst_cfg.get('enabled', False):
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        check_interval = analyst_cfg.get('check_interval_minutes', 30)
                        last_check = _analyst_last_run.get(order_id)
                        if last_check and (now - last_check).total_seconds() / 60 < check_interval:
                            log.debug(f"[{symbol}] Skipping analyst — last run {(now - last_check).total_seconds() / 60:.0f}m ago")
                        else:
                          min_age_hours = analyst_cfg.get('min_position_age_hours', 2)
                          entry_ts = position.get('entry_timestamp')
                          if entry_ts:
                            try:
                                if isinstance(entry_ts, str):
                                    entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
                                else:
                                    entry_dt = entry_ts
                                if entry_dt.tzinfo is None:
                                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                                age_hours = (now - entry_dt).total_seconds() / 3600
                                if age_hours >= min_age_hours:
                                    # 1. News velocity (cheap DB query — gates Gemini call)
                                    velocity = compute_news_velocity(symbol)

                                    # 2. Gate: skip Gemini if no news activity
                                    if (velocity['articles_last_4h'] == 0
                                            and not velocity['breaking_detected']
                                            and velocity['sentiment_trend'] == 'stable'):
                                        log.debug(f"[{symbol}] Position analyst: no news activity, default HOLD")
                                        _analyst_last_run[order_id] = now
                                    else:
                                        # 3. Gather full article data + additions history
                                        recent_articles = get_recent_articles(symbol, hours=48, limit=30)
                                        additions = get_position_additions(order_id)

                                        tech_data = {
                                            'rsi': market_price_data['rsi'],
                                            'sma': market_price_data['sma'],
                                            'regime': 'unknown',
                                        }
                                        peak_price = _trailing_stop_peaks.get(order_id)
                                        ts_info = None
                                        if peak_price is not None:
                                            ts_info = {
                                                'peak_price': peak_price,
                                                'trailing_active': pnl_percentage >= trailing_stop_activation,
                                                'pnl_percentage': pnl_percentage,
                                                'activation_threshold': trailing_stop_activation,
                                            }

                                        max_mult = analyst_cfg.get('max_position_multiplier', 3.0)

                                        # 4. Call Gemini analyst
                                        result = analyze_position_investment(
                                            position, current_price, recent_articles, tech_data,
                                            velocity, hours_held=age_hours,
                                            trailing_stop_info=ts_info,
                                            position_additions=additions,
                                            max_position_multiplier=max_mult,
                                        )
                                        _analyst_last_run[order_id] = now

                                        if result:
                                            rec = result.get('recommendation', 'hold')
                                            confidence = result.get('confidence', 0)
                                            reasoning = result.get('reasoning', '')
                                            risk_level = result.get('risk_level', 'green')

                                            if rec == 'increase' and confidence >= analyst_cfg.get('increase_confidence_threshold', 0.75):
                                                # Size the addition based on hint
                                                hint = result.get('increase_sizing_hint', 'small')
                                                hint_fractions = {'small': 0.25, 'medium': 0.50, 'large': 0.75}
                                                fraction = hint_fractions.get(hint, 0.25)
                                                original_value = entry_price * position['quantity']
                                                # Subtract already-added value
                                                total_added = sum(a.get('addition_price', 0) * a.get('addition_quantity', 0) for a in additions)
                                                estimated_original = original_value - total_added if total_added < original_value else original_value
                                                add_value = estimated_original * fraction
                                                add_qty = add_value / current_price if current_price > 0 else 0

                                                # Check position multiplier cap
                                                new_total_value = original_value + add_value
                                                current_mult = new_total_value / estimated_original if estimated_original > 0 else 999
                                                if current_mult > max_mult:
                                                    log.info(f"[{symbol}] INCREASE blocked: would exceed {max_mult}x cap")
                                                else:
                                                    # Balance check
                                                    balance = get_account_balance(asset_type='crypto')
                                                    available = balance.get('USDT', 0)
                                                    if add_value > available:
                                                        log.info(f"[{symbol}] INCREASE blocked: need ${add_value:.2f} but only ${available:.2f} available")
                                                    elif is_confirmation_required("INCREASE"):
                                                        await send_signal_for_confirmation({
                                                            'signal': 'INCREASE', 'symbol': symbol,
                                                            'current_price': current_price,
                                                            'quantity': add_qty,
                                                            'reason': reasoning,
                                                            'asset_type': 'crypto',
                                                            'position': position,
                                                        })
                                                    else:
                                                        add_to_position(order_id, symbol, add_qty, current_price,
                                                                        reason=reasoning, asset_type='crypto')

                                            elif rec == 'sell' and confidence >= analyst_cfg.get('exit_confidence_threshold', 0.8):
                                                if is_confirmation_required("SELL"):
                                                    await send_signal_for_confirmation({
                                                        'signal': 'SELL', 'symbol': symbol,
                                                        'current_price': current_price,
                                                        'quantity': position['quantity'],
                                                        'reason': reasoning,
                                                        'asset_type': 'crypto',
                                                        'position': position,
                                                    })
                                                else:
                                                    place_order(symbol, "SELL", position['quantity'], current_price,
                                                                existing_order_id=order_id)
                                                    _clear_trailing_stop(order_id)
                                                    _analyst_last_run.pop(order_id, None)
                                                    await send_telegram_alert({
                                                        "signal": "SELL", "symbol": symbol,
                                                        "current_price": current_price,
                                                        "reason": f"Position analyst sell ({confidence:.0%}): {reasoning}"
                                                    })

                                            else:
                                                # HOLD — log, optionally alert if risk is elevated
                                                log.info(f"[{symbol}] Position analyst: {rec} (confidence={confidence:.2f}, risk={risk_level})")
                                                if risk_level in ('yellow', 'red'):
                                                    await send_position_health_alert(
                                                        symbol, current_price, pnl_percentage * 100, result, position
                                                    )
                            except Exception as e:
                                log.warning(f"Position analyst error for {symbol}: {e}")

                    # Fallback: old position_monitor when position_analyst is disabled
                    elif settings.get('position_monitor', {}).get('enabled', False):
                        pos_monitor_cfg = settings.get('position_monitor', {})
                        check_interval = pos_monitor_cfg.get('check_interval_minutes', 60)
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        last_check = _analyst_last_run.get(order_id)
                        if last_check and (now - last_check).total_seconds() / 60 < check_interval:
                            pass
                        else:
                          min_age_hours = pos_monitor_cfg.get('min_position_age_hours', 4)
                          entry_ts = position.get('entry_timestamp')
                          if entry_ts:
                            try:
                                if isinstance(entry_ts, str):
                                    entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
                                else:
                                    entry_dt = entry_ts
                                if entry_dt.tzinfo is None:
                                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                                age_hours = (now - entry_dt).total_seconds() / 3600
                                if age_hours >= min_age_hours:
                                    pos_headlines = []
                                    sym_news = news_per_symbol.get(symbol, {})
                                    if sym_news:
                                        pos_headlines.extend(sym_news.get('headlines', [])[:5])
                                    try:
                                        archived = get_recent_articles(symbol, hours=24)
                                        pos_headlines.extend([a.get('title', '') for a in archived[:5]])
                                    except Exception:
                                        pass
                                    tech_data = {
                                        'rsi': market_price_data['rsi'],
                                        'sma': market_price_data['sma'],
                                        'regime': 'unknown',
                                    }
                                    peak_price = _trailing_stop_peaks.get(order_id)
                                    ts_info = None
                                    if peak_price is not None:
                                        ts_info = {
                                            'peak_price': peak_price,
                                            'trailing_active': pnl_percentage >= trailing_stop_activation,
                                            'pnl_percentage': pnl_percentage,
                                            'activation_threshold': trailing_stop_activation,
                                        }
                                    health = analyze_position_health(
                                        position, current_price, pos_headlines, tech_data,
                                        hours_held=age_hours, trailing_stop_info=ts_info,
                                    )
                                    _analyst_last_run[order_id] = now
                                    if health:
                                        exit_threshold = pos_monitor_cfg.get('exit_confidence_threshold', 0.8)
                                        if health.get('recommendation') == 'exit' and health.get('confidence', 0) >= exit_threshold:
                                            await send_position_health_alert(
                                                symbol, current_price, pnl_percentage * 100, health, position
                                            )
                            except Exception as e:
                                log.warning(f"Position monitor error for {symbol}: {e}")

        # Skip signal generation for symbols with open positions —
        # position monitor above handles all exits (SL/TP/trailing/health)
        if (paper_trading or is_live) and any(
            p['symbol'] == symbol and p['status'] == 'OPEN'
            for p in _cached_crypto_positions
        ):
            log.debug(f"Skipping signal generation for {symbol}: position open (managed by position monitor).")
            continue

        # 3. Generate a signal
        log.info(f"Generating signal for {symbol}...")

        # Build per-symbol news sentiment data for the signal engine
        symbol_news_data = None
        ga = gemini_assessments.get('symbol_assessments', {}).get(symbol) if gemini_assessments else None
        sym_news = news_per_symbol.get(symbol)

        if ga or sym_news:
            symbol_news_data = {
                'avg_sentiment_score': sym_news.get('avg_sentiment_score', 0) if sym_news else 0,
                'sentiment_buy_threshold': news_config.get('sentiment_buy_threshold', 0.15),
                'sentiment_sell_threshold': news_config.get('sentiment_sell_threshold', -0.15),
                'min_gemini_confidence': news_config.get('min_gemini_confidence',
                                                         news_config.get('min_claude_confidence', 0.6)),
            }
            if ga:
                symbol_news_data['gemini_assessment'] = ga

        signal = generate_signal(
            symbol=symbol,
            market_data=market_price_data,
            news_sentiment_data=symbol_news_data,
            signal_mode=signal_mode,
            sentiment_config=sentiment_config,
            rsi_overbought_threshold=rsi_overbought_threshold,
            rsi_oversold_threshold=rsi_oversold_threshold,
        )
        log.info(f"Generated Signal for {symbol}: {signal}")
        save_signal(signal)

        # --- 4. Trade Execution (Paper & Live) with Dynamic Sizing ---
        live_config = settings.get('live_trading', {})
        is_live = _is_live_trading()
        can_trade = paper_trading or is_live

        if can_trade:
            # Circuit breaker check (live trading only)
            if is_live:
                cb_balance = get_account_balance().get('USDT', 0)
                cb_daily_pnl = get_daily_pnl()
                cb_unrealized = get_unrealized_pnl(current_prices_dict)
                cb_effective_pnl = cb_daily_pnl + cb_unrealized
                cb_recent_trades = get_recent_closed_trades(limit=live_config.get('max_consecutive_losses', 3))
                cb_tripped, cb_reason = check_circuit_breaker(cb_balance, cb_effective_pnl, cb_recent_trades)
                if cb_tripped:
                    log.warning(f"Circuit breaker active: {cb_reason}")
                    await send_telegram_alert({
                        "signal": "CIRCUIT_BREAKER", "symbol": symbol,
                        "current_price": current_price,
                        "reason": f"Circuit breaker: {cb_reason}"
                    })
                    continue

            log.info(f"Processing signal for {trading_mode} trading...")
            open_positions = _cached_crypto_positions

            # Use live or paper initial capital
            if is_live:
                current_balance = get_account_balance().get('USDT', live_config.get('initial_capital', 100.0))
                active_max_positions = live_config.get('max_concurrent_positions', max_concurrent_positions)
            else:
                current_balance = get_account_balance().get('total_usd', paper_trading_initial_capital)
                active_max_positions = max_concurrent_positions

            # --- Stop-loss cooldown check ---
            if signal['signal'] in ("BUY", "SELL") and symbol in _stoploss_cooldowns:
                from datetime import datetime, timezone
                if datetime.now(timezone.utc) < _stoploss_cooldowns[symbol]:
                    log.info(f"Skipping {signal['signal']} for {symbol}: stop-loss cooldown active.")
                    signal['signal'] = 'HOLD'
                else:
                    del _stoploss_cooldowns[symbol]
                    clear_stoploss_cooldown(symbol)

            if signal['signal'] == "BUY":
                # --- Pre-trade gates: macro regime ---
                if suppress_buys:
                    log.info(f"Skipping BUY for {symbol}: Macro regime RISK_OFF (suppress_buys).")
                    signal['signal'] = 'HOLD'
                elif any(p['symbol'] == symbol and p['status'] == 'OPEN' for p in open_positions):
                    log.info(f"Skipping BUY for {symbol}: Position already open.")
                elif len(open_positions) >= active_max_positions:
                    log.info(
                        f"Skipping BUY for {symbol}: Max concurrent positions ({active_max_positions}) reached.")
                else:
                    # --- Pre-trade gates: sector limit ---
                    sector_allowed, sector_reason = check_sector_limit(symbol, open_positions)
                    if not sector_allowed:
                        log.info(f"Skipping BUY for {symbol}: {sector_reason}")
                    else:
                        # --- Pre-trade gates: event calendar ---
                        event_action, event_mult, event_reason = check_event_gate(
                            symbol, 'BUY', asset_type='crypto')
                        if event_action == 'block':
                            log.info(f"Skipping BUY for {symbol}: {event_reason}")
                            signal['signal'] = 'HOLD'
                        else:
                            # Apply macro + event multipliers to position sizing
                            size_mult = macro_multiplier * (event_mult if event_action == 'reduce' else 1.0)
                            if event_action == 'reduce':
                                log.info(f"Reducing BUY for {symbol}: {event_reason} (mult={event_mult})")
                            capital_to_risk = current_balance * effective_risk_pct * size_mult
                            quantity_to_buy = capital_to_risk / current_price
                            if quantity_to_buy * current_price > current_balance:
                                log.warning(f"Skipping BUY for {symbol}: Insufficient balance.")
                            else:
                                signal['quantity'] = quantity_to_buy
                                signal['asset_type'] = 'crypto'
                                if is_confirmation_required("BUY"):
                                    log.info(f"Sending BUY {symbol} for confirmation (qty={quantity_to_buy:.6f}).")
                                    await send_signal_for_confirmation(signal)
                                else:
                                    log.info(f"Executing {trading_mode} trade: BUY {quantity_to_buy:.6f} {symbol} "
                                             f"(risk={effective_risk_pct:.4f}, size_mult={size_mult:.2f}).")
                                    order_result = place_order(symbol, "BUY", quantity_to_buy, current_price)
                                    if order_result.get('status') == 'FILLED':
                                        signal['order_result'] = order_result
                                    await send_telegram_alert(signal)

            elif signal['signal'] == "SELL":
                position_to_close = next(
                    (p for p in open_positions if p['symbol'] == symbol and p['status'] == 'OPEN'), None)
                if position_to_close:
                    signal['quantity'] = position_to_close['quantity']
                    signal['position'] = position_to_close
                    signal['asset_type'] = 'crypto'
                    if is_confirmation_required("SELL"):
                        log.info(f"Sending SELL {symbol} for confirmation.")
                        await send_signal_for_confirmation(signal)
                    else:
                        log.info(f"Executing {trading_mode} trade: SELL {position_to_close['quantity']:.6f} {symbol}.")
                        order_result = place_order(symbol, "SELL", position_to_close['quantity'], current_price,
                                    existing_order_id=position_to_close['order_id'])
                        _clear_trailing_stop(position_to_close['order_id'])
                        if order_result.get('status') == 'CLOSED':
                            signal['order_result'] = order_result
                        await send_telegram_alert(signal)
                else:
                    log.info(f"Skipping SELL for {symbol}: No open position found.")
            else:  # HOLD
                log.info(f"Signal is HOLD for {symbol}. No trade action taken.")

        # --- Auto-Trading Shadow Bot: Position Monitoring ---
        if auto_enabled:
            for position in _cached_auto_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    entry_price = position['entry_price']
                    pnl_percentage = (current_price - entry_price) / entry_price
                    order_id = position['order_id']

                    peak_price = _auto_update_trailing_stop(order_id, current_price)
                    drawdown_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0

                    # Trailing stop
                    if trailing_stop_enabled and pnl_percentage >= trailing_stop_activation:
                        if drawdown_from_peak >= trailing_stop_distance:
                            log.info(f"[AUTO] Trailing stop triggered for {symbol}. "
                                     f"Peak: ${peak_price:,.2f}, Current: ${current_price:,.2f}")
                            place_order(symbol, "SELL", position['quantity'], current_price,
                                        existing_order_id=order_id, trading_strategy='auto')
                            _auto_clear_trailing_stop(order_id)
                            _auto_analyst_last_run.pop(order_id, None)
                            continue

                    # Fixed stop-loss
                    if pnl_percentage <= -stop_loss_percentage:
                        log.info(f"[AUTO] Stop-loss hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id, trading_strategy='auto')
                        _auto_clear_trailing_stop(order_id)
                        _auto_analyst_last_run.pop(order_id, None)
                        if stoploss_cooldown_hours > 0:
                            from datetime import datetime, timedelta, timezone
                            _auto_stoploss_cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(hours=stoploss_cooldown_hours)

                    # Take profit
                    elif pnl_percentage >= take_profit_percentage:
                        log.info(f"[AUTO] Take-profit hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id, trading_strategy='auto')
                        _auto_clear_trailing_stop(order_id)
                        _auto_analyst_last_run.pop(order_id, None)

        # --- Auto-Trading Shadow Bot: Signal Execution ---
        # `signal` is only set when signal generation ran (i.e., no open manual position for this symbol)
        if auto_enabled and bot_is_running.is_set() and signal is not None:
            auto_open_crypto = [p for p in _cached_auto_positions
                                if p.get('asset_type', 'crypto') == 'crypto' and p['status'] == 'OPEN']
            auto_max = auto_cfg.get('max_concurrent_positions', max_concurrent_positions)

            # Auto-bot cooldown check
            auto_signal_type = signal.get('signal', 'HOLD')
            if auto_signal_type in ("BUY", "SELL") and symbol in _auto_stoploss_cooldowns:
                from datetime import datetime, timezone
                if datetime.now(timezone.utc) < _auto_stoploss_cooldowns[symbol]:
                    auto_signal_type = 'HOLD'
                else:
                    del _auto_stoploss_cooldowns[symbol]

            if auto_signal_type == "BUY":
                if suppress_buys:
                    log.info(f"[AUTO] Skipping BUY for {symbol}: Macro regime RISK_OFF.")
                elif not any(p['symbol'] == symbol and p['status'] == 'OPEN' for p in auto_open_crypto):
                    if len(auto_open_crypto) < auto_max:
                        sector_ok, sector_msg = check_sector_limit(symbol, auto_open_crypto, 'auto')
                        if not sector_ok:
                            log.info(f"[AUTO] Skipping BUY for {symbol}: {sector_msg}")
                        else:
                            ev_action, ev_mult, ev_reason = check_event_gate(
                                symbol, 'BUY', asset_type='crypto')
                            if ev_action == 'block':
                                log.info(f"[AUTO] Skipping BUY for {symbol}: {ev_reason}")
                            else:
                                size_mult = macro_multiplier * (ev_mult if ev_action == 'reduce' else 1.0)
                                auto_balance = get_account_balance(asset_type='crypto', trading_strategy='auto')
                                auto_available = auto_balance.get('USDT', 0)
                                capital_to_risk = auto_available * effective_risk_pct * size_mult
                                qty = capital_to_risk / current_price if current_price > 0 else 0
                                if qty > 0 and qty * current_price <= auto_available:
                                    log.info(f"[AUTO] Executing BUY {qty:.6f} {symbol} (size_mult={size_mult:.2f})")
                                    place_order(symbol, "BUY", qty, current_price, trading_strategy='auto')
                                else:
                                    log.info(f"[AUTO] Insufficient balance for BUY {symbol}")

            elif auto_signal_type == "SELL":
                auto_pos = next((p for p in auto_open_crypto
                                 if p['symbol'] == symbol and p['status'] == 'OPEN'), None)
                if auto_pos:
                    log.info(f"[AUTO] Executing SELL {auto_pos['quantity']:.6f} {symbol}")
                    place_order(symbol, "SELL", auto_pos['quantity'], current_price,
                                existing_order_id=auto_pos['order_id'], trading_strategy='auto')
                    _auto_clear_trailing_stop(auto_pos['order_id'])

    # --- Event warnings for open positions ---
    try:
        all_open = open_positions + (auto_open_crypto if auto_enabled else [])
        event_warnings = get_event_warnings_for_positions(all_open)
        for warn in event_warnings:
            log.info(f"Event warning: {warn['symbol']} — {warn['event_type']} in {warn['hours_until']:.0f}h")
            await send_telegram_alert({
                'signal': 'INFO', 'symbol': warn['symbol'],
                'current_price': 0,
                'reason': f"Upcoming {warn['event_type']} in {warn['hours_until']:.0f}h — "
                          f"consider reducing exposure.",
            })
    except Exception as e:
        log.warning(f"Event warnings failed: {e}")

    # --- Run Stock Trading Cycle ---
    await run_stock_cycle(settings, news_per_symbol=news_per_symbol,
                          news_config=news_config,
                          gemini_assessments=gemini_assessments,
                          signal_mode=signal_mode,
                          sentiment_config=sentiment_config,
                          macro_multiplier=macro_multiplier,
                          suppress_buys=suppress_buys)


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
        return

    # IPO watchlist promotion: auto-add recently listed tickers
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
                            'signal': 'INFO',
                            'symbol': ticker,
                            'current_price': 0,
                            'reason': f'IPO Watchlist Update: Added {ticker} to stock watchlist. Now being tracked for signals.',
                        })
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"[IPO] Watchlist promotion failed: {e}")

    watch_list = stock_settings.get('watch_list', [])
    if not watch_list:
        log.info("Stock watch list is empty. Skipping stock cycle.")
        return

    broker = stock_settings.get('broker', 'paper_only')
    use_alpaca_data = broker == 'alpaca'

    # Skip if market is closed and using Alpaca broker
    if broker == 'alpaca' and not _is_market_open():
        log.info("NYSE is closed. Skipping stock cycle (broker=alpaca).")
        return

    log.info(f"--- Starting stock trading cycle for {len(watch_list)} symbols (broker={broker}) ---")

    # Load stock-specific settings with fallbacks to shared settings
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

    # Shared risk management settings
    paper_trading = settings.get('paper_trading', True)
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    max_concurrent_positions = stock_settings.get('max_concurrent_positions', settings.get('max_concurrent_positions', 3))
    paper_trading_initial_capital = stock_settings.get('paper_trading_initial_capital', settings.get('paper_trading_initial_capital', 10000.0))
    trailing_stop_enabled = settings.get('trailing_stop_enabled', True)
    trailing_stop_activation = settings.get('trailing_stop_activation', 0.02)
    trailing_stop_distance = settings.get('trailing_stop_distance', 0.015)

    # Batch-fetch stock prices and daily data via yfinance (2 calls for all stocks)
    stock_batch_prices = get_batch_stock_prices(watch_list) if not use_alpaca_data else {}
    stock_batch_daily = get_batch_daily_prices(watch_list) if not use_alpaca_data else {}

    # Cache open stock positions once per cycle
    _cached_stock_positions = get_open_positions(asset_type='stock', trading_strategy='manual') if paper_trading else []
    _cached_alpaca_positions = get_stock_positions() if broker == 'alpaca' else []

    # Auto-trading shadow bot for stocks
    auto_cfg = settings.get('auto_trading', {})
    auto_enabled = auto_cfg.get('enabled', False)
    _cached_auto_stock_positions = get_open_positions(asset_type='stock', trading_strategy='auto') if auto_enabled else []

    for symbol in watch_list:
        signal = None  # reset per symbol; set below if signal generation runs
        log.info(f"--- Processing stock: {symbol} ---")

        # Use batch price first, fall back to per-symbol call
        if use_alpaca_data:
            from src.collectors.alpaca_data import get_stock_price_alpaca
            price_data = get_stock_price_alpaca(symbol)
        elif symbol in stock_batch_prices:
            price_data = stock_batch_prices[symbol]
        else:
            price_data = get_stock_price(symbol)
        if not price_data or not price_data.get('price'):
            log.warning(f"Could not fetch current price for stock {symbol}. Skipping.")
            continue

        current_price = price_data['price']
        log.info(f"Current stock price for {symbol}: ${current_price:,.2f}")

        # --- Position Monitoring with Trailing Stop ---
        if paper_trading:
            for position in _cached_stock_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    entry_price = position['entry_price']
                    pnl_percentage = (current_price - entry_price) / entry_price
                    order_id = position['order_id']

                    peak_price = _update_trailing_stop(order_id, current_price)
                    drawdown_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0

                    if trailing_stop_enabled and pnl_percentage >= trailing_stop_activation:
                        if drawdown_from_peak >= trailing_stop_distance:
                            locked_gain = (peak_price - entry_price) / entry_price
                            log.info(f"[PAPER TRADE] Trailing stop triggered for stock {symbol}.")
                            place_order(symbol, "SELL", position['quantity'], current_price,
                                        existing_order_id=order_id, asset_type='stock')
                            _clear_trailing_stop(order_id)
                            _analyst_last_run.pop(order_id, None)
                            await send_telegram_alert({"signal": "SELL", "symbol": symbol,
                                                       "current_price": current_price, "asset_type": "stock",
                                                       "reason": f"Trailing stop hit (peak ${peak_price:,.2f}, "
                                                                 f"locked ~{locked_gain * 100:.1f}% gain)."})
                            continue

                    if pnl_percentage <= -stop_loss_percentage:
                        log.info(f"[PAPER TRADE] Stop-loss hit for stock {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id, asset_type='stock')
                        _clear_trailing_stop(order_id)
                        _analyst_last_run.pop(order_id, None)
                        if stoploss_cooldown_hours > 0:
                            from datetime import datetime, timedelta, timezone
                            _stoploss_cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(hours=stoploss_cooldown_hours)
                            save_stoploss_cooldown(symbol, _stoploss_cooldowns[symbol])
                            log.info(f"[{symbol}] Stop-loss cooldown set for {stoploss_cooldown_hours}h")
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol,
                                                   "current_price": current_price, "asset_type": "stock",
                                                   "reason": f"Stop-loss hit ({stop_loss_percentage * 100:.2f}% loss)."})
                    elif pnl_percentage >= take_profit_percentage:
                        log.info(f"[PAPER TRADE] Take-profit hit for stock {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id, asset_type='stock')
                        _clear_trailing_stop(order_id)
                        _analyst_last_run.pop(order_id, None)
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol,
                                                   "current_price": current_price, "asset_type": "stock",
                                                   "reason": f"Take-profit hit ({take_profit_percentage * 100:.2f}% gain)."})

                    # --- Stock Position Analyst (tri-state: HOLD / INCREASE / SELL) ---
                    else:
                        analyst_cfg = settings.get('position_analyst', {})
                        if analyst_cfg.get('enabled', False):
                            from datetime import datetime, timezone
                            now = datetime.now(timezone.utc)
                            check_interval = analyst_cfg.get('check_interval_minutes', 30)
                            last_check = _analyst_last_run.get(order_id)
                            if last_check and (now - last_check).total_seconds() / 60 < check_interval:
                                pass
                            else:
                              min_age_hours = analyst_cfg.get('min_position_age_hours', 2)
                              entry_ts = position.get('entry_timestamp')
                              if entry_ts:
                                try:
                                    if isinstance(entry_ts, str):
                                        entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
                                    else:
                                        entry_dt = entry_ts
                                    if entry_dt.tzinfo is None:
                                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                                    age_hours = (now - entry_dt).total_seconds() / 3600
                                    if age_hours >= min_age_hours:
                                        velocity = compute_news_velocity(symbol)
                                        if (velocity['articles_last_4h'] == 0
                                                and not velocity['breaking_detected']
                                                and velocity['sentiment_trend'] == 'stable'):
                                            log.debug(f"[{symbol}] Stock analyst: no news, default HOLD")
                                            _analyst_last_run[order_id] = now
                                        else:
                                            recent_articles = get_recent_articles(symbol, hours=48, limit=30)
                                            additions = get_position_additions(order_id)
                                            tech_data = {'rsi': None, 'sma': None, 'regime': 'unknown'}
                                            peak_price = _trailing_stop_peaks.get(order_id)
                                            ts_info = None
                                            if peak_price is not None:
                                                ts_info = {
                                                    'peak_price': peak_price,
                                                    'trailing_active': pnl_percentage >= trailing_stop_activation,
                                                    'pnl_percentage': pnl_percentage,
                                                    'activation_threshold': trailing_stop_activation,
                                                }
                                            max_mult = analyst_cfg.get('max_position_multiplier', 3.0)
                                            result = analyze_position_investment(
                                                position, current_price, recent_articles, tech_data,
                                                velocity, hours_held=age_hours,
                                                trailing_stop_info=ts_info,
                                                position_additions=additions,
                                                max_position_multiplier=max_mult,
                                            )
                                            _analyst_last_run[order_id] = now
                                            if result:
                                                rec = result.get('recommendation', 'hold')
                                                confidence = result.get('confidence', 0)
                                                reasoning = result.get('reasoning', '')
                                                risk_level = result.get('risk_level', 'green')

                                                if rec == 'increase' and confidence >= analyst_cfg.get('increase_confidence_threshold', 0.75):
                                                    hint = result.get('increase_sizing_hint', 'small')
                                                    hint_fractions = {'small': 0.25, 'medium': 0.50, 'large': 0.75}
                                                    fraction = hint_fractions.get(hint, 0.25)
                                                    original_value = entry_price * position['quantity']
                                                    total_added = sum(a.get('addition_price', 0) * a.get('addition_quantity', 0) for a in additions)
                                                    estimated_original = original_value - total_added if total_added < original_value else original_value
                                                    add_value = estimated_original * fraction
                                                    add_qty = add_value / current_price if current_price > 0 else 0

                                                    new_total_value = original_value + add_value
                                                    current_mult = new_total_value / estimated_original if estimated_original > 0 else 999
                                                    if current_mult > max_mult:
                                                        log.info(f"[{symbol}] Stock INCREASE blocked: would exceed {max_mult}x cap")
                                                    else:
                                                        balance = get_account_balance(asset_type='stock')
                                                        available = balance.get('USDT', 0)
                                                        if add_value > available:
                                                            log.info(f"[{symbol}] Stock INCREASE blocked: need ${add_value:.2f} but only ${available:.2f}")
                                                        elif is_confirmation_required("INCREASE"):
                                                            await send_signal_for_confirmation({
                                                                'signal': 'INCREASE', 'symbol': symbol,
                                                                'current_price': current_price,
                                                                'quantity': add_qty,
                                                                'reason': reasoning,
                                                                'asset_type': 'stock',
                                                                'position': position,
                                                            })
                                                        else:
                                                            add_to_position(order_id, symbol, add_qty, current_price,
                                                                            reason=reasoning, asset_type='stock')

                                                elif rec == 'sell' and confidence >= analyst_cfg.get('exit_confidence_threshold', 0.8):
                                                    if is_confirmation_required("SELL"):
                                                        await send_signal_for_confirmation({
                                                            'signal': 'SELL', 'symbol': symbol,
                                                            'current_price': current_price,
                                                            'quantity': position['quantity'],
                                                            'reason': reasoning,
                                                            'asset_type': 'stock',
                                                            'position': position,
                                                        })
                                                    else:
                                                        place_order(symbol, "SELL", position['quantity'], current_price,
                                                                    existing_order_id=order_id, asset_type='stock')
                                                        _clear_trailing_stop(order_id)
                                                        _analyst_last_run.pop(order_id, None)
                                                        await send_telegram_alert({
                                                            "signal": "SELL", "symbol": symbol,
                                                            "current_price": current_price, "asset_type": "stock",
                                                            "reason": f"Position analyst sell ({confidence:.0%}): {reasoning}"
                                                        })
                                                else:
                                                    log.info(f"[{symbol}] Stock analyst: {rec} (confidence={confidence:.2f}, risk={risk_level})")
                                                    if risk_level in ('yellow', 'red'):
                                                        await send_position_health_alert(
                                                            symbol, current_price, pnl_percentage * 100, result, position
                                                        )
                                except Exception as e:
                                    log.warning(f"Stock position analyst error for {symbol}: {e}")

                        # Fallback: old position_monitor
                        elif settings.get('position_monitor', {}).get('enabled', False):
                            pos_monitor_cfg = settings.get('position_monitor', {})
                            check_interval = pos_monitor_cfg.get('check_interval_minutes', 60)
                            from datetime import datetime, timezone
                            now = datetime.now(timezone.utc)
                            last_check = _analyst_last_run.get(order_id)
                            if last_check and (now - last_check).total_seconds() / 60 < check_interval:
                                pass
                            else:
                              min_age_hours = pos_monitor_cfg.get('min_position_age_hours', 4)
                              entry_ts = position.get('entry_timestamp')
                              if entry_ts:
                                try:
                                    if isinstance(entry_ts, str):
                                        entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
                                    else:
                                        entry_dt = entry_ts
                                    if entry_dt.tzinfo is None:
                                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                                    age_hours = (now - entry_dt).total_seconds() / 3600
                                    if age_hours >= min_age_hours:
                                        pos_headlines = []
                                        sym_news = news_per_symbol.get(symbol, {})
                                        if sym_news:
                                            pos_headlines.extend(sym_news.get('headlines', [])[:5])
                                        try:
                                            archived = get_recent_articles(symbol, hours=24)
                                            pos_headlines.extend([a.get('title', '') for a in archived[:5]])
                                        except Exception:
                                            pass
                                        tech_data = {'rsi': None, 'sma': None, 'regime': 'unknown'}
                                        peak_price = _trailing_stop_peaks.get(order_id)
                                        ts_info = None
                                        if peak_price is not None:
                                            ts_info = {
                                                'peak_price': peak_price,
                                                'trailing_active': pnl_percentage >= trailing_stop_activation,
                                                'pnl_percentage': pnl_percentage,
                                                'activation_threshold': trailing_stop_activation,
                                            }
                                        health = analyze_position_health(
                                            position, current_price, pos_headlines, tech_data,
                                            hours_held=age_hours, trailing_stop_info=ts_info,
                                        )
                                        _analyst_last_run[order_id] = now
                                        if health:
                                            exit_threshold = pos_monitor_cfg.get('exit_confidence_threshold', 0.8)
                                            if health.get('recommendation') == 'exit' and health.get('confidence', 0) >= exit_threshold:
                                                await send_position_health_alert(
                                                    symbol, current_price, pnl_percentage * 100, health, position
                                                )
                                except Exception as e:
                                    log.warning(f"Stock position monitor error for {symbol}: {e}")

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new stock signal generation.")
            continue

        # Skip signal generation for stocks with open positions —
        # position monitor above handles all exits (SL/TP/trailing/health)
        if paper_trading and any(
            p['symbol'] == symbol and p['status'] == 'OPEN'
            for p in _cached_stock_positions
        ):
            log.debug(f"Skipping signal generation for stock {symbol}: paper position open.")
            continue
        if broker == 'alpaca' and any(
            p['symbol'] == symbol for p in _cached_alpaca_positions
        ):
            log.debug(f"Skipping signal generation for stock {symbol}: Alpaca position open.")
            continue

        # Fetch daily prices for technical analysis (batch or per-symbol fallback)
        if use_alpaca_data:
            from src.collectors.alpaca_data import get_daily_prices_alpaca
            daily_data = get_daily_prices_alpaca(symbol)
        elif symbol in stock_batch_daily:
            daily_data = stock_batch_daily[symbol]
        else:
            daily_data = get_daily_prices(symbol)
        if not daily_data or not daily_data.get('prices'):
            log.warning(f"Could not fetch daily prices for {symbol}. Skipping analysis.")
            continue

        prices = daily_data['prices']
        volumes = daily_data.get('volumes', [])

        # Calculate technical indicators locally
        sma_value = calculate_sma(prices, period=sma_period)
        rsi_value = calculate_rsi(prices, period=rsi_period)

        market_data = {
            'current_price': current_price,
            'sma': sma_value,
            'rsi': rsi_value
        }

        # Prepare volume data
        volume_data = {}
        if volumes:
            current_volume = volumes[-1] if volumes else None
            avg_volume = sum(volumes) / len(volumes) if volumes else None
            volume_data = {
                'current_volume': current_volume,
                'avg_volume': avg_volume,
                'price_change_percent': price_data.get('change_percent', 0)
            }

        # Fetch fundamental data (US stocks only — international tickers have no Alpha Vantage data)
        is_international = '.' in symbol and not symbol.startswith('BRK')
        fundamental_data = get_company_overview(symbol) if not is_international else {}

        # Build per-symbol news sentiment data for the stock signal engine
        stock_news_data = None
        ga = gemini_assessments.get('symbol_assessments', {}).get(symbol) if gemini_assessments else None
        sym_news = news_per_symbol.get(symbol)

        if ga or sym_news:
            stock_news_data = {
                'avg_sentiment_score': sym_news.get('avg_sentiment_score', 0) if sym_news else 0,
                'sentiment_buy_threshold': news_config.get('sentiment_buy_threshold', 0.15),
                'sentiment_sell_threshold': news_config.get('sentiment_sell_threshold', -0.15),
                'min_gemini_confidence': news_config.get('min_gemini_confidence',
                                                         news_config.get('min_claude_confidence', 0.6)),
            }
            if ga:
                stock_news_data['gemini_assessment'] = ga

        # Build stock-specific sentiment config (adds P/E veto)
        stock_sentiment_config = dict(sentiment_config or {})
        stock_sentiment_config['pe_buy_veto_threshold'] = pe_sell  # reuse pe_ratio_sell_threshold

        # Generate stock signal
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
        save_signal(signal)

        # --- Stop-loss cooldown check ---
        if signal['signal'] in ("BUY", "SELL") and symbol in _stoploss_cooldowns:
            from datetime import datetime, timezone
            if datetime.now(timezone.utc) < _stoploss_cooldowns[symbol]:
                log.info(f"Skipping {signal['signal']} for stock {symbol}: stop-loss cooldown active.")
                signal['signal'] = 'HOLD'
            else:
                del _stoploss_cooldowns[symbol]
                clear_stoploss_cooldown(symbol)

        # --- Trade Execution (broker-aware) ---
        if broker == 'alpaca':
            # Use Alpaca for real/paper execution
            alpaca_positions = _cached_alpaca_positions
            pdt_status = _check_pdt_rule()

            if signal['signal'] == "BUY":
                if suppress_buys:
                    log.info(f"Skipping BUY for stock {symbol}: Macro regime RISK_OFF.")
                    signal['signal'] = 'HOLD'
                elif any(p['symbol'] == symbol for p in alpaca_positions):
                    log.info(f"Skipping BUY for stock {symbol}: Position already open on Alpaca.")
                elif len(alpaca_positions) >= max_concurrent_positions:
                    log.info(f"Skipping BUY for stock {symbol}: Max concurrent positions reached.")
                elif pdt_status['is_restricted']:
                    log.info(f"Skipping BUY for stock {symbol}: PDT rule — no day trades remaining.")
                else:
                    sector_ok, sector_msg = check_sector_limit(symbol, alpaca_positions)
                    if not sector_ok:
                        log.info(f"Skipping BUY for stock {symbol}: {sector_msg}")
                    else:
                        ev_action, ev_mult, ev_reason = check_event_gate(symbol, 'BUY', asset_type='stock')
                        if ev_action == 'block':
                            log.info(f"Skipping BUY for stock {symbol}: {ev_reason}")
                            signal['signal'] = 'HOLD'
                        else:
                            size_mult = macro_multiplier * (ev_mult if ev_action == 'reduce' else 1.0)
                            balance = get_stock_balance()
                            buying_power = balance.get('buying_power', 0)
                            stock_trade_stats = get_trade_history_stats()
                            stock_kelly = stock_trade_stats.get('kelly_fraction', 0.0)
                            stock_risk_pct = stock_kelly if (stock_kelly > 0 and stock_trade_stats.get('total_trades', 0) >= 10) else trade_risk_percentage
                            capital_to_risk = buying_power * stock_risk_pct * size_mult
                            quantity_to_buy = capital_to_risk / current_price
                            if quantity_to_buy * current_price > buying_power:
                                log.warning(f"Skipping BUY for stock {symbol}: Insufficient buying power.")
                            else:
                                signal['quantity'] = quantity_to_buy
                                if is_confirmation_required("BUY"):
                                    log.info(f"Sending BUY {symbol} (Alpaca) for confirmation.")
                                    await send_signal_for_confirmation(signal)
                                else:
                                    log.info(f"Executing Alpaca trade: BUY {quantity_to_buy:.4f} {symbol} (size_mult={size_mult:.2f}).")
                                    order_result = place_stock_order(symbol, "BUY", quantity_to_buy, current_price)
                                    if order_result.get('status') == 'FILLED':
                                        signal['order_result'] = order_result
                                    await send_telegram_alert(signal)

            elif signal['signal'] == "SELL":
                alpaca_pos = next((p for p in alpaca_positions if p['symbol'] == symbol), None)
                if alpaca_pos:
                    signal['quantity'] = alpaca_pos['quantity']
                    signal['position'] = alpaca_pos
                    if is_confirmation_required("SELL"):
                        log.info(f"Sending SELL {symbol} (Alpaca) for confirmation.")
                        await send_signal_for_confirmation(signal)
                    else:
                        log.info(f"Executing Alpaca trade: SELL {alpaca_pos['quantity']:.4f} {symbol}.")
                        order_result = place_stock_order(symbol, "SELL", alpaca_pos['quantity'], current_price)
                        if order_result.get('status') == 'FILLED':
                            signal['order_result'] = order_result
                        await send_telegram_alert(signal)
                else:
                    log.info(f"Skipping SELL for stock {symbol}: No open position on Alpaca.")
            else:
                log.info(f"Signal is HOLD for stock {symbol}. No trade action taken.")

        else:
            # Paper-only execution via binance_trader paper path
            open_positions = _cached_stock_positions
            current_balance = get_account_balance(asset_type='stock').get('total_usd', paper_trading_initial_capital)

            stock_trade_stats = get_trade_history_stats()
            stock_kelly = stock_trade_stats.get('kelly_fraction', 0.0)
            stock_risk_pct = stock_kelly if (stock_kelly > 0 and stock_trade_stats.get('total_trades', 0) >= 10) else trade_risk_percentage

            if signal['signal'] == "BUY":
                if suppress_buys:
                    log.info(f"Skipping BUY for stock {symbol}: Macro regime RISK_OFF.")
                    signal['signal'] = 'HOLD'
                elif any(p['symbol'] == symbol and p['status'] == 'OPEN' for p in open_positions):
                    log.info(f"Skipping BUY for stock {symbol}: Position already open.")
                elif len(open_positions) >= max_concurrent_positions:
                    log.info(f"Skipping BUY for stock {symbol}: Max concurrent positions reached.")
                else:
                    sector_ok, sector_msg = check_sector_limit(symbol, open_positions)
                    if not sector_ok:
                        log.info(f"Skipping BUY for stock {symbol}: {sector_msg}")
                    else:
                        ev_action, ev_mult, ev_reason = check_event_gate(symbol, 'BUY', asset_type='stock')
                        if ev_action == 'block':
                            log.info(f"Skipping BUY for stock {symbol}: {ev_reason}")
                            signal['signal'] = 'HOLD'
                        else:
                            size_mult = macro_multiplier * (ev_mult if ev_action == 'reduce' else 1.0)
                            capital_to_risk = current_balance * stock_risk_pct * size_mult
                            quantity_to_buy = capital_to_risk / current_price
                            if quantity_to_buy * current_price > current_balance:
                                log.warning(f"Skipping BUY for stock {symbol}: Insufficient balance.")
                            else:
                                signal['quantity'] = quantity_to_buy
                                if is_confirmation_required("BUY"):
                                    log.info(f"Sending BUY {symbol} (paper stock) for confirmation.")
                                    await send_signal_for_confirmation(signal)
                                else:
                                    log.info(f"Executing paper trade: BUY {quantity_to_buy:.4f} {symbol} "
                                             f"(risk={stock_risk_pct:.4f}, size_mult={size_mult:.2f}).")
                                    place_order(symbol, "BUY", quantity_to_buy, current_price, asset_type='stock')
                                    await send_telegram_alert(signal)

            elif signal['signal'] == "SELL":
                position_to_close = next(
                    (p for p in open_positions if p['symbol'] == symbol and p['status'] == 'OPEN'), None)
                if position_to_close:
                    signal['quantity'] = position_to_close['quantity']
                    signal['position'] = position_to_close
                    if is_confirmation_required("SELL"):
                        log.info(f"Sending SELL {symbol} (paper stock) for confirmation.")
                        await send_signal_for_confirmation(signal)
                    else:
                        log.info(f"Executing paper trade: SELL {position_to_close['quantity']:.4f} {symbol}.")
                        place_order(symbol, "SELL", position_to_close['quantity'], current_price,
                                    existing_order_id=position_to_close['order_id'], asset_type='stock')
                        _clear_trailing_stop(position_to_close['order_id'])
                        await send_telegram_alert(signal)
                else:
                    log.info(f"Skipping SELL for stock {symbol}: No open position found.")
            else:
                log.info(f"Signal is HOLD for stock {symbol}. No trade action taken.")

        # --- Auto-Trading Shadow Bot: Stock Position Monitoring ---
        if auto_enabled:
            for position in _cached_auto_stock_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    entry_price = position['entry_price']
                    pnl_percentage = (current_price - entry_price) / entry_price
                    order_id = position['order_id']

                    peak_price = _auto_update_trailing_stop(order_id, current_price)
                    drawdown_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0

                    if trailing_stop_enabled and pnl_percentage >= trailing_stop_activation:
                        if drawdown_from_peak >= trailing_stop_distance:
                            log.info(f"[AUTO] Trailing stop triggered for stock {symbol}.")
                            place_order(symbol, "SELL", position['quantity'], current_price,
                                        existing_order_id=order_id, asset_type='stock', trading_strategy='auto')
                            _auto_clear_trailing_stop(order_id)
                            continue

                    if pnl_percentage <= -stop_loss_percentage:
                        log.info(f"[AUTO] Stop-loss hit for stock {symbol}.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id, asset_type='stock', trading_strategy='auto')
                        _auto_clear_trailing_stop(order_id)
                        if stoploss_cooldown_hours > 0:
                            from datetime import datetime, timedelta, timezone
                            _auto_stoploss_cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(hours=stoploss_cooldown_hours)
                    elif pnl_percentage >= take_profit_percentage:
                        log.info(f"[AUTO] Take-profit hit for stock {symbol}.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id, asset_type='stock', trading_strategy='auto')
                        _auto_clear_trailing_stop(order_id)

        # --- Auto-Trading Shadow Bot: Stock Signal Execution ---
        if auto_enabled and bot_is_running.is_set() and signal is not None:
            auto_open_stocks = [p for p in _cached_auto_stock_positions
                                if p['status'] == 'OPEN']
            auto_max = auto_cfg.get('max_concurrent_positions', max_concurrent_positions)

            auto_signal_type = signal.get('signal', 'HOLD')
            if auto_signal_type in ("BUY", "SELL") and symbol in _auto_stoploss_cooldowns:
                from datetime import datetime, timezone
                if datetime.now(timezone.utc) < _auto_stoploss_cooldowns[symbol]:
                    auto_signal_type = 'HOLD'
                else:
                    del _auto_stoploss_cooldowns[symbol]

            if auto_signal_type == "BUY":
                if suppress_buys:
                    log.info(f"[AUTO] Skipping BUY for stock {symbol}: Macro regime RISK_OFF.")
                elif not any(p['symbol'] == symbol and p['status'] == 'OPEN' for p in auto_open_stocks):
                    if len(auto_open_stocks) < auto_max:
                        sector_ok, sector_msg = check_sector_limit(symbol, auto_open_stocks, 'auto')
                        if not sector_ok:
                            log.info(f"[AUTO] Skipping BUY for stock {symbol}: {sector_msg}")
                        else:
                            ev_action, ev_mult, ev_reason = check_event_gate(
                                symbol, 'BUY', asset_type='stock')
                            if ev_action == 'block':
                                log.info(f"[AUTO] Skipping BUY for stock {symbol}: {ev_reason}")
                            else:
                                size_mult = macro_multiplier * (ev_mult if ev_action == 'reduce' else 1.0)
                                auto_balance = get_account_balance(asset_type='stock', trading_strategy='auto')
                                auto_available = auto_balance.get('USDT', 0)
                                capital_to_risk = auto_available * trade_risk_percentage * size_mult
                                qty = capital_to_risk / current_price if current_price > 0 else 0
                                if qty > 0 and qty * current_price <= auto_available:
                                    log.info(f"[AUTO] Executing BUY {qty:.4f} stock {symbol} (size_mult={size_mult:.2f})")
                                    place_order(symbol, "BUY", qty, current_price,
                                                asset_type='stock', trading_strategy='auto')
                                else:
                                    log.info(f"[AUTO] Insufficient balance for stock BUY {symbol}")

            elif auto_signal_type == "SELL":
                auto_pos = next((p for p in auto_open_stocks
                                 if p['symbol'] == symbol and p['status'] == 'OPEN'), None)
                if auto_pos:
                    log.info(f"[AUTO] Executing SELL {auto_pos['quantity']:.4f} stock {symbol}")
                    place_order(symbol, "SELL", auto_pos['quantity'], current_price,
                                existing_order_id=auto_pos['order_id'],
                                asset_type='stock', trading_strategy='auto')
                    _auto_clear_trailing_stop(auto_pos['order_id'])

    log.info("--- Stock trading cycle complete ---")


async def bot_loop():
    """
    The main indefinite loop for the bot.
    """
    run_interval_minutes = app_config.get('settings', {}).get('run_interval_minutes', 15)
    while True:
        try:
            await run_bot_cycle()
        except Exception as e:
            log.error(f"Error in bot_loop cycle: {e}", exc_info=True)
        log.info(f"Cycle complete. Waiting for {run_interval_minutes} minutes...")
        await asyncio.sleep(run_interval_minutes * 60)


async def run_single_status_update():
    """Fetches and sends a single status update."""
    status_config = app_config.get('settings', {}).get('regular_status_update', {})
    interval_hours = status_config.get('interval_hours', 1)

    from src.database import get_trade_summary
    from src.notify.telegram_bot import send_performance_report

    try:
        log.info("Fetching trade summary for status update...")
        summary = get_trade_summary(hours_ago=interval_hours)
        if application:
            await send_performance_report(application, summary, interval_hours)
    except Exception as e:
        log.error(f"Error in run_single_status_update: {e}")


async def status_update_loop():
    """
    A separate loop to send periodic status updates.
    """
    status_config = app_config.get('settings', {}).get('regular_status_update', {})
    if not status_config.get('enabled'):
        log.info("Regular status updates are disabled.")
        return

    interval_hours = status_config.get('interval_hours', 1)
    log.info(f"Starting regular status update loop. Interval: {interval_hours} hours.")

    while True:
        try:
            await run_single_status_update()
        except Exception as e:
            log.error(f"Error in status_update_loop: {e}", exc_info=True)
        await asyncio.sleep(interval_hours * 3600)


async def _signal_cleanup_loop():
    """Periodically cleans up expired pending signals."""
    while True:
        try:
            await cleanup_expired_signals()
        except Exception as e:
            log.error(f"Error in signal cleanup loop: {e}", exc_info=True)
        await asyncio.sleep(60)  # check every minute


async def auto_bot_summary_loop():
    """Periodic summary of auto-bot performance."""
    auto_cfg = app_config.get('settings', {}).get('auto_trading', {})
    if not auto_cfg.get('enabled', False):
        return
    interval = auto_cfg.get('summary_interval_hours', 1)
    while True:
        await asyncio.sleep(interval * 3600)
        try:
            summary = get_trade_summary(hours_ago=interval, trading_strategy='auto')
            auto_positions = get_open_positions(trading_strategy='auto')
            auto_balance = get_account_balance(trading_strategy='auto')
            if application:
                await send_auto_bot_summary(application, summary, auto_positions, auto_balance, interval)
        except Exception as e:
            log.error(f"Auto-bot summary error: {e}", exc_info=True)


@app.on_event("startup")
async def startup_event():
    """
    On startup, initialize the Telegram bot, set the webhook,
    and start the background tasks.
    """
    global application
    log.info("Starting application...")

    # Restore trailing stop peaks from database (survives restarts)
    try:
        loaded = load_trailing_stop_peaks()
        _trailing_stop_peaks.update(loaded)
        log.info(f"Loaded {len(loaded)} trailing stop peaks from database.")
    except Exception as e:
        log.warning(f"Could not load trailing stop peaks: {e}")

    # Restore stoploss cooldowns from database (survives restarts)
    try:
        loaded_cooldowns = load_stoploss_cooldowns()
        _stoploss_cooldowns.update(loaded_cooldowns)
        log.info(f"Loaded {len(loaded_cooldowns)} stoploss cooldowns from database.")
    except Exception as e:
        log.warning(f"Could not load stoploss cooldowns: {e}")

    # Initialize the Telegram application
    application = await start_bot()

    # Set the webhook. The URL must be passed as an environment variable.
    # For Google Cloud Run, this is often provided as `GOOGLE_CLOUD_RUN_SERVICE_URL`.
    service_url = os.environ.get("SERVICE_URL")
    if not service_url:
        log.warning("SERVICE_URL environment variable not set. Webhook will not be set.")
    else:
        webhook_url = f"{service_url}/webhook"
        webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        log.info(f"Setting webhook to: {webhook_url}")
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=webhook_secret
        )

    # Register signal confirmation callback
    register_execute_callback(execute_confirmed_signal)

    # Start background tasks
    _background_tasks.append(asyncio.create_task(bot_loop()))
    _background_tasks.append(asyncio.create_task(status_update_loop()))
    _background_tasks.append(asyncio.create_task(_signal_cleanup_loop()))

    # Start auto-bot summary loop if enabled
    auto_cfg = app_config.get('settings', {}).get('auto_trading', {})
    if auto_cfg.get('enabled', False):
        _background_tasks.append(asyncio.create_task(auto_bot_summary_loop()))
        log.info("Auto-trading shadow bot enabled — summary loop started.")

    log.info("Startup complete. Background tasks running.")


@app.on_event("shutdown")
async def shutdown_event_handler():
    """
    On shutdown, cancel background tasks and gracefully clean up.
    """
    log.info("Shutting down application...")

    # Cancel background tasks
    for task in _background_tasks:
        task.cancel()
    for task in _background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _background_tasks.clear()

    # Stop Telegram bot
    if application:
        try:
            from src.notify.telegram_bot import stop_bot
            await stop_bot(application)
        except Exception as e:
            log.error(f"Error stopping Telegram bot during shutdown: {e}", exc_info=True)

    log.info("Shutdown complete.")


@app.get("/health", status_code=200)
async def health_check():
    """
    Health check endpoint for Cloud Run.
    """
    return {"status": "ok"}


@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Handles incoming updates from the Telegram API webhook.
    Validates the secret token header to prevent CSRF attacks.
    """
    if not application:
        log.error("Webhook received but application not initialized.")
        return {"status": "error", "message": "Bot not initialized"}, 500

    # Validate the Telegram secret token if configured
    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if webhook_secret:
        token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token_header != webhook_secret:
            log.warning("Webhook request rejected: invalid secret token.")
            return {"status": "error", "message": "Unauthorized"}, 403

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        log.error(f"Error processing webhook: {e}", exc_info=True)
        return {"status": "error"}, 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the crypto trading bot.")
    parser.add_argument('--collect-only', action='store_true',
                        help='Run in data collection mode only.')
    args = parser.parse_args()

    if args.collect_only:
        log.info("--- Collect-only mode: no standalone data collection needed ---")
        log.info("News scraping is handled by scripts/scrape_news_standalone.py")
    else:
        port = int(os.environ.get("PORT", 8080))
        log.info(f"Starting Uvicorn server on port {port}...")
        uvicorn.run(app, host="0.0.0.0", port=port)

