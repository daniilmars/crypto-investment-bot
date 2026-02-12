"""
Stock backtesting module.

Reuses Portfolio and calculate_risk_metrics from the crypto backtest module,
with a simplified strategy that wraps generate_stock_signal().

Usage:
    .venv/bin/python -m src.analysis.stock_backtest \
        --symbols AAPL,MSFT --start-date 2024-01-01 --initial-capital 10000
"""

import argparse
import math
import sys
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.analysis.backtest import Portfolio, calculate_risk_metrics, _empty_metrics, DEFAULT_SLIPPAGE_BPS
from src.analysis.stock_signal_engine import generate_stock_signal
from src.analysis.technical_indicators import calculate_rsi, calculate_sma
from src.config import app_config
from src.logger import log


# ---------------------------------------------------------------------------
# Stock Data Loader
# ---------------------------------------------------------------------------

class StockDataLoader:
    """Loads historical stock data from Alpaca or CSV files."""

    @staticmethod
    def load_from_alpaca(symbols, start_date, end_date=None, limit=500):
        """Fetches daily bars from Alpaca Data API."""
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
        except ImportError:
            log.error("alpaca-py not installed. Use CSV mode or install alpaca-py.")
            return pd.DataFrame()

        api_key = app_config.get('api_keys', {}).get('alpaca', {}).get('api_key')
        api_secret = app_config.get('api_keys', {}).get('alpaca', {}).get('api_secret')
        client = StockHistoricalDataClient(api_key or None, api_secret or None)

        if end_date is None:
            end_date = datetime.now()
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y-%m-%d')
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, '%Y-%m-%d')

        all_bars = []
        for symbol in symbols:
            try:
                request = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Day,
                    start=start_date,
                    end=end_date,
                    limit=limit,
                )
                bars = client.get_stock_bars(request)
                bar_list = bars[symbol] if symbol in bars else []
                for bar in bar_list:
                    all_bars.append({
                        'symbol': symbol,
                        'timestamp': bar.timestamp,
                        'open': float(bar.open),
                        'high': float(bar.high),
                        'low': float(bar.low),
                        'close': float(bar.close),
                        'volume': float(bar.volume),
                    })
                log.info(f"Loaded {len(bar_list)} bars for {symbol} from Alpaca.")
            except Exception as e:
                log.error(f"Failed to load Alpaca data for {symbol}: {e}")

        if not all_bars:
            return pd.DataFrame()

        df = pd.DataFrame(all_bars)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        if df['timestamp'].dt.tz is None:
            df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
        return df.sort_values(['timestamp', 'symbol']).reset_index(drop=True)

    @staticmethod
    def load_from_csv(csv_path, symbols=None):
        """
        Loads stock data from a CSV file.
        Expected columns: symbol, timestamp, open, high, low, close, volume
        """
        df = pd.read_csv(csv_path)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        if df['timestamp'].dt.tz is None:
            df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
        if symbols:
            df = df[df['symbol'].isin(symbols)]
        return df.sort_values(['timestamp', 'symbol']).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Stock Strategy
# ---------------------------------------------------------------------------

class StockStrategy:
    """Wraps generate_stock_signal() for backtesting."""

    def __init__(self, params):
        self.params = params

    def generate_signals(self, symbol, price_history, current_price, volume_history=None):
        """
        Generates a stock signal using historical price/volume data.

        Args:
            symbol: Stock ticker
            price_history: list of close prices (oldest -> newest)
            current_price: latest price
            volume_history: list of volumes (oldest -> newest)
        """
        sma_period = getattr(self.params, 'sma_period', 20)
        rsi_period = getattr(self.params, 'rsi_period', 14)

        if len(price_history) < max(sma_period, rsi_period):
            return {'signal': 'HOLD', 'symbol': symbol, 'reason': 'Not enough data',
                    'current_price': current_price}

        sma = calculate_sma(price_history, period=sma_period)
        rsi = calculate_rsi(price_history, period=rsi_period)

        market_data = {'current_price': current_price, 'sma': sma, 'rsi': rsi}

        volume_data = {}
        if volume_history and len(volume_history) > 1:
            current_volume = volume_history[-1]
            avg_volume = sum(volume_history) / len(volume_history)
            price_change = ((current_price - price_history[-2]) / price_history[-2] * 100
                            if len(price_history) >= 2 and price_history[-2] > 0 else 0)
            volume_data = {
                'current_volume': current_volume,
                'avg_volume': avg_volume,
                'price_change_percent': price_change,
            }

        signal = generate_stock_signal(
            symbol=symbol,
            market_data=market_data,
            volume_data=volume_data,
            rsi_overbought_threshold=getattr(self.params, 'rsi_overbought_threshold', 70),
            rsi_oversold_threshold=getattr(self.params, 'rsi_oversold_threshold', 30),
            historical_prices=price_history,
        )
        return signal


# ---------------------------------------------------------------------------
# Stock Backtester
# ---------------------------------------------------------------------------

class StockBacktester:
    """Backtests stock signals using historical daily data."""

    def __init__(self, symbols, prices_df, params):
        self.symbols = symbols
        self.prices_df = prices_df
        self.params = params
        self.portfolio = Portfolio(
            params.initial_capital,
            slippage_bps=getattr(params, 'slippage_bps', DEFAULT_SLIPPAGE_BPS),
        )
        self.strategy = StockStrategy(params)
        self.warmup_bars = max(
            getattr(params, 'sma_period', 20),
            getattr(params, 'rsi_period', 14),
            30
        )
        # Trailing stop params
        self.trailing_stop_enabled = getattr(params, 'trailing_stop_enabled', True)
        self.trailing_stop_activation = getattr(params, 'trailing_stop_activation', 0.02)
        self.trailing_stop_distance = getattr(params, 'trailing_stop_distance', 0.015)
        # Kelly state
        self._trade_count = 0
        self._wins = 0
        self._total_win_pnl = 0.0
        self._total_loss_pnl = 0.0

    def _get_effective_risk(self):
        """Returns risk fraction: Kelly-based if enough history, else fixed."""
        if self._trade_count >= 10 and self._wins > 0:
            losses_count = self._trade_count - self._wins
            avg_win = self._total_win_pnl / self._wins if self._wins > 0 else 0.0
            avg_loss = abs(self._total_loss_pnl / losses_count) if losses_count > 0 else 0.0
            win_rate = self._wins / self._trade_count
            if avg_loss > 0:
                wl_ratio = avg_win / avg_loss
                kelly = win_rate - (1 - win_rate) / wl_ratio
                kelly = max(0.0, min(kelly * 0.5, 0.25))
                if kelly > 0:
                    return kelly
        return self.params.trade_risk_percentage

    def _update_kelly_state(self, pnl):
        self._trade_count += 1
        if pnl > 0:
            self._wins += 1
            self._total_win_pnl += pnl
        else:
            self._total_loss_pnl += pnl

    def run(self):
        log.info("\n--- Starting Stock Backtest ---")
        log.info(f"Symbols: {self.symbols}")
        log.info(f"Warm-up: {self.warmup_bars} bars, Slippage: {self.portfolio.slippage_bps} bps")

        all_prices = self.prices_df.pivot(index='timestamp', columns='symbol', values='close').ffill()

        # Also pivot volumes for signal generation
        all_volumes = None
        if 'volume' in self.prices_df.columns:
            all_volumes = self.prices_df.pivot(index='timestamp', columns='symbol', values='volume').ffill()

        for bar_idx, (timestamp, prices) in enumerate(all_prices.iterrows()):
            current_prices = prices.to_dict()
            self.portfolio.record_equity(timestamp, current_prices)
            self._check_exits(current_prices, timestamp)

            if bar_idx < self.warmup_bars:
                continue

            if len(self.portfolio.positions) < self.params.max_concurrent_positions:
                self._check_entries(current_prices, timestamp, all_prices, all_volumes, bar_idx)

        return self._get_results()

    def _check_exits(self, current_prices, timestamp):
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions[symbol]
            current_price = current_prices.get(symbol)
            if current_price is None or pd.isna(current_price):
                continue

            entry_price = pos['entry_price']
            pnl_pct = (current_price - entry_price) / entry_price

            # Trailing stop
            if self.trailing_stop_enabled and pos['side'] == 'LONG':
                peak = self.portfolio.update_trailing_peak(symbol, current_price)
                if pnl_pct >= self.trailing_stop_activation:
                    dd = (peak - current_price) / peak if peak > 0 else 0
                    if dd >= self.trailing_stop_distance:
                        self.portfolio.place_order(symbol, 'CLOSE', pos['quantity'],
                                                   current_price, timestamp)
                        self._update_kelly_state(self.portfolio.trade_history[-1]['pnl'])
                        continue

            # Fixed SL/TP
            if pnl_pct <= -self.params.stop_loss_percentage:
                self.portfolio.place_order(symbol, 'CLOSE', pos['quantity'],
                                           current_price, timestamp)
                self._update_kelly_state(self.portfolio.trade_history[-1]['pnl'])
            elif pnl_pct >= self.params.take_profit_percentage:
                self.portfolio.place_order(symbol, 'CLOSE', pos['quantity'],
                                           current_price, timestamp)
                self._update_kelly_state(self.portfolio.trade_history[-1]['pnl'])

    def _check_entries(self, current_prices, timestamp, all_prices, all_volumes, bar_idx):
        for symbol in self.symbols:
            if len(self.portfolio.positions) >= self.params.max_concurrent_positions:
                break
            if symbol in self.portfolio.positions:
                continue
            current_price = current_prices.get(symbol)
            if current_price is None or pd.isna(current_price):
                continue

            # Build price history up to this bar
            price_col = all_prices[symbol].iloc[:bar_idx + 1].dropna()
            price_history = price_col.tolist()

            volume_history = None
            if all_volumes is not None and symbol in all_volumes.columns:
                vol_col = all_volumes[symbol].iloc[:bar_idx + 1].dropna()
                volume_history = vol_col.tolist()

            signal = self.strategy.generate_signals(symbol, price_history, current_price, volume_history)

            if signal.get('signal') == 'BUY':
                effective_risk = self._get_effective_risk()
                capital_to_risk = self.portfolio.cash * effective_risk
                quantity = capital_to_risk / current_price
                self.portfolio.place_order(symbol, 'BUY', quantity, current_price, timestamp)

    def _get_results(self):
        bar_interval = getattr(self.params, 'bar_interval_minutes', 390)  # trading day = 390 min
        metrics = calculate_risk_metrics(
            self.portfolio.equity_curve,
            self.portfolio.trade_history,
            self.params.initial_capital,
            bar_interval_minutes=bar_interval,
        )
        final_value = (self.portfolio.equity_curve[-1]['value']
                       if self.portfolio.equity_curve else self.params.initial_capital)
        total_pnl = final_value - self.params.initial_capital
        metrics['final_value'] = round(final_value, 2)
        metrics['total_pnl'] = round(total_pnl, 2)
        metrics['initial_capital'] = self.params.initial_capital
        return metrics

    def print_results(self):
        results = self._get_results()
        log.info("\n--- Stock Backtest Results ---")
        log.info(f"Final Portfolio Value: ${results['final_value']:,.2f}")
        log.info(f"Total PnL: ${results['total_pnl']:,.2f} ({results['total_return_pct']:.2f}%)")
        log.info(f"Total Trades: {results['total_trades']}")
        log.info(f"Win Rate: {results['win_rate']:.2f}%")
        log.info(f"Sharpe Ratio: {results['sharpe_ratio']}")
        log.info(f"Sortino Ratio: {results['sortino_ratio']}")
        log.info(f"Max Drawdown: {results['max_drawdown_pct']:.2f}%")
        log.info(f"Profit Factor: {results['profit_factor']:.3f}")
        log.info(f"Avg Trade PnL: ${results['avg_trade_pnl']:.2f}")
        log.info(f"Avg Win: ${results['avg_win']:.2f} | Avg Loss: ${results['avg_loss']:.2f}")
        print(f"Final PnL: {results['total_pnl']:.2f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run a stock trading backtest.")
    parser.add_argument('--symbols', type=str, default='AAPL,MSFT',
                        help='Comma-separated stock symbols')
    parser.add_argument('--start-date', type=str, default='2024-01-01',
                        help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, default=None,
                        help='End date (YYYY-MM-DD), defaults to today')
    parser.add_argument('--initial-capital', type=float, default=10000.0)
    parser.add_argument('--trade-risk-percentage', type=float, default=0.05)
    parser.add_argument('--stop-loss-percentage', type=float, default=0.07)
    parser.add_argument('--take-profit-percentage', type=float, default=0.10)
    parser.add_argument('--max-concurrent-positions', type=int, default=3)
    parser.add_argument('--slippage-bps', type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument('--trailing-stop-enabled', type=bool, default=True)
    parser.add_argument('--trailing-stop-activation', type=float, default=0.02)
    parser.add_argument('--trailing-stop-distance', type=float, default=0.015)
    parser.add_argument('--sma-period', type=int, default=20)
    parser.add_argument('--rsi-period', type=int, default=14)
    parser.add_argument('--rsi-overbought-threshold', type=int, default=70)
    parser.add_argument('--rsi-oversold-threshold', type=int, default=30)
    parser.add_argument('--csv', type=str, default=None,
                        help='Path to CSV file with historical data (instead of Alpaca)')
    parser.add_argument('--bar-interval-minutes', type=int, default=390)

    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(',')]

    if args.csv:
        log.info(f"Loading data from CSV: {args.csv}")
        prices_df = StockDataLoader.load_from_csv(args.csv, symbols=symbols)
    else:
        log.info(f"Loading data from Alpaca for {symbols}")
        prices_df = StockDataLoader.load_from_alpaca(symbols, args.start_date, args.end_date)

    if prices_df.empty:
        log.error("No data loaded. Exiting.")
        return

    backtester = StockBacktester(symbols, prices_df, args)
    backtester.run()
    backtester.print_results()


if __name__ == '__main__':
    main()
