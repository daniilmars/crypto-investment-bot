# Signal Confirmation Flow

This document describes the Telegram-based signal confirmation system that gates BUY/SELL trade execution behind manual user approval.

---

## Motivation

The bot previously auto-executed trades immediately when a signal fired, then sent a Telegram notification after the fact. On a Mac Mini-based architecture where the bot runs locally with direct user oversight, this is unnecessarily aggressive. The confirmation flow gives the user a chance to review each signal before capital is committed, while keeping protective exits (stop-loss, take-profit, trailing stop) fully automatic for safety.

---

## Architecture

### Before (auto-execute)

```
Signal Engine -> place_order() -> send_telegram_alert(signal + order_result)
```

### After (confirmation flow)

```
Signal Engine -> send_signal_for_confirmation(signal)
                     |
              [User taps button]
                     |
            Approve -> execute_confirmed_signal() -> edit message with result
            Reject  -> edit message with "Skipped"
            Timeout -> edit message with "Expired"
```

### What requires confirmation

| Event | Requires confirmation? |
|---|---|
| BUY signal (crypto or stock) | Yes (configurable) |
| SELL signal (crypto or stock) | Yes (configurable) |
| Stop-loss exit | No -- automatic |
| Take-profit exit | No -- automatic |
| Trailing stop exit | No -- automatic |
| Position monitor auto-exit | No -- automatic |
| Circuit breaker halt | No -- automatic |

---

## Configuration

In `config/settings.yaml`:

```yaml
settings:
  signal_confirmation:
    enabled: true                    # false = auto-execute (old behavior)
    timeout_minutes: 30              # auto-reject after this
    require_confirmation_for:
      - "BUY"
      - "SELL"
```

Setting `enabled: false` restores the original auto-execute behavior with no code changes needed.

---

## Components

### 1. Pending Signals Store (`telegram_bot.py`)

An in-memory dictionary holds signals awaiting user action:

```python
_pending_signals: dict[int, dict] = {}  # signal_id -> {signal, message_id, chat_id, created_at}
_signal_counter: int = 0                # incrementing ID
```

Each signal gets a short integer ID used in callback data. The store is cleared on approval, rejection, or timeout.

### 2. Callback Data Format

Telegram's `callback_data` field is limited to 64 bytes. We use a compact format:

- `a:42` -- approve signal #42
- `r:42` -- reject signal #42

### 3. Key Functions

| Function | Location | Purpose |
|---|---|---|
| `register_execute_callback(fn)` | `telegram_bot.py` | main.py registers its execution function at startup |
| `is_confirmation_required(signal_type)` | `telegram_bot.py` | Checks config to see if this signal type needs approval |
| `send_signal_for_confirmation(signal)` | `telegram_bot.py` | Sends Telegram message with inline Execute/Skip buttons |
| `_handle_signal_callback(update, ctx)` | `telegram_bot.py` | Processes button press -- executes or rejects the trade |
| `cleanup_expired_signals()` | `telegram_bot.py` | Edits messages for signals past the timeout |
| `execute_confirmed_signal(signal)` | `main.py` | Extracted trade execution logic (crypto + stock, all brokers) |
| `_signal_cleanup_loop()` | `main.py` | Background task that calls cleanup every 60 seconds |

### 4. Authorization

Only Telegram users in the `authorized_user_ids` list can tap the buttons. Unauthorized users see an alert and the signal remains pending.

---

## Message Format

### Pending (with inline buttons)

```
📊 NEW SIGNAL: BUY BTC

💰 Price: $67,432.50
📈 Reason: Gemini bullish (confidence 0.85) + RSI oversold (28.3)
💵 Quantity: 0.004400 BTC ($300.00)
⏱ Expires in 30 min
Crypto signal

[✅ Execute]  [❌ Skip]
```

### After approval

```
✅ EXECUTED: BUY BTC

💰 Fill price: $67,435.00
📈 Reason: Gemini bullish (confidence 0.85) + RSI oversold (28.3)
💵 Quantity: 0.004400 BTC
🎯 TP: $72,829.80 | SL: $65,074.78

Approved by user
```

### After rejection

```
❌ SKIPPED: BUY BTC

💰 Price: $67,432.50
📈 Reason: Gemini bullish (confidence 0.85) + RSI oversold (28.3)

Rejected by user
```

### After timeout

```
⏰ EXPIRED: BUY BTC

💰 Price: $67,432.50
📈 Reason: Gemini bullish (confidence 0.85) + RSI oversold (28.3)

Auto-rejected after 30 min
```

---

## Signal Data Flow

When the signal engine generates a BUY or SELL, the signal dict is enriched before being sent for confirmation:

```python
signal = {
    'signal': 'BUY',
    'symbol': 'BTC',
    'current_price': 67432.50,
    'reason': 'Gemini bullish ...',
    'asset_type': 'crypto',       # or 'stock'
    'quantity': 0.0044,            # pre-calculated by main.py
    'position': {...},             # for SELL: the position being closed
}
```

The `quantity` is calculated by main.py before sending (using Kelly criterion or fixed risk percentage), so the user sees the exact trade size in the confirmation message. For SELL signals, the `position` dict is attached so `execute_confirmed_signal()` knows which order to close.

---

## Execution Path in `execute_confirmed_signal()`

The function handles all combinations:

| asset_type | signal | broker | Action |
|---|---|---|---|
| crypto | BUY | any | `place_order(symbol, "BUY", quantity, price)` |
| crypto | SELL | any | `place_order(symbol, "SELL", qty, price, existing_order_id=...)` + clear trailing stop |
| stock | BUY | alpaca | `place_stock_order(symbol, "BUY", quantity, price)` |
| stock | SELL | alpaca | `place_stock_order(symbol, "SELL", quantity, price)` |
| stock | BUY | paper_only | `place_order(symbol, "BUY", quantity, price, asset_type='stock')` |
| stock | SELL | paper_only | `place_order(symbol, "SELL", qty, price, existing_order_id=..., asset_type='stock')` + clear trailing stop |

---

## Timeout and Cleanup

- A background asyncio task (`_signal_cleanup_loop`) runs every 60 seconds
- It calls `cleanup_expired_signals()` which checks each pending signal's `created_at`
- Signals older than `timeout_minutes` are removed from `_pending_signals`
- Their Telegram messages are edited to show the "EXPIRED" state
- The trade is NOT executed

---

## Error Handling

If `execute_confirmed_signal()` raises an exception during trade execution:

- The signal is removed from `_pending_signals` (it won't be retried)
- The Telegram message is edited to show "EXECUTION FAILED" with the error message
- The error is logged

---

## Testing

17 tests in `tests/test_signal_confirmation.py` covering:

- `is_confirmation_required` -- enabled/disabled/HOLD cases
- `send_signal_for_confirmation` -- inline keyboard structure, counter increment, disabled bot
- Callback handler -- approve (with execution), reject, unknown ID, unauthorized user, execution error, PnL display
- Cleanup -- expired signals edited, fresh signals untouched, empty state
- `register_execute_callback` -- function registration

All tests use `asyncio.get_event_loop().run_until_complete()` to run async code in sync tests (no pytest-asyncio dependency needed).

---

## Disabling the Feature

Set `signal_confirmation.enabled: false` in settings.yaml. All BUY/SELL signals will auto-execute and send alerts as before. No other code changes needed.
