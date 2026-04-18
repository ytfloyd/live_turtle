# Turtle Crypto Trading System — Backtest Specification

## Overview

Modified Turtle Trading system adapted for spot cryptocurrency markets.
Long-only, daily timeframe, trend-following breakout system with
volatility-based position sizing.

---

## 1. Universe

### Source
All USD-quoted spot pairs on Coinbase Advanced.

### Filters (applied daily)
1. Quote currency = USD
2. Status = online
3. Exclude stablecoins, wrapped tokens, and fiat-pegged assets:
   ```
   USDT, USDC, DAI, BUSD, TUSD, USDP, GUSD, FRAX, PYUSD, FDUSD,
   EURC, EURT, GBPT, GYEN, USDS, UST, MIM, LUSD, SUSD, CRVUSD,
   GHO, MKUSD, WBTC, CBBTC, CBETH, WETH, STETH, RETH, MSOL,
   PAX, HUSD, TRIBE, FEI, ALUSD, RAI
   ```
4. 24h USD volume >= $50,000 (scanner universe)
5. For execution: 24h USD volume >= $100,000
6. For execution: ATR(20) as % of close <= 12%
7. Minimum 56 daily bars of history required

### Notes for backtesting
- Universe is dynamic — assets enter/exit based on volume each day
- Use Coinbase listing dates as the earliest available data per asset
- Survivorship bias: include delisted assets if possible

---

## 2. Indicators (computed daily at the close)

### Donchian Channels
All channels **exclude the current bar** (no look-ahead).
Slicing: `df.iloc[-(N+1):-1]` for the prior N bars.

- **S1 Entry Channel**: 20-day high of highs
- **S1 Exit Channel**: 10-day low of lows
- **S2 Entry Channel**: 55-day high of highs
- **S2 Exit Channel**: 20-day low of lows

### ATR (Average True Range)
- True Range = max(H-L, |H-C_prev|, |L-C_prev|)
- ATR = simple moving average of TR over 20 bars
- ATR% = ATR / close × 100

### Channel Position %
- S1 channel % = (close - S1_exit_low) / (S1_entry_high - S1_exit_low) × 100
- S2 channel % = (close - S2_exit_low) / (S2_entry_high - S2_exit_low) × 100

### Signals
- **LONG**: close >= prior N-day high (breakout confirmed at daily close)
- **EXIT**: close <= prior N-day low
- **—**: neither

### 55-day Return
- return_55d = (close / close_55_bars_ago) - 1

---

## 3. Entry Rules

Entries are checked once daily at the close. Only **confirmed breakouts**
(close >= channel high) generate orders. No intraday stop-entry orders.

### Classification (priority order, first match wins)

| Priority | Label        | Condition                                    | Size       |
|----------|-------------|----------------------------------------------|------------|
| 1        | S2 LONG      | S2 signal = LONG, S2 channel % <= 140%      | Full unit  |
| 3        | S2 EXTENDED  | S2 signal = LONG, S2 channel % > 140%       | Half unit  |
| 2        | S1 LONG      | S1 signal = LONG, 55d return > 5%           | Full unit  |
| 4        | S1 WEAK      | S1 signal = LONG, 55d return <= 5%          | Half unit  |
| 5        | WATCH        | S1 channel % >= 75%, no breakout             | No order   |

- S2 breakouts take priority over S1 (a pair that is both S2 and S1 LONG
  enters as S2 LONG, not duplicated)
- WATCH is display-only — no order placed, check again next day

### Entry execution
- Market buy at the close (or next open in backtesting)
- No position if the asset is already held
- One unit per asset maximum (no pyramiding in this version)

---

## 4. Position Sizing

### Unit size formula
```
base_size = (account_equity × risk_per_unit) / (stop_loss_atr_multiple × ATR)

where:
  account_equity     = fixed at starting capital (not marked to market)
  risk_per_unit      = 0.005 (0.5% of account)
  stop_loss_atr_multiple = 2
  ATR                = 20-day ATR in dollar terms
```

### Example
```
Account = $20,000, BTC close = $75,000, ATR = $2,400

base_size = ($20,000 × 0.005) / (2 × $2,400)
          = $100 / $4,800
          = 0.02083 BTC
notional  = 0.02083 × $75,000 = $1,562
risk      = 0.02083 × 2 × $2,400 = $100 (0.5% of account)
```

### Half unit
For S2 EXTENDED and S1 WEAK classifications, multiply base_size by 0.5.
Risk per half unit = 0.25% of account.

### Rounding
- Round base_size DOWN to the asset's minimum trade increment
- Reject if rounded size < minimum trade size

---

## 5. Ranking (when more signals than capital allows)

When multiple breakouts fire on the same day, rank by composite score
and enter from highest to lowest until capital or heat cap is exhausted.

```
rank_score = breakout_strength × trend_confirmation × liquidity

breakout_strength:
  S2 LONG:   2.0 + (close - S2_entry_high) / ATR
  S1 LONG:   1.0 + (close - S1_entry_high) / ATR
  neither:   S1_channel_% / 100

trend_confirmation:
  1.0 + max(return_55d, 0)

liquidity:
  log10(volume_24h_usd)
```

Higher score = higher priority for capital allocation.

---

## 6. Exit Rules

Two independent exit mechanisms. Whichever triggers first closes the position.

### 6a. Hard Stop-Loss (2N stop)
- Stop price = entry_close - 2 × ATR (at time of entry)
- Placed as a resting stop-limit sell order immediately after entry fill
- Triggers intraday — does not wait for daily close
- Limit price = stop_price × 0.995 (0.5% slippage tolerance)
- In backtesting: assume fill at stop price (or use next bar open if
  stop is breached by gap)

### 6b. Donchian Low Exit (trend reversal)
- Checked once daily at the close
- Exit if close <= 10-day Donchian low (S1 exit channel)
- Sell entire position at market on the next bar's open
- Cancel any resting stop-loss order after exit fills

### Backtesting priority
On any given day, check if the intraday low breached the 2N stop BEFORE
checking the daily close against the Donchian low. If both trigger on
the same bar, assume the stop filled first (conservative).

---

## 7. Portfolio Constraints

| Constraint               | Value                    | Notes                                |
|--------------------------|--------------------------|--------------------------------------|
| Risk per full unit       | 0.5% of account          | $100 on $20k                        |
| Risk per half unit       | 0.25% of account         | $50 on $20k                         |
| Max portfolio heat       | 20% of account           | Sum of risk across all open positions|
| Max per-order notional   | 20% of account           | $4,000 on $20k                      |
| Max units per asset      | 1 (no pyramiding)        |                                      |
| Max daily orders         | 30                       |                                      |
| Deployment cap           | None (100% of capital)   | Heat cap is the binding constraint   |

### Heat cap enforcement
If placing a new order would push total portfolio risk above 20%, the
order is rejected. Lower-ranked signals are dropped first (the order
list is sorted by rank_score descending).

---

## 8. Execution Assumptions for Backtesting

| Parameter          | Value                                              |
|--------------------|----------------------------------------------------|
| Timeframe          | Daily bars (OHLCV)                                 |
| Entry execution    | Next bar open after signal (close >= channel high) |
| Exit execution     | Stop: at stop price intraday. Donchian: next open. |
| Slippage           | 0.5% on entries and exits (or use actual spreads)  |
| Commission         | Coinbase Advanced taker fee: 0.60% (or current)    |
| Starting capital   | $20,000                                            |
| Account sizing     | Fixed at starting capital (not compounded)          |
| Rebalance frequency| Daily, after the close                             |
| Long only          | Yes — no short selling                             |

---

## 9. What This System Does NOT Do

- No short selling
- No pyramiding (no adding to winners)
- No correlation-based position limits (single heat cap only)
- No trailing stops (stop is fixed at entry)
- No profit targets (exit only on Donchian low or hard stop)
- No rebalancing of existing positions (hold until exit signal)
- No compounding (account_equity is fixed, not marked to market)

---

## 10. Daily Workflow (for reference)

```
1. At the daily close:
   a. Check exits: any held position where close <= 10-day low → sell
   b. Check entries: any new breakout (close >= 20d or 55d high) → buy
   c. Place stops: 2N stop-limit sell for any new fill
   d. Record: log portfolio value, positions, heat to daily_log.csv
```

---

## 11. Key Differences from Original Turtle Rules

| Aspect              | Original Turtles            | This System                |
|---------------------|-----------------------------|----------------------------|
| Asset class         | Futures (leveraged)         | Spot crypto (no leverage)  |
| Risk per unit       | 1% (2% at 2N stop)         | 0.5% (0.5% at 2N stop)    |
| Direction           | Long and short              | Long only                  |
| Pyramiding          | Up to 4 units at ½N adds   | No pyramiding              |
| Correlation limits  | 6/10/12 unit caps by group  | Single 20% heat cap        |
| Entry mechanism     | Next-day market order       | Same — daily close signal  |
| Stop placement      | Resting order (intraday)    | Same                       |
| Exit mechanism      | 10d/20d Donchian low        | 10d Donchian low only      |
| Position sizing     | 1N = 1% of account          | 1N = 0.25% of account     |
| Account sizing      | Marked to market             | Fixed at starting capital  |
| Universe            | ~25 futures markets          | ~250 crypto pairs          |
| Ranking             | No formal ranking            | Composite score            |
