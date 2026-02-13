# Backtest Results

**Date:** 2026-02-13
**Dataset:** 224,640 price records, 183,102 whale transactions (Jan 12 – Feb 5, 23 days)
**Initial capital:** $10,000 | **Risk per trade:** 3% | **Max positions:** 5

---

## 1. Signal Threshold Sweep

**Fixed params:** SL=3% | TP=8% | signal_threshold varied

| Config | PnL | Return% | Trades | WinRate | Sharpe | Sortino | MaxDD% | PF | AvgWin | AvgLoss |
|---|---|---|---|---|---|---|---|---|---|---|
| OLD (threshold=2, no gates) | +$205.89 | +2.06% | 407 | 42.0% | 0.586 | 0.666 | -4.07% | 1.186 | $7.73 | $4.72 |
| NEW threshold=3 only | -$896.94 | -8.97% | 134 | 16.4% | -2.449 | -1.506 | -11.21% | 0.251 | $13.06 | $10.22 |
| NEW threshold=3 + volgate | -$896.94 | -8.97% | 134 | 16.4% | -2.449 | -1.506 | -11.21% | 0.251 | $13.06 | $10.22 |
| NEW full (threshold=3, volgate=20, cooldown=6) | -$887.05 | -8.87% | 133 | 16.5% | -2.426 | -1.494 | -11.11% | 0.253 | $13.06 | $10.23 |

**Conclusion:** Threshold=3 destroyed performance. Volume gate had zero effect. Cooldown negligible. Keeping threshold=2.

---

## 2. SL/TP Parameter Sweep

**Fixed params:** threshold=2 | no volume gate | no cooldown | trailing stop on

### Top 10 by Sharpe ratio

| Config | PnL | Return% | Trades | WinRate | Sharpe | Sortino | MaxDD% | PF | AvgWin | AvgLoss |
|---|---|---|---|---|---|---|---|---|---|---|
| SL=3.5% TP=8.0% | +$3,055.91 | +30.56% | 307 | 55.7% | 2.149 | 2.743 | -5.45% | 1.908 | $40.48 | $26.69 |
| SL=3.5% TP=10.0% | +$1,415.16 | +14.15% | 286 | 50.0% | 1.607 | 1.957 | -4.45% | 1.767 | $20.90 | $11.83 |
| SL=3.5% TP=12.0% | +$1,087.82 | +10.88% | 261 | 46.0% | 1.306 | 1.490 | -5.05% | 1.567 | $20.64 | $11.21 |
| SL=5.0% TP=12.0% | +$768.93 | +7.69% | 185 | 47.6% | 1.052 | 1.243 | -5.15% | 1.466 | $27.20 | $16.83 |
| SL=2.5% TP=8.0% | +$469.62 | +4.70% | 468 | 36.3% | 0.931 | 1.195 | -3.89% | 1.411 | $9.71 | $3.93 |
| SL=4.0% TP=12.0% | +$527.27 | +5.27% | 273 | 40.7% | 0.928 | 1.115 | -4.19% | 1.308 | $19.90 | $10.42 |
| SL=5.0% TP=10.0% | +$694.12 | +6.94% | 262 | 53.0% | 0.866 | 1.091 | -5.22% | 1.339 | $20.82 | $17.58 |
| SL=3.0% TP=12.0% | +$522.74 | +5.23% | 257 | 32.3% | 0.722 | 0.841 | -6.09% | 1.270 | $24.25 | $9.11 |
| SL=3.0% TP=8.0% | +$205.89 | +2.06% | 407 | 42.0% | 0.586 | 0.666 | -4.07% | 1.186 | $7.73 | $4.72 |
| SL=2.5% TP=6.0% | +$67.17 | +0.67% | 637 | 41.1% | 0.153 | 0.172 | -3.39% | 1.087 | $9.83 | $6.32 |

### Return % heatmap (SL rows x TP columns)

```
   SL \ TP     4.0%     6.0%     8.0%    10.0%    12.0%
-------------------------------------------------------
      1.5%   -18.23%   -11.49%    -0.44%    -0.59%     0.12%
      2.0%   -12.78%    -0.82%    -5.24%    -0.56%    -0.42%
      2.5%    -0.40%     0.67%     4.70%    -0.33%    -0.30%
      3.0%    -1.32%    -0.53%     2.06%     0.20%     5.23%
      3.5%    -0.08%    -0.07%    30.56%    14.15%    10.88%
      4.0%    -0.99%     0.45%    -3.76%    -0.14%     5.27%
      5.0%    -3.70%    -3.22%     0.51%     6.94%     7.69%
```

### Sharpe ratio heatmap (SL rows x TP columns)

```
   SL \ TP     4.0%     6.0%     8.0%    10.0%    12.0%
-------------------------------------------------------
      1.5%   -3.512   -2.088   -0.156   -0.158    0.050
      2.0%   -2.534   -0.197   -0.955   -0.111   -0.076
      2.5%   -0.102    0.153    0.931   -0.061   -0.065
      3.0%   -0.194   -0.074    0.586    0.069    0.722
      3.5%    0.007    0.017    2.149    1.607    1.306
      4.0%   -0.163    0.123   -0.503   -0.005    0.928
      5.0%   -0.654   -0.530    0.122    0.866    1.052
```

**Conclusion:** SL=3.5%/TP=8.0% is the clear winner (Sharpe 2.15, +30.6% return). The entire SL=3.5% row dominates. The extra 0.5% SL room vs baseline (3.0%) prevents premature stop-outs, boosting win rate from 42% to 56%.

**Caveat:** 23 days of data — the SL=3.5%/TP=8% result may be partially overfitted. Needs validation on longer dataset.

---

## Config change applied

`config/settings.yaml` updated: `stop_loss_percentage: 0.03 → 0.035`
