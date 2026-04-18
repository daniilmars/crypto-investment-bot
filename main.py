#!/usr/bin/env python3
# --- Main Application File ---
# This script orchestrates the entire bot's workflow.
# Force redeploy 2025-11-02_v2
# Force redeploy 2025-11-02
# Force redeploy 2025-10-16

import argparse
import asyncio
import os
import signal
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update

from src.config import app_config
from src.database import (initialize_database,
                          load_trailing_stop_peaks,
                          load_stoploss_cooldowns, load_signal_cooldowns,
                          load_bot_state)
from src.execution.circuit_breaker import (resolve_stale_circuit_breaker_events,
                                           load_session_peaks)
from src.logger import log
from src.notify.telegram_bot import (start_bot,
                                     register_execute_callback,
                                     cleanup_expired_signals)
from src.notify.telegram_live_dashboard import load_dashboard_state
from src.orchestration import bot_state
from src.orchestration.trade_executor import execute_confirmed_signal
from src.orchestration.cycle_runner import (
    run_bot_cycle, set_application,
)

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

# Mini App dashboard (dark-launched — dormant until MINIAPP_BASE_URL is set)
try:
    from src.api.miniapp_routes import router as _miniapp_router, mount_miniapp_static
    app.include_router(_miniapp_router, prefix="/api/miniapp")
    mount_miniapp_static(app)
except Exception as _miniapp_err:  # pragma: no cover — scaffolding guard
    import logging as _logging
    _logging.getLogger(__name__).warning("Mini App routes not mounted: %s", _miniapp_err)


# State is managed centrally in bot_state module


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


async def periodic_summary_loop():
    """Sends a consolidated 4-hour summary."""
    summary_cfg = app_config.get('settings', {}).get('periodic_summary', {})
    if not summary_cfg.get('enabled', True):
        return
    interval = summary_cfg.get('interval_hours', 4) * 3600
    startup_delay = summary_cfg.get('startup_delay_minutes', 10) * 60
    await asyncio.sleep(startup_delay)
    while True:
        try:
            from src.notify.telegram_periodic_summary import send_periodic_summary
            await send_periodic_summary()
        except Exception as e:
            log.error(f"Periodic summary error: {e}", exc_info=True)
        await asyncio.sleep(interval)


async def _signal_cleanup_loop():
    """Periodically cleans up expired pending signals."""
    while True:
        try:
            await cleanup_expired_signals()
        except Exception as e:
            log.error(f"Error in signal cleanup loop: {e}", exc_info=True)
        await asyncio.sleep(60)


async def daily_sector_review_loop():
    """Runs daily sector review at configured hour (UTC)."""
    sector_cfg = app_config.get('settings', {}).get('sector_review', {})
    if not sector_cfg.get('enabled', True):
        return
    target_hour = sector_cfg.get('review_hour_utc', 7)
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        try:
            from src.analysis.sector_review import run_sector_review
            result = await asyncio.to_thread(run_sector_review)
            if result:
                log.info(f"Daily sector review complete: "
                         f"{len(result.get('sectors', {}))} sectors scored.")

            # Check for conviction spikes requiring midweek thesis refresh
            try:
                strategies = app_config.get('settings', {}).get('strategies', {})
                midweek_cfg = strategies.get('longterm', {}).get(
                    'thesis_review', {}).get('midweek_refresh', {})
                if midweek_cfg.get('enabled', False):
                    from src.analysis.thesis_generator import check_conviction_spike_refresh
                    refresh_result = await asyncio.to_thread(
                        check_conviction_spike_refresh)
                    if refresh_result:
                        added = refresh_result.get('added_sectors', [])
                        n_stocks = refresh_result.get('added_stocks', 0)
                        summary = (
                            f"Midweek thesis refresh: added {len(added)} sector(s) "
                            f"({', '.join(added)}), {n_stocks} new stocks")
                        from src.notify.telegram_bot import send_telegram_alert
                        await send_telegram_alert({
                            'signal': 'INFO', 'symbol': 'THESIS',
                            'reason': summary, 'asset_type': 'system'})
                        log.info(summary)
            except Exception as e:
                log.error(f"Midweek thesis refresh check error: {e}", exc_info=True)
        except Exception as e:
            log.error(f"Daily sector review error: {e}", exc_info=True)


async def weekly_self_review_loop():
    """Runs weekly self-review on configured day/hour (UTC)."""
    review_cfg = app_config.get('settings', {}).get('autonomous_bot', {}).get(
        'weekly_self_review', {})
    if not review_cfg.get('enabled', True):
        return
    target_day = review_cfg.get('review_day', 6)  # 0=Mon, 6=Sun
    target_hour = review_cfg.get('review_hour_utc', 4)
    while True:
        now = datetime.now(timezone.utc)
        # Find next target_day at target_hour
        days_ahead = (target_day - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= target_hour:
            days_ahead = 7
        next_run = (now + timedelta(days=days_ahead)).replace(
            hour=target_hour, minute=0, second=0, microsecond=0)
        wait_seconds = (next_run - now).total_seconds()
        log.info(f"Weekly self-review scheduled in {wait_seconds/3600:.1f}h")
        await asyncio.sleep(wait_seconds)
        try:
            from src.analysis.weekly_self_review import (
                run_weekly_self_review, format_weekly_review_telegram)
            result = await asyncio.to_thread(run_weekly_self_review)
            if result:
                msg = format_weekly_review_telegram(result)
                from src.notify.telegram_bot import send_telegram_alert
                await send_telegram_alert({
                    'signal': 'INFO', 'symbol': 'WEEKLY_REVIEW',
                    'reason': msg, 'asset_type': 'system'})
                log.info("Weekly self-review sent.")
        except Exception as e:
            log.error(f"Weekly self-review error: {e}", exc_info=True)


async def weekly_thesis_review_loop():
    """Runs weekly investment thesis generation for longterm strategy."""
    strategies = app_config.get('settings', {}).get('strategies', {})
    thesis_cfg = strategies.get('longterm', {}).get('thesis_review', {})
    if not thesis_cfg.get('enabled', False):
        return
    target_day = thesis_cfg.get('review_day', 0)  # 0=Monday
    target_hour = thesis_cfg.get('review_hour_utc', 5)
    while True:
        now = datetime.now(timezone.utc)
        days_ahead = (target_day - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= target_hour:
            days_ahead = 7
        next_run = (now + timedelta(days=days_ahead)).replace(
            hour=target_hour, minute=0, second=0, microsecond=0)
        wait_seconds = (next_run - now).total_seconds()
        log.info(f"Thesis review scheduled in {wait_seconds/3600:.1f}h")
        await asyncio.sleep(wait_seconds)
        try:
            from src.analysis.thesis_generator import generate_investment_thesis
            result = await asyncio.to_thread(generate_investment_thesis)
            if result:
                sectors = result.get('sectors', [])
                total_stocks = sum(len(s.get('stocks', [])) for s in sectors)
                summary = f"Thesis updated: {len(sectors)} sectors, {total_stocks} stocks"
                from src.notify.telegram_bot import send_telegram_alert
                await send_telegram_alert({
                    'signal': 'INFO', 'symbol': 'THESIS_REVIEW',
                    'reason': summary, 'asset_type': 'system'})
                log.info(summary)
        except Exception as e:
            log.error(f"Thesis review error: {e}", exc_info=True)


async def db_cleanup_loop():
    """Daily cleanup of old market_prices, signals, and news_sentiment rows."""
    while True:
        await asyncio.sleep(24 * 3600)  # run once per day
        try:
            from src.database import cleanup_old_rows
            deleted = await asyncio.to_thread(cleanup_old_rows, 30)
            if any(v > 0 for v in deleted.values()):
                log.info(f"DB cleanup: {deleted}")
        except Exception as e:
            log.error(f"DB cleanup error: {e}", exc_info=True)


async def fx_refresh_loop():
    """Refresh foreign-currency USD rates every 6 hours. Hydrates at startup."""
    from src.analysis.fx import refresh_all_rates
    # Initial hydrate on startup so to_usd() hits real rates, not the fallback map
    try:
        await asyncio.to_thread(refresh_all_rates)
    except Exception as e:
        log.error(f"FX initial refresh error: {e}", exc_info=True)
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            await asyncio.to_thread(refresh_all_rates)
        except Exception as e:
            log.error(f"FX refresh error: {e}", exc_info=True)


async def _chat_session_cleanup_loop():
    """Periodically cleans up expired AI chat sessions and watchlist items."""
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            from src.notify.telegram_chat import cleanup_expired_sessions
            cleanup_expired_sessions()
        except Exception as e:
            log.error(f"Chat session cleanup error: {e}", exc_info=True)
        try:
            from src.database import expire_watchlist_items
            await asyncio.to_thread(expire_watchlist_items)
        except Exception as e:
            log.error(f"Watchlist expiry error: {e}", exc_info=True)


async def db_backup_loop():
    """Daily database backup to GCS (if configured)."""
    if not os.environ.get('GCP_PROJECT_ID'):
        log.info("GCP_PROJECT_ID not set — DB backup loop disabled.")
        return
    while True:
        await asyncio.sleep(24 * 3600)  # run once per day
        try:
            from src.database_backup import backup_db_to_gcs
            project_id = os.environ.get('GCP_PROJECT_ID', '')
            bucket_name = f"{project_id}-db-backups"
            result = await asyncio.to_thread(backup_db_to_gcs, bucket_name)
            if result:
                log.info(f"DB backup completed: {result}")
        except Exception as e:
            log.error(f"DB backup error: {e}", exc_info=True)


@app.on_event("startup")
async def startup_event():
    """
    On startup, initialize the Telegram bot, set the webhook,
    and start the background tasks.
    """
    global application
    log.info("Starting application...")

    # Reload auto-tuned parameters from DB (survives restarts)
    try:
        from src.analysis.auto_tuner import reload_tuned_params
        await asyncio.to_thread(reload_tuned_params)
    except Exception as e:
        log.warning(f"Could not reload tuned params: {e}")

    # Restore trailing stop peaks from database (survives restarts)
    try:
        loaded = await load_trailing_stop_peaks()
        manual_peaks = {oid: peak for oid, (peak, strat) in loaded.items() if strat != 'auto'}
        auto_peaks = {oid: peak for oid, (peak, strat) in loaded.items() if strat == 'auto'}
        bot_state.load_peaks(manual_peaks)
        bot_state.load_auto_peaks(auto_peaks)
        log.info(f"Loaded {len(manual_peaks)} manual + {len(auto_peaks)} auto trailing stop peaks from database.")
    except Exception as e:
        log.warning(f"Could not load trailing stop peaks: {e}")

    # Restore stoploss cooldowns from database (survives restarts)
    try:
        loaded_cooldowns = await load_stoploss_cooldowns()
        bot_state.load_cooldowns(loaded_cooldowns)
        log.info(f"Loaded {len(loaded_cooldowns)} stoploss cooldowns from database.")
    except Exception as e:
        log.warning(f"Could not load stoploss cooldowns: {e}")

    # Restore signal cooldowns from database (survives restarts)
    try:
        manual_cd, auto_cd = await load_signal_cooldowns()
        bot_state.load_signal_cooldown_state(manual_cd, auto_cd)
        log.info(f"Loaded {len(manual_cd)} manual + {len(auto_cd)} auto signal cooldowns from database.")
    except Exception as e:
        log.warning(f"Could not load signal cooldowns: {e}")

    # Restore streak sizing state from database (survives restarts)
    try:
        import json as _json
        for _sn in ('auto', 'conservative', 'longterm'):
            _stored = load_bot_state(f'streak_state:{_sn}')
            if _stored:
                bot_state.strategy_load_streak_state(_sn, _json.loads(_stored))
        log.info("Loaded streak sizing state from database.")
    except Exception as e:
        log.warning(f"Could not load streak state: {e}")

    # Resolve stale circuit breaker events from previous runs
    try:
        await asyncio.to_thread(resolve_stale_circuit_breaker_events)
    except Exception as e:
        log.warning(f"Could not resolve stale circuit breaker events: {e}")

    # Load session peak balances from DB (survives restarts)
    try:
        await asyncio.to_thread(load_session_peaks)
    except Exception as e:
        log.warning(f"Could not load session peaks: {e}")

    # Reconcile positions against exchange (live/testnet only)
    try:
        from src.execution.binance_trader import reconcile_crypto_positions
        stale_crypto = await asyncio.to_thread(reconcile_crypto_positions)
        if stale_crypto:
            log.info(f"Reconciled {stale_crypto} stale crypto positions at startup.")
    except Exception as e:
        log.warning(f"Crypto position reconciliation failed: {e}")

    try:
        from src.execution.stock_trader import reconcile_stock_positions
        stale_stocks = await asyncio.to_thread(reconcile_stock_positions)
        if stale_stocks:
            log.info(f"Reconciled {stale_stocks} stale stock positions at startup.")
    except Exception as e:
        log.warning(f"Stock position reconciliation failed: {e}")

    # Initialize the Telegram application
    application = await start_bot()

    # Share application reference with cycle_runner
    set_application(application)

    # Set the webhook
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

    # Load sector convictions from DB (survives container restarts)
    try:
        from src.database import get_latest_sector_convictions
        from src.analysis.sector_review import load_convictions_into_cache
        convictions = await get_latest_sector_convictions()
        if convictions:
            load_convictions_into_cache(convictions)
    except Exception as e:
        log.warning(f"Could not load sector convictions: {e}")

    # Load live dashboard state (persisted message ID)
    await load_dashboard_state()

    # Start background tasks
    _background_tasks.append(asyncio.create_task(bot_loop()))
    _background_tasks.append(asyncio.create_task(_signal_cleanup_loop()))

    _background_tasks.append(asyncio.create_task(periodic_summary_loop()))
    log.info("4h periodic summary loop started.")

    sector_review_cfg = app_config.get('settings', {}).get('sector_review', {})
    if sector_review_cfg.get('enabled', True):
        _background_tasks.append(asyncio.create_task(daily_sector_review_loop()))
        log.info("Daily sector review loop started.")

    _background_tasks.append(asyncio.create_task(weekly_self_review_loop()))
    _background_tasks.append(asyncio.create_task(weekly_thesis_review_loop()))

    # Load longterm thesis into cache at startup
    try:
        from src.database import get_active_thesis
        from src.analysis.thesis_generator import load_thesis_into_cache
        thesis = await asyncio.to_thread(get_active_thesis)
        if thesis:
            load_thesis_into_cache(thesis['thesis_json'])
    except Exception as e:
        log.debug(f"Thesis cache not loaded: {e}")

    _background_tasks.append(asyncio.create_task(db_cleanup_loop()))
    _background_tasks.append(asyncio.create_task(db_backup_loop()))
    _background_tasks.append(asyncio.create_task(_chat_session_cleanup_loop()))
    _background_tasks.append(asyncio.create_task(fx_refresh_loop()))

    # Register SIGTERM handler for logging (Uvicorn triggers shutdown hooks)
    def _sigterm_handler(signum, frame):
        log.info(f"Received signal {signum} (SIGTERM) — shutdown will proceed via FastAPI hooks")

    signal.signal(signal.SIGTERM, _sigterm_handler)

    log.info("Startup complete. Background tasks running.")


@app.on_event("shutdown")
async def shutdown_event_handler():
    """
    On shutdown, cancel background tasks and gracefully clean up.
    """
    log.info("Shutting down application...")

    # Grace period: let in-flight DB writes finish before cancelling tasks
    log.info("Waiting 2s grace period for in-flight operations...")
    await asyncio.sleep(2)

    for task in _background_tasks:
        task.cancel()
    for task in _background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _background_tasks.clear()

    if application:
        try:
            from src.notify.telegram_bot import stop_bot
            await stop_bot(application)
        except Exception as e:
            log.error(f"Error stopping Telegram bot during shutdown: {e}", exc_info=True)

    # Close DB connection pool
    try:
        from src.database import close_db_pool
        close_db_pool()
    except Exception as e:
        log.error(f"Error closing DB pool during shutdown: {e}", exc_info=True)

    log.info("Shutdown complete.")


@app.get("/health")
async def health_check():
    """
    Health check endpoint — verifies bot loop and DB are alive.
    """
    checks = {}
    healthy = True

    # Check bot loop is running (not stalled)
    for task in _background_tasks:
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc:
                checks['bot_loop'] = f"crashed: {exc}"
                healthy = False
                break
    else:
        checks['bot_loop'] = 'running'

    # Check for hung bot loop (last cycle too old)
    last_cycle = bot_state.get_last_cycle_at()
    if last_cycle:
        run_interval = app_config.get('settings', {}).get('run_interval_minutes', 15)
        stale_threshold = timedelta(minutes=run_interval * 2.5)
        if datetime.now(timezone.utc) - last_cycle > stale_threshold:
            checks['bot_loop'] = f'stale (last cycle: {last_cycle.isoformat()})'
            healthy = False
        else:
            checks['bot_loop'] = f'ok (last cycle: {last_cycle.strftime("%H:%M:%S")})'

    # Check DB connectivity
    try:
        from src.database import get_db_connection
        conn = await asyncio.to_thread(get_db_connection)
        if conn:
            conn.close()
            checks['database'] = 'ok'
        else:
            checks['database'] = 'no connection'
            healthy = False
    except Exception as e:
        checks['database'] = str(e)
        healthy = False

    # Check Telegram bot initialized
    checks['telegram'] = 'ok' if application else 'not initialized'
    if not application:
        healthy = False

    status_code = 200 if healthy else 503
    return JSONResponse({"status": "ok" if healthy else "degraded", "checks": checks}, status_code=status_code)


@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Handles incoming updates from the Telegram API webhook.
    """
    if not application:
        log.error("Webhook received but application not initialized.")
        return JSONResponse({"status": "error", "message": "Bot not initialized"}, status_code=500)

    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if webhook_secret:
        token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token_header != webhook_secret:
            log.warning("Webhook request rejected: invalid secret token.")
            return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=403)

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        if update is None:
            return {"status": "ignored"}
        # Enqueue and return 200 immediately. Telegram times out the POST at
        # ~60s; during a cycle, handlers like /dashboard can exceed that.
        # The application worker processes the update off the request path.
        await application.update_queue.put(update)
        return {"status": "ok"}
    except Exception as e:
        log.error(f"Error processing webhook: {e}", exc_info=True)
        return JSONResponse({"status": "error"}, status_code=500)


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
