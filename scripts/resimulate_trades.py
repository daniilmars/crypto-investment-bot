import argparse
import pandas as pd
from src.database import get_db_connection, get_price_history_for_trade
from src.logger import log
from src.config import app_config

def resimulate_trades(database_url: str, dry_run: bool):
    """
    Re-simulates all closed trades to correct exit price, timestamp, and PnL
    based on historical price data and the original stop-loss/take-profit rules.
    """
    log.info(f"Starting trade re-simulation. DRY RUN: {dry_run}")
    conn = None
    try:
        conn = get_db_connection(database_url)
        
        # Load risk management settings from config (assuming they were constant)
        # NOTE: This uses local config, which should match the historical settings.
        settings = app_config.get('settings', {})
        stop_loss_pct = settings.get('stop_loss_percentage', 0.02)
        take_profit_pct = settings.get('take_profit_percentage', 0.05)
        log.info(f"Using Stop-Loss: {stop_loss_pct*100:.2f}%, Take-Profit: {take_profit_pct*100:.2f}%")

        # Fetch all trades that need correction
        trades_df = pd.read_sql_query("SELECT * FROM trades", conn)
        log.info(f"Found {len(trades_df)} total trades to analyze.")

        corrections = []
        for _, trade in trades_df.iterrows():
            price_history = get_price_history_for_trade(trade['symbol'], trade['entry_timestamp'], database_url)
            
            true_exit_price = None
            true_exit_timestamp = None

            for price_point in price_history:
                price = price_point['price']
                timestamp = price_point['timestamp']
                
                if trade['side'] == 'BUY':
                    # Check for stop-loss or take-profit for a BUY trade
                    if price <= trade['entry_price'] * (1 - stop_loss_pct):
                        true_exit_price = price
                        true_exit_timestamp = timestamp
                        log.info(f"Trade {trade['order_id']} (BUY {trade['symbol']}): Stop-loss hit at {price} on {timestamp}")
                        break
                    if price >= trade['entry_price'] * (1 + take_profit_pct):
                        true_exit_price = price
                        true_exit_timestamp = timestamp
                        log.info(f"Trade {trade['order_id']} (BUY {trade['symbol']}): Take-profit hit at {price} on {timestamp}")
                        break
                elif trade['side'] == 'SELL':
                    # Check for stop-loss or take-profit for a SELL trade
                    if price >= trade['entry_price'] * (1 + stop_loss_pct):
                        true_exit_price = price
                        true_exit_timestamp = timestamp
                        log.info(f"Trade {trade['order_id']} (SELL {trade['symbol']}): Stop-loss hit at {price} on {timestamp}")
                        break
                    if price <= trade['entry_price'] * (1 - take_profit_pct):
                        true_exit_price = price
                        true_exit_timestamp = timestamp
                        log.info(f"Trade {trade['order_id']} (SELL {trade['symbol']}): Take-profit hit at {price} on {timestamp}")
                        break
            
            if true_exit_price and true_exit_timestamp:
                # Recalculate PnL with the correct formula
                if trade['side'] == 'BUY':
                    pnl = (true_exit_price - trade['entry_price']) * trade['quantity']
                else: # SELL
                    pnl = (trade['entry_price'] - true_exit_price) * trade['quantity']
                
                corrections.append({
                    'order_id': trade['order_id'],
                    'pnl': pnl,
                    'exit_price': true_exit_price,
                    'exit_timestamp': true_exit_timestamp,
                    'status': 'CLOSED'
                })

        if dry_run:
            log.info("--- DRY RUN SUMMARY ---")
            log.info(f"Would apply {len(corrections)} corrections.")
            if corrections:
                df = pd.DataFrame(corrections)
                print(df)
                total_pnl = df['pnl'].sum()
                log.info(f"Corrected Total PnL would be: ${total_pnl:.2f}")
            else:
                log.info("No corrections to apply.")
        else:
            # Apply the corrections to the database
            log.info(f"Applying {len(corrections)} corrections to the database...")
            cursor = conn.cursor()
            for corr in corrections:
                cursor.execute(
                    "UPDATE trades SET pnl = %s, exit_price = %s, exit_timestamp = %s, status = %s WHERE order_id = %s",
                    (corr['pnl'], corr['exit_price'], corr['exit_timestamp'], corr['status'], corr['order_id'])
                )
            conn.commit()
            cursor.close()
            log.info("✅ Historical trade re-simulation and correction complete.")

    except Exception as e:
        log.error(f"An error occurred during re-simulation: {e}", exc_info=True)
        if conn and not dry_run:
            conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-simulate and correct historical trade data.")
    parser.add_argument("--db-url", required=True, help="The PostgreSQL database connection URL.")
    parser.add_argument("--dry-run", action='store_true', help="Run the script without making any changes to the database.")
    args = parser.parse_args()
    resimulate_trades(args.db_url, args.dry_run)
