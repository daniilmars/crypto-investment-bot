import subprocess
import itertools
import re
import sys
import os
from multiprocessing import Pool, cpu_count

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.logger import log
from src.database import save_optimization_result, initialize_database

# --- Parameter Grid ---
# Define the range of values to test for each parameter.
param_grid = {
    '--rsi-oversold-threshold': [35],
    '--stop-loss-percentage': [0.02, 0.03, 0.04],
    '--take-profit-percentage': [0.05, 0.08, 0.10]
}

def run_backtest(params):
    """Runs the backtester with a given set of parameters and returns the PnL."""
    command = ['python3', 'src/analysis/backtest.py']
    param_str = " ".join([f"{key}={value}" for key, value in params.items()])
    
    for key, value in params.items():
        command.extend([key, str(value)])
    
    log.info(f"Starting backtest with: {param_str}")
    
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        
        match = re.search(r"Final PnL: (-?\d+\.\d+)", result.stdout)
        if match:
            pnl = float(match.group(1))
            log.info(f"Finished backtest with PnL: {pnl:.2f} for {param_str}")
            return params, pnl
        else:
            log.error(f"Could not parse PnL for {param_str}")
            return params, None
            
    except subprocess.CalledProcessError as e:
        log.error(f"Backtest failed for {param_str}. Stderr: {e.stderr}")
        return params, None

def optimize_strategy():
    """
    Runs a grid search in parallel to find the best strategy parameters.
    """
    initialize_database()
    
    keys, values = zip(*param_grid.items())
    param_combinations = [dict(zip(keys, v)) for v in itertools.product(*values)]
    
    log.info(f"--- Starting Strategy Optimization ---")
    log.info(f"Testing {len(param_combinations)} parameter combinations using up to {cpu_count()} cores.")
    
    # --- Run backtests in parallel ---
    with Pool(processes=cpu_count()) as pool:
        results = pool.map(run_backtest, param_combinations)

    # --- Process and save results ---
    best_pnl = -float('inf')
    best_params = None
    
    for params, pnl in results:
        if pnl is not None:
            save_optimization_result(params, pnl)
            if pnl > best_pnl:
                best_pnl = pnl
                best_params = params

    log.info("\n--- ✅ Optimization Complete ---")
    if best_params:
        log.info(f"Best PnL: ${best_pnl:,.2f}")
        log.info("Best Parameters:")
        for key, value in best_params.items():
            log.info(f"  {key}: {value}")
    else:
        log.warning("Could not determine the best parameters from the optimization run.")

if __name__ == "__main__":
    optimize_strategy()
