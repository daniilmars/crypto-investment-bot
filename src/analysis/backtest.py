import argparse
import math
import numpy as np
import pandas as pd
import sys
import os

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config import app_config
from src.database import get_db_connection
from src.analysis.signal_engine import generate_signal
from src.analysis.technical_indicators import (
    calculate_rsi, detect_market_regime, multi_timeframe_confirmation,
)
from src.logger import log

# --- Constants ---
FEE_RATE = 0.001
DEFAULT_SLIPPAGE_BPS = 5  # 5 basis points (0.05%) default slippage
DEFAULT_PARALLEL_WORKERS = 4


# ---------------------------------------------------------------------------
# Parallel Signal Pre-computation
# ---------------------------------------------------------------------------

def _compute_signals_for_symbol(args):
    """Worker function: compute signals for one symbol across all its rows.

    Runs in a subprocess via fork.  Returns (symbol, [signal_data_list])
    where each entry corresponds to a row in the symbol's price DataFrame.
    """
    symbol, sym_prices_list, sym_ts_ns_list, params_dict = args

    # Reconstruct DataFrame inside the worker
    sym_df = pd.DataFrame({
        'timestamp': pd.to_datetime(sym_ts_ns_list, unit='ns', utc=True),
        'price': sym_prices_list,
    })

    from argparse import Namespace
    params = Namespace(**params_dict)
    strategy = Strategy(params)

    results = []
    for i in range(len(sym_df)):
        price = sym_df.iloc[i]['price']
        hist = sym_df.iloc[:i + 1]
        signal_data = strategy.generate_signals(symbol, hist, price)
        results.append(signal_data)

    return symbol, results


# ---------------------------------------------------------------------------
# Risk Metrics
# ---------------------------------------------------------------------------

def calculate_risk_metrics(equity_curve: list, trade_history: list,
                           initial_capital: float, risk_free_rate: float = 0.0,
                           bar_interval_minutes: int = 60) -> dict:
    """
    Calculates comprehensive risk-adjusted performance metrics from a backtest.

    Args:
        equity_curve: list of {'timestamp': ..., 'value': ...} dicts.
        trade_history: list of {'symbol', 'side', 'pnl'} dicts.
        initial_capital: starting capital.
        risk_free_rate: annualized risk-free rate (default 0).

    Returns:
        dict with: sharpe_ratio, sortino_ratio, max_drawdown, max_drawdown_pct,
                   profit_factor, calmar_ratio, avg_trade_pnl, total_return_pct,
                   win_rate, total_trades, avg_win, avg_loss.
    """
    if not equity_curve or len(equity_curve) < 2:
        return _empty_metrics()

    values = pd.Series([e['value'] for e in equity_curve], dtype=float)
    returns = values.pct_change().dropna()

    # --- Return metrics ---
    total_return = (values.iloc[-1] - initial_capital) / initial_capital
    total_return_pct = total_return * 100

    # --- Drawdown ---
    cummax = values.cummax()
    drawdowns = (values - cummax) / cummax
    max_drawdown_pct = float(drawdowns.min()) * 100  # negative number
    max_drawdown = float((values - cummax).min())

    # --- Sharpe Ratio (annualized) ---
    periods_per_year = int(365 * 24 * 60 / bar_interval_minutes)
    excess_returns = returns - risk_free_rate / periods_per_year
    sharpe = float('nan')
    if len(returns) > 1 and returns.std() > 0:
        sharpe = float(excess_returns.mean() / returns.std() * math.sqrt(periods_per_year))

    # --- Sortino Ratio (only penalizes downside volatility) ---
    downside = returns[returns < 0]
    sortino = float('nan')
    if len(downside) > 1 and downside.std() > 0:
        sortino = float(excess_returns.mean() / downside.std() * math.sqrt(periods_per_year))

    # --- Trade-level metrics ---
    pnls = [t['pnl'] for t in trade_history]
    num_trades = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / num_trades * 100 if num_trades > 0 else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    avg_trade = sum(pnls) / num_trades if num_trades > 0 else 0.0

    # Profit factor = gross profit / gross loss
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Calmar ratio = annualized return / max drawdown
    calmar = float('nan')
    if max_drawdown_pct < 0:
        calmar = total_return_pct / abs(max_drawdown_pct)

    return {
        'total_return_pct': round(total_return_pct, 2),
        'sharpe_ratio': round(sharpe, 3) if not math.isnan(sharpe) else None,
        'sortino_ratio': round(sortino, 3) if not math.isnan(sortino) else None,
        'max_drawdown_pct': round(max_drawdown_pct, 2),
        'max_drawdown': round(max_drawdown, 2),
        'profit_factor': round(profit_factor, 3),
        'calmar_ratio': round(calmar, 3) if not math.isnan(calmar) else None,
        'total_trades': num_trades,
        'win_rate': round(win_rate, 2),
        'avg_trade_pnl': round(avg_trade, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
    }


def _empty_metrics():
    return {
        'total_return_pct': 0.0, 'sharpe_ratio': None, 'sortino_ratio': None,
        'max_drawdown_pct': 0.0, 'max_drawdown': 0.0, 'profit_factor': 0.0,
        'calmar_ratio': None, 'total_trades': 0, 'win_rate': 0.0,
        'avg_trade_pnl': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
    }


# ---------------------------------------------------------------------------
# Data Loader
# ---------------------------------------------------------------------------

class DataLoader:
    """Handles loading of historical data."""
    @staticmethod
    def load_historical_data():
        log.info("Loading historical data...")
        conn = get_db_connection()
        prices_df = pd.read_sql_query("SELECT * FROM market_prices ORDER BY timestamp ASC", conn)
        conn.close()
        if not prices_df.empty:
            prices_df['timestamp'] = pd.to_datetime(prices_df['timestamp'])
            prices_df['timestamp'] = prices_df['timestamp'].dt.tz_localize('UTC')
        if not prices_df.empty:
            assert prices_df['timestamp'].is_monotonic_increasing, \
                "Historical prices must be sorted by timestamp ASC"
        log.info(f"Loaded {len(prices_df)} price records.")
        return prices_df


# ---------------------------------------------------------------------------
# Portfolio (with slippage)
# ---------------------------------------------------------------------------

class Portfolio:
    """Manages portfolio state and performance tracking."""
    def __init__(self, initial_capital, slippage_bps=DEFAULT_SLIPPAGE_BPS):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}
        self.trade_history = []
        self.equity_curve = []
        self.slippage_bps = slippage_bps
        # Trailing stop state: symbol -> peak price
        self._trailing_peaks = {}
        # Per-position metadata (entry conditions + running MFE/MAE)
        self._position_meta = {}

    def _apply_slippage(self, price, side):
        """Applies slippage: worse fill for entries, worse fill for exits."""
        slip = price * self.slippage_bps / 10000
        if side in ('BUY', 'CLOSE_SHORT'):
            return price + slip  # pay more when buying
        else:
            return price - slip  # receive less when selling

    def get_total_value(self, current_prices):
        total_value = self.cash
        for symbol, pos in self.positions.items():
            cp = current_prices.get(symbol, pos['entry_price'])
            if pos['side'] == 'LONG':
                total_value += pos['quantity'] * cp
            else:  # SHORT
                total_value += pos['margin'] + (pos['entry_price'] - cp) * pos['quantity']
        return total_value

    def place_order(self, symbol, side, quantity, price, timestamp,
                    exit_reason=None):
        fill_price = self._apply_slippage(price, side)
        fee = quantity * fill_price * FEE_RATE
        if side == 'BUY' and self.cash >= quantity * fill_price + fee:
            self.cash -= (quantity * fill_price + fee)
            self.positions[symbol] = {
                'side': 'LONG', 'quantity': quantity, 'entry_price': fill_price,
                'entry_timestamp': timestamp,
            }
            self._trailing_peaks[symbol] = fill_price
        elif side == 'SHORT' and self.cash >= quantity * fill_price + fee:
            margin = quantity * fill_price
            self.cash -= (margin + fee)
            self.positions[symbol] = {
                'side': 'SHORT', 'quantity': quantity, 'entry_price': fill_price,
                'margin': margin, 'entry_timestamp': timestamp,
            }
        elif side == 'CLOSE' and symbol in self.positions:
            pos = self.positions.pop(symbol)
            close_slip = 'CLOSE_SHORT' if pos['side'] == 'SHORT' else 'CLOSE'
            actual_fill = self._apply_slippage(price, close_slip)
            fee = pos['quantity'] * actual_fill * FEE_RATE
            if pos['side'] == 'LONG':
                revenue = pos['quantity'] * actual_fill
                pnl = (actual_fill - pos['entry_price']) * pos['quantity'] - fee
                self.cash += (revenue - fee)
            else:  # close SHORT
                pnl = (pos['entry_price'] - actual_fill) * pos['quantity'] - fee
                self.cash += (pos['margin'] + pnl)
            # Attach entry metadata + MFE/MAE
            meta = self._position_meta.pop(symbol, {})
            trade_record = {
                'symbol': symbol, 'side': pos['side'], 'pnl': pnl,
                'entry_price': pos['entry_price'], 'exit_price': actual_fill,
                'entry_time': pos['entry_timestamp'], 'exit_time': timestamp,
                'exit_reason': exit_reason or 'unknown',
                'rsi_at_entry': meta.get('rsi_at_entry'),
                'sma_alignment': meta.get('sma_alignment'),
                'regime': meta.get('regime'),
                'effective_risk': meta.get('effective_risk'),
                'mfe': meta.get('mfe', 0.0),
                'mae': meta.get('mae', 0.0),
            }
            self.trade_history.append(trade_record)
            self._trailing_peaks.pop(symbol, None)

    def set_entry_meta(self, symbol, meta: dict):
        """Store entry-time metadata for a position (RSI, SMA, regime, etc.)."""
        meta.setdefault('mfe', 0.0)
        meta.setdefault('mae', 0.0)
        self._position_meta[symbol] = meta

    def update_mfe_mae(self, symbol, current_price):
        """Update Max Favorable / Max Adverse Excursion for an open position."""
        if symbol not in self.positions or symbol not in self._position_meta:
            return
        pos = self.positions[symbol]
        entry = pos['entry_price']
        if pos['side'] == 'LONG':
            pnl_pct = (current_price - entry) / entry
        else:
            pnl_pct = (entry - current_price) / entry
        meta = self._position_meta[symbol]
        meta['mfe'] = max(meta['mfe'], pnl_pct)
        meta['mae'] = min(meta['mae'], pnl_pct)

    def update_trailing_peak(self, symbol, current_price):
        """Updates and returns the peak price for trailing stop."""
        prev = self._trailing_peaks.get(symbol, current_price)
        new_peak = max(prev, current_price)
        self._trailing_peaks[symbol] = new_peak
        return new_peak

    def record_equity(self, timestamp, current_prices):
        self.equity_curve.append({'timestamp': timestamp, 'value': self.get_total_value(current_prices)})


# ---------------------------------------------------------------------------
# Strategy (with regime detection + multi-TF)
# ---------------------------------------------------------------------------

class Strategy:
    """Generates trading signals with regime detection and multi-timeframe confirmation."""
    def __init__(self, params):
        self.params = params

    def generate_signals(self, symbol, historical_prices, current_price):
        sma_period = self.params.sma_period
        rsi_period = self.params.rsi_period
        if len(historical_prices) < max(sma_period, rsi_period):
            log.debug(f"[{symbol}] HOLD: Not enough data ({len(historical_prices)} points).")
            return {'signal': 'HOLD', 'regime': 'unknown', 'mtf_direction': 'mixed'}

        price_list = historical_prices['price'].tolist()
        sma = historical_prices['price'].rolling(window=sma_period).mean().iloc[-1]
        rsi = calculate_rsi(price_list, period=rsi_period)
        market_data = {'current_price': current_price, 'sma': sma, 'rsi': rsi}

        signal = generate_signal(
            symbol=symbol,
            market_data=market_data,
            rsi_overbought_threshold=self.params.rsi_overbought_threshold,
            rsi_oversold_threshold=self.params.rsi_oversold_threshold,
            historical_prices=price_list,
            signal_threshold=getattr(self.params, 'signal_threshold', 3),
            signal_mode=getattr(self.params, 'signal_mode', 'scoring'),
            sentiment_config=getattr(self.params, 'sentiment_config', None),
        )

        # --- Market Regime Detection ---
        regime_data = detect_market_regime(price_list)
        regime = regime_data.get('regime', 'ranging')
        regime_params = regime_data.get('strategy_params', {})

        # --- Multi-Timeframe Confirmation ---
        mtf = multi_timeframe_confirmation(price_list, sma_period=sma_period, rsi_period=rsi_period)
        mtf_direction = mtf['confirmed_direction']

        # --- Filter signals based on regime + MTF ---
        original = signal.get('signal')
        if original in ('BUY', 'SELL'):
            signal_direction = 'bullish' if original == 'BUY' else 'bearish'

            if regime == 'volatile' and mtf['agreement_count'] < 3:
                signal['signal'] = 'HOLD'
            elif mtf_direction == 'mixed':
                signal['signal'] = 'HOLD'
            elif mtf_direction != signal_direction:
                signal['signal'] = 'HOLD'

        signal['regime'] = regime
        signal['regime_params'] = regime_params
        signal['mtf_direction'] = mtf_direction
        signal['rsi'] = rsi
        signal['sma_alignment'] = 'above' if current_price > sma else 'below'
        return signal


# ---------------------------------------------------------------------------
# Backtester (with trailing stop, warm-up, Kelly sizing)
# ---------------------------------------------------------------------------

class Backtester:
    """Orchestrates the backtesting simulation with all advanced features."""
    def __init__(self, watch_list, prices_df, params):
        self.watch_list = watch_list
        self.prices_df = prices_df
        self.params = params
        self.portfolio = Portfolio(
            params.initial_capital,
            slippage_bps=getattr(params, 'slippage_bps', DEFAULT_SLIPPAGE_BPS),
        )
        self.strategy = Strategy(params)
        # Warm-up: skip this many bars before allowing trades
        self.warmup_bars = max(params.sma_period, params.rsi_period, 30)
        # Trailing stop params
        self.trailing_stop_enabled = getattr(params, 'trailing_stop_enabled', True)
        self.trailing_stop_activation = getattr(params, 'trailing_stop_activation', 0.02)
        self.trailing_stop_distance = getattr(params, 'trailing_stop_distance', 0.015)
        # Volume gate
        self.volume_gate_enabled = getattr(params, 'volume_gate_enabled', True)
        self.volume_gate_period = getattr(params, 'volume_gate_period', 20)
        # Stop-loss cooldown (bars)
        self.stoploss_cooldown_bars = getattr(params, 'stoploss_cooldown_bars', 6)
        self._stoploss_cooldowns = {}  # symbol -> bar index when cooldown expires
        # Kelly state (updated as trades accumulate)
        self._trade_count = 0
        self._wins = 0
        self._total_win_pnl = 0.0
        self._total_loss_pnl = 0.0
        # Pre-computed signal cache: {symbol: {timestamp: signal_data}}
        self._signal_cache = None
        # Pre-split price data by symbol (avoids O(n) filter per call)
        self._symbol_dfs = {}
        for sym in self.watch_list:
            self._symbol_dfs[sym] = self.prices_df[
                self.prices_df['symbol'] == sym
            ].reset_index(drop=True)

    def precompute_signals_parallel(self, n_workers=DEFAULT_PARALLEL_WORKERS):
        """Pre-compute signals for all symbols using multiprocessing.

        Call this before run() to parallelise the expensive signal generation.
        Uses fork context on macOS/Linux for fast startup (no re-import).
        """
        import multiprocessing as mp

        ctx = mp.get_context('fork')

        # Serialize params as a plain dict (picklable)
        params_dict = vars(self.params)

        tasks = []
        for sym in self.watch_list:
            sym_df = self._symbol_dfs[sym]
            if sym_df.empty:
                continue
            tasks.append((
                sym,
                sym_df['price'].tolist(),
                sym_df['timestamp'].astype('int64').tolist(),  # nanoseconds
                params_dict,
            ))

        log.info(f"Pre-computing signals for {len(tasks)} symbols "
                 f"using {n_workers} workers...")
        with ctx.Pool(n_workers) as pool:
            results = pool.map(_compute_signals_for_symbol, tasks)

        # Store as list per symbol + build timestamp-to-index mapping
        self._signal_cache = {}
        self._signal_cache_idx = {}
        for sym, signal_list in results:
            self._signal_cache[sym] = signal_list
            sym_df = self._symbol_dfs[sym]
            self._signal_cache_idx[sym] = {
                ts: i for i, ts in enumerate(sym_df['timestamp'])
            }

        total_sigs = sum(len(v) for v in self._signal_cache.values())
        log.info(f"Signal pre-computation done: {total_sigs} signals cached.")

    def _get_effective_risk(self, regime_params):
        """Returns risk fraction: Kelly-based if enough history, else fixed."""
        if self._trade_count >= 10 and self._wins > 0:
            losses_count = self._trade_count - self._wins
            avg_win = self._total_win_pnl / self._wins if self._wins > 0 else 0.0
            avg_loss = abs(self._total_loss_pnl / losses_count) if losses_count > 0 else 0.0
            win_rate = self._wins / self._trade_count

            if avg_loss > 0:
                wl_ratio = avg_win / avg_loss
                kelly = win_rate - (1 - win_rate) / wl_ratio
                kelly = max(0.0, min(kelly * 0.5, 0.25))  # half-Kelly, capped
                if kelly > 0:
                    risk_mult = regime_params.get('risk_multiplier', 1.0)
                    return kelly * risk_mult

        risk_mult = regime_params.get('risk_multiplier', 1.0)
        return self.params.trade_risk_percentage * risk_mult

    def _update_kelly_state(self, pnl):
        """Updates running Kelly statistics after each closed trade."""
        self._trade_count += 1
        if pnl > 0:
            self._wins += 1
            self._total_win_pnl += pnl
        else:
            self._total_loss_pnl += pnl

    def run(self):
        log.info("\n--- Starting Backtest Simulation ---")
        log.info(f"Warm-up period: {self.warmup_bars} bars")
        log.info(f"Trailing stop: {'enabled' if self.trailing_stop_enabled else 'disabled'} "
                 f"(activation={self.trailing_stop_activation}, distance={self.trailing_stop_distance})")
        log.info(f"Slippage: {self.portfolio.slippage_bps} bps")

        all_prices = self.prices_df.pivot(index='timestamp', columns='symbol', values='price').ffill()
        assert all_prices.index.is_monotonic_increasing, \
            "Pivoted prices must maintain chronological order"

        # Volume data for volume gate (gracefully absent for crypto)
        self._all_volumes = None
        if 'volume' in self.prices_df.columns:
            self._all_volumes = self.prices_df.pivot(index='timestamp', columns='symbol', values='volume').ffill()

        for bar_idx, (timestamp, prices) in enumerate(all_prices.iterrows()):
            current_prices = prices.to_dict()
            self.portfolio.record_equity(timestamp, current_prices)
            self.check_for_exits(current_prices, timestamp, bar_idx)

            # Skip entries during warm-up period
            if bar_idx < self.warmup_bars:
                continue

            if len(self.portfolio.positions) < self.params.max_concurrent_positions:
                self.check_for_entries(current_prices, timestamp, bar_idx)

        return self.get_results()

    def check_for_exits(self, current_prices, timestamp, bar_idx=0):
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions[symbol]
            current_price = current_prices.get(symbol)
            if current_price is None:
                continue

            # Track MFE/MAE every bar
            self.portfolio.update_mfe_mae(symbol, current_price)

            entry_price = pos['entry_price']
            if pos['side'] == 'LONG':
                pnl_percentage = (current_price - entry_price) / entry_price
            else:  # SHORT
                pnl_percentage = (entry_price - current_price) / entry_price

            # --- Trailing Stop (LONG positions only) ---
            if self.trailing_stop_enabled and pos['side'] == 'LONG':
                peak = self.portfolio.update_trailing_peak(symbol, current_price)
                if pnl_percentage >= self.trailing_stop_activation:
                    drawdown_from_peak = (peak - current_price) / peak if peak > 0 else 0
                    if drawdown_from_peak >= self.trailing_stop_distance:
                        log.debug(f"[{timestamp}] TRAILING STOP '{symbol}': "
                                  f"peak=${peak:.2f}, now=${current_price:.2f}")
                        self.portfolio.place_order(symbol, 'CLOSE', pos['quantity'],
                                                   current_price, timestamp,
                                                   exit_reason='trailing_stop')
                        self._update_kelly_state(self.portfolio.trade_history[-1]['pnl'])
                        continue

            # --- Fixed stop-loss / take-profit ---
            if pnl_percentage <= -self.params.stop_loss_percentage:
                log.debug(f"[{timestamp}] STOP-LOSS '{symbol}' ({pos['side']}): PnL% {pnl_percentage:.2%}")
                self.portfolio.place_order(symbol, 'CLOSE', pos['quantity'],
                                           current_price, timestamp,
                                           exit_reason='stop_loss')
                self._update_kelly_state(self.portfolio.trade_history[-1]['pnl'])
                # Set stop-loss cooldown
                if self.stoploss_cooldown_bars > 0:
                    self._stoploss_cooldowns[symbol] = bar_idx + self.stoploss_cooldown_bars
            elif pnl_percentage >= self.params.take_profit_percentage:
                log.debug(f"[{timestamp}] TAKE-PROFIT '{symbol}' ({pos['side']}): PnL% {pnl_percentage:.2%}")
                self.portfolio.place_order(symbol, 'CLOSE', pos['quantity'],
                                           current_price, timestamp,
                                           exit_reason='take_profit')
                self._update_kelly_state(self.portfolio.trade_history[-1]['pnl'])

    def check_for_entries(self, current_prices, timestamp, bar_idx=0):
        for symbol in self.watch_list:
            if symbol in self.portfolio.positions:
                continue
            current_price = current_prices.get(symbol)
            if pd.isna(current_price):
                continue

            # --- Stop-loss cooldown check ---
            if symbol in self._stoploss_cooldowns:
                if bar_idx < self._stoploss_cooldowns[symbol]:
                    continue
                del self._stoploss_cooldowns[symbol]

            # --- Generate Signal (with regime + MTF filtering) ---
            if self._signal_cache and symbol in self._signal_cache:
                idx = self._signal_cache_idx[symbol].get(timestamp)
                if idx is None:
                    continue
                signal_data = self._signal_cache[symbol][idx]
            else:
                sym_df = self._symbol_dfs.get(symbol)
                if sym_df is not None:
                    historical_prices = sym_df[sym_df['timestamp'] <= timestamp]
                else:
                    historical_prices = self.prices_df[
                        (self.prices_df['symbol'] == symbol) & (self.prices_df['timestamp'] <= timestamp)
                    ]
                signal_data = self.strategy.generate_signals(
                    symbol, historical_prices, current_price,
                )

            signal = signal_data.get('signal')
            regime_params = signal_data.get('regime_params', {})

            # --- Volume gate: skip entry if volume below N-bar average ---
            if signal in ('BUY', 'SELL') and self.volume_gate_enabled and self._all_volumes is not None:
                if symbol in self._all_volumes.columns:
                    vol_series = self._all_volumes[symbol].iloc[:bar_idx + 1].dropna()
                    if len(vol_series) >= self.volume_gate_period:
                        vol_avg = vol_series.iloc[-self.volume_gate_period:].mean()
                        if vol_series.iloc[-1] < vol_avg:
                            log.debug(f"[{timestamp}] Volume gate blocked {signal} for '{symbol}': "
                                      f"vol={vol_series.iloc[-1]:.0f} < avg={vol_avg:.0f}")
                            signal = 'HOLD'

            # --- Dynamic position sizing ---
            effective_risk = self._get_effective_risk(regime_params)

            if signal in ('BUY', 'SELL'):
                entry_meta = {
                    'rsi_at_entry': signal_data.get('rsi'),
                    'sma_alignment': signal_data.get('sma_alignment'),
                    'regime': signal_data.get('regime'),
                    'effective_risk': effective_risk,
                }

            if signal == 'BUY':
                log.debug(f"[{timestamp}] ENTRY '{symbol}' LONG (risk={effective_risk:.4f})")
                capital_to_risk = self.portfolio.cash * effective_risk
                quantity = capital_to_risk / current_price
                self.portfolio.place_order(symbol, 'BUY', quantity, current_price, timestamp)
                self.portfolio.set_entry_meta(symbol, entry_meta)
            elif signal == 'SELL':
                log.debug(f"[{timestamp}] ENTRY '{symbol}' SHORT (risk={effective_risk:.4f})")
                capital_to_risk = self.portfolio.cash * effective_risk
                quantity = capital_to_risk / current_price
                self.portfolio.place_order(symbol, 'SHORT', quantity, current_price, timestamp)
                self.portfolio.set_entry_meta(symbol, entry_meta)

    def get_results(self, include_trades=False) -> dict:
        """Returns full results dict with risk metrics.

        If include_trades=True, adds 'trades' key with full trade_history list
        (each trade has entry conditions, MFE/MAE, exit_reason).
        """
        bar_interval = getattr(self.params, 'bar_interval_minutes', 60)
        metrics = calculate_risk_metrics(
            self.portfolio.equity_curve,
            self.portfolio.trade_history,
            self.params.initial_capital,
            bar_interval_minutes=bar_interval,
        )
        final_value = self.portfolio.equity_curve[-1]['value'] if self.portfolio.equity_curve else self.params.initial_capital
        total_pnl = final_value - self.params.initial_capital
        metrics['final_value'] = round(final_value, 2)
        metrics['total_pnl'] = round(total_pnl, 2)
        metrics['initial_capital'] = self.params.initial_capital
        if include_trades:
            metrics['trades'] = self.portfolio.trade_history
        return metrics

    def print_results(self):
        results = self.get_results()
        log.info("\n--- Backtest Results ---")
        log.info(f"Final Portfolio Value: ${results['final_value']:,.2f}")
        log.info(f"Total PnL: ${results['total_pnl']:,.2f} ({results['total_return_pct']:.2f}%)")
        log.info(f"Total Trades: {results['total_trades']}")
        log.info(f"Win Rate: {results['win_rate']:.2f}%")
        log.info(f"Sharpe Ratio: {results['sharpe_ratio']}")
        log.info(f"Sortino Ratio: {results['sortino_ratio']}")
        log.info(f"Max Drawdown: {results['max_drawdown_pct']:.2f}%")
        log.info(f"Profit Factor: {results['profit_factor']:.3f}")
        log.info(f"Calmar Ratio: {results['calmar_ratio']}")
        log.info(f"Avg Trade PnL: ${results['avg_trade_pnl']:.2f}")
        log.info(f"Avg Win: ${results['avg_win']:.2f} | Avg Loss: ${results['avg_loss']:.2f}")
        # Standardized output for parsing
        print(f"Final PnL: {results['total_pnl']:.2f}")


# ---------------------------------------------------------------------------
# Walk-Forward Validation
# ---------------------------------------------------------------------------

def run_walk_forward(prices_df, params, n_splits=3):
    """
    Walk-forward analysis: splits data into n_splits windows, trains on each
    window, tests on the next. Prevents overfitting by validating out-of-sample.

    Args:
        prices_df: Full historical price DataFrame.
        params: argparse Namespace with strategy parameters.
        n_splits: Number of train/test windows (default 3).

    Returns:
        dict with per-fold and aggregate metrics.
    """
    timestamps = prices_df['timestamp'].sort_values().unique()
    total_bars = len(timestamps)

    # Each fold: 60% train, 40% test (overlapping windows)
    fold_size = total_bars // (n_splits + 1)
    train_size = int(fold_size * 1.5)

    fold_results = []
    all_equity = []

    for fold in range(n_splits):
        train_start_idx = fold * fold_size
        train_end_idx = min(train_start_idx + train_size, total_bars - fold_size)
        test_start_idx = train_end_idx
        test_end_idx = min(test_start_idx + fold_size, total_bars)

        if test_end_idx <= test_start_idx:
            break

        train_end_ts = timestamps[train_end_idx]
        test_start_ts = timestamps[test_start_idx]
        test_end_ts = timestamps[test_end_idx - 1]

        test_prices = prices_df[
            (prices_df['timestamp'] >= test_start_ts) & (prices_df['timestamp'] <= test_end_ts)
        ].copy()

        if test_prices.empty:
            continue

        watchlist = test_prices['symbol'].unique().tolist()
        bt = Backtester(watchlist, test_prices, params)
        fold_result = bt.run()
        fold_result['fold'] = fold + 1
        fold_result['test_start'] = str(test_start_ts)
        fold_result['test_end'] = str(test_end_ts)
        fold_results.append(fold_result)

        log.info(f"Fold {fold + 1}: PnL=${fold_result['total_pnl']:.2f}, "
                 f"Sharpe={fold_result.get('sharpe_ratio')}, "
                 f"MaxDD={fold_result.get('max_drawdown_pct'):.2f}%")

    # --- Aggregate metrics ---
    if not fold_results:
        return {'folds': [], 'aggregate': _empty_metrics()}

    avg_return = np.mean([f['total_return_pct'] for f in fold_results])
    avg_sharpe = np.mean([f['sharpe_ratio'] for f in fold_results if f['sharpe_ratio'] is not None])
    avg_max_dd = np.mean([f['max_drawdown_pct'] for f in fold_results])
    total_trades = sum(f['total_trades'] for f in fold_results)
    avg_win_rate = np.mean([f['win_rate'] for f in fold_results if f['total_trades'] > 0])
    consistency = sum(1 for f in fold_results if f['total_pnl'] > 0) / len(fold_results) * 100

    aggregate = {
        'avg_return_pct': round(float(avg_return), 2),
        'avg_sharpe': round(float(avg_sharpe), 3) if not np.isnan(avg_sharpe) else None,
        'avg_max_drawdown_pct': round(float(avg_max_dd), 2),
        'total_trades': total_trades,
        'avg_win_rate': round(float(avg_win_rate), 2) if not np.isnan(avg_win_rate) else 0.0,
        'fold_consistency_pct': round(consistency, 1),
    }

    log.info(f"\n--- Walk-Forward Summary ({n_splits} folds) ---")
    log.info(f"Avg Return: {aggregate['avg_return_pct']:.2f}%")
    log.info(f"Avg Sharpe: {aggregate['avg_sharpe']}")
    log.info(f"Avg Max DD: {aggregate['avg_max_drawdown_pct']:.2f}%")
    log.info(f"Fold Consistency: {aggregate['fold_consistency_pct']:.0f}% profitable")

    return {'folds': fold_results, 'aggregate': aggregate}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run a backtest of the crypto trading bot.")
    # Portfolio & Risk
    parser.add_argument('--initial-capital', type=float, default=app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0))
    parser.add_argument('--trade-risk-percentage', type=float, default=app_config.get('settings', {}).get('trade_risk_percentage', 0.01))
    parser.add_argument('--stop-loss-percentage', type=float, default=app_config.get('settings', {}).get('stop_loss_percentage', 0.02))
    parser.add_argument('--take-profit-percentage', type=float, default=app_config.get('settings', {}).get('take_profit_percentage', 0.05))
    parser.add_argument('--max-concurrent-positions', type=int, default=app_config.get('settings', {}).get('max_concurrent_positions', 3))

    # Signal quality
    parser.add_argument('--signal-threshold', type=int, default=app_config.get('settings', {}).get('signal_threshold', 3), help='Min indicators to agree for BUY/SELL')
    parser.add_argument('--volume-gate-enabled', action='store_true', default=app_config.get('settings', {}).get('volume_gate_enabled', True))
    parser.add_argument('--no-volume-gate', dest='volume_gate_enabled', action='store_false')
    parser.add_argument('--volume-gate-period', type=int, default=app_config.get('settings', {}).get('volume_gate_period', 20), help='Bars for volume moving average')
    parser.add_argument('--stoploss-cooldown-bars', type=int, default=app_config.get('settings', {}).get('stoploss_cooldown_hours', 6), help='Bars to wait after SL before re-entry')

    # New features
    parser.add_argument('--slippage-bps', type=float, default=DEFAULT_SLIPPAGE_BPS, help='Slippage in basis points')
    parser.add_argument('--trailing-stop-enabled', type=bool, default=True)
    parser.add_argument('--trailing-stop-activation', type=float, default=0.02)
    parser.add_argument('--trailing-stop-distance', type=float, default=0.015)

    # Technical Indicators
    parser.add_argument('--sma-period', type=int, default=app_config.get('settings', {}).get('sma_period', 20))
    parser.add_argument('--rsi-period', type=int, default=app_config.get('settings', {}).get('rsi_period', 14))
    parser.add_argument('--rsi-overbought-threshold', type=int, default=app_config.get('settings', {}).get('rsi_overbought_threshold', 70))
    parser.add_argument('--rsi-oversold-threshold', type=int, default=app_config.get('settings', {}).get('rsi_oversold_threshold', 30))

    # Data
    parser.add_argument('--bar-interval-minutes', type=int, default=60, help='Bar interval in minutes (15 for 15m, 60 for 1h)')

    # Signal mode
    parser.add_argument('--signal-mode', type=str, default='scoring',
                        choices=['scoring', 'sentiment'],
                        help='Signal generation mode (default: scoring). '
                             'Note: sentiment mode requires live Gemini data, '
                             'so backtests default to scoring.')

    # Mode
    parser.add_argument('--walk-forward', action='store_true', help='Run walk-forward validation instead of single backtest')
    parser.add_argument('--walk-forward-splits', type=int, default=3, help='Number of walk-forward folds')

    args = parser.parse_args()

    prices = DataLoader.load_historical_data()
    if prices.empty:
        log.info("No data found. Exiting backtest.")
        return

    if args.walk_forward:
        run_walk_forward(prices, args, n_splits=args.walk_forward_splits)
    else:
        watchlist = prices['symbol'].unique().tolist()
        backtester = Backtester(watchlist, prices, args)
        backtester.run()
        backtester.print_results()

if __name__ == '__main__':
    main()
