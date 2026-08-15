# Turtle Crypto — Research & Backtest Specification

**Version:** 1.0
**Status:** Live since 2026-04-14 on Coinbase Advanced, $20,000 walled-off portfolio
**Audience:** Research team
**Supersedes:** `docs/backtest_spec.md` (mechanical rules only; this document is self-contained)

---

## 1. Purpose

We are running a modified Turtle breakout system on spot crypto with real money.
The rules were specified a priori and have not been fitted to any backtest — they
are an adaptation of the published Turtle rules to a spot, long-only, no-leverage
context.

**We need to know whether the system has an edge, and which of its components
carry that edge.**

Two deliverables:

1. **Validation** — does the system as specified in §3 produce positive
   risk-adjusted returns net of realistic costs?
2. **Attribution** — which rules matter? Specifically: the ranking function,
   the S1/S2 split, the 55-day trend filter, the dual-exit structure, and the
   universe filters are all unvalidated design choices. Several were invented
   for this implementation and have no basis in the original Turtle rules.

Please treat §3 as the null hypothesis, not as a recommendation.

---

## 2. Critical Constraints (read before designing the backtest)

These three items will dominate the result. Get them right first.

### 2.1 Fee drag is likely the binding constraint

Coinbase Advanced taker fee is **0.60%** at our volume tier (verify current
schedule — it is volume-banded and may be lower).

Working the numbers on a live snapshot (2026-04-19, 18 proposed entries,
$13,728 total notional on a $20,000 account):

```
Average position notional     $762
Entry fee   @ 0.60%           $4.57
Exit fee    @ 0.60%           $4.57
Round-trip cost per position  $9.15

Risk budget per full unit     $100  (0.5% of $20,000)
Fee as % of risk budget       9.2%
```

**The system must generate more than 0.09R of expected edge per trade just to
break even on fees.** Portfolio-level: a full turnover of the book costs ~0.82%
of account equity. At a 30-day average holding period that is ~10%/yr; at 15
days it is ~20%/yr.

Average holding period is unknown and is itself a research output. Please
report it early — it determines whether this system is viable at all.

Model fees explicitly. Do not run a gross-return backtest and add a haircut
later.

### 2.2 Survivorship bias will flatter the results badly

The universe is ~250 Coinbase USD pairs, heavily weighted to small-cap
altcoins. Delistings and go-to-zero events are common. A backtest built only
from currently-listed assets will produce a materially wrong answer.

Include delisted pairs with their full history and delisting date. Model a
delisting as a forced exit at the last available price (or worse — assume a
haircut, and report sensitivity to it).

### 2.3 Token supply events break naive OHLCV

Real example observed live on 2026-04-19: RAVE-USD printed a close of $1.16
against a 20-day ATR of $4.04 — an **ATR of 347% of price** — after trading
near $14 the prior day. This is a redenomination, rebase, or similar supply
event, not a 92% single-day drawdown.

Our `MAX_ATR_PCT = 12%` filter happened to exclude it from entry, but a
backtest that takes raw OHLCV at face value will book fictional catastrophic
losses (or fictional gains) on every such event.

You need either split/rebase-adjusted series, or a detection heuristic
(e.g. flag and exclude any bar where |log return| exceeds some threshold and
volume does not corroborate). Please document whatever you choose.

---

## 3. System Definition

### 3.1 Universe

Source: all spot pairs on Coinbase Advanced.

Filters, applied daily:

| # | Filter | Value |
|---|--------|-------|
| 1 | Quote currency | USD |
| 2 | Status | `online` |
| 3 | Base currency not in exclusion set | see below |
| 4 | 24h USD volume (universe) | ≥ $50,000 |
| 5 | 24h USD volume (execution) | ≥ $100,000 |
| 6 | ATR(20) / close | ≤ 12% |
| 7 | Bars of history available | ≥ 56 |

Exclusion set (stablecoins, wrapped, pegged, liquid-staking derivatives):

```
USDT USDC DAI BUSD TUSD USDP GUSD FRAX PYUSD FDUSD
EURC EURT GBPT GYEN USDS UST MIM LUSD SUSD CRVUSD
GHO MKUSD WBTC CBBTC CBETH WETH STETH RETH MSOL
PAX HUSD TRIBE FEI ALUSD RAI
```

The universe is **dynamic** — membership is recomputed every day and assets
enter and leave as volume fluctuates. Note that filters 5 and 6 gate *entry
only*; a held position that later fails them is not force-exited.

Typical live universe size: 250–295 pairs after filtering, from ~900 raw
products.

### 3.2 Indicators

All computed on daily bars at the close. **Donchian channels exclude the
current bar** — the lookback is `bars[-(N+1):-1]`. This is deliberate and
must be preserved; including the current bar leaks the breakout into its own
signal.

| Indicator | Definition |
|-----------|------------|
| S1 entry channel | 20-bar high of highs |
| S1 exit channel | 10-bar low of lows |
| S2 entry channel | 55-bar high of highs |
| S2 exit channel | 20-bar low of lows |
| True Range | `max(H−L, abs(H−C_prev), abs(L−C_prev))` |
| ATR | **simple** 20-bar mean of TR (not Wilder smoothing) |
| ATR% | `ATR / close × 100` |
| S1 channel % | `(close − S1_exit_low) / (S1_entry_high − S1_exit_low) × 100` |
| S2 channel % | `(close − S2_exit_low) / (S2_entry_high − S2_exit_low) × 100` |
| 55-day return | `close / close[−56] − 1` |

Signals per system: `LONG` if `close ≥ prior N-bar high`; `EXIT` if
`close ≤ prior N-bar low`; otherwise none.

Note ATR uses a simple mean, not Wilder's smoothing. The original Turtles used
Wilder. This is a deviation — worth testing both.

### 3.3 Entry Classification

Evaluated once daily at the close. First match wins; a pair produces at most
one order.

| Order | Label | Condition | Size |
|-------|-------|-----------|------|
| 1 | S2 LONG | S2 signal = LONG **and** S2 channel % ≤ 140 | Full unit |
| 2 | S2 EXTENDED | S2 signal = LONG **and** S2 channel % > 140 | Half unit |
| 3 | S1 LONG | S1 signal = LONG **and** 55d return > 5% | Full unit |
| 4 | S1 WEAK | S1 signal = LONG **and** 55d return ≤ 5% | Half unit |
| 5 | WATCH | S1 channel % ≥ 75, no breakout | No order |

Because S2 is evaluated first, a pair that fires both S1 and S2 enters once, as
S2. Entry is a **market order at the close** (model as next-bar open).

Only confirmed breakouts trade. There are no resting stop-entry orders — an
earlier version had them and they were removed, since the Turtles checked at
the close and entered at market rather than resting buy-stops in the book.

### 3.4 Position Sizing

```
base_size = (ACCOUNT_SIZE × RISK_PER_UNIT) / (STOP_ATR_MULTIPLE × ATR)
          × size_multiplier
```

with `size_multiplier` = 1.0 (full unit) or 0.5 (half unit).

Worked example:

```
ACCOUNT_SIZE      $20,000
RISK_PER_UNIT     0.005
STOP_ATR_MULTIPLE 2
BTC close         $79,008
BTC ATR(20)       $2,487

base_size = ($20,000 × 0.005) / (2 × $2,487) = $100 / $4,974 = 0.0201 BTC
notional  = 0.0201 × $79,008 = $1,589   (7.9% of account)
risk      = 0.0201 × 2 × $2,487 = $100  (0.5% of account)
```

**`ACCOUNT_SIZE` is a fixed constant — it is not marked to market.** Position
sizes do not grow with profits or shrink with losses. This is a deliberate
choice for the live system but is an obvious research variable (§7.2).

Size is floored to the exchange's `base_increment`; orders below `base_min_size`
are rejected.

### 3.5 Ranking

When more signals fire than the heat cap permits, orders are sorted by a
composite score, descending, and filled until a cap binds.

```
rank_score = breakout_strength × trend_confirmation × liquidity

breakout_strength =
    S2 LONG : 2.0 + (close − S2_entry_high) / ATR
    S1 LONG : 1.0 + (close − S1_entry_high) / ATR
    neither : S1_channel_% / 100

trend_confirmation = 1 + max(55d_return, 0)

liquidity = log10(volume_24h_usd)
```

**This function was invented for this implementation.** It has no basis in the
Turtle rules and has never been validated. It is a priority research target
(§7.4) — the null hypothesis is that it adds nothing over random selection.

### 3.6 Exits

Two independent mechanisms; first to trigger wins.

**(a) Hard stop — 2N**
Stop price fixed at entry: `entry_close − 2 × ATR_at_entry`. Placed as a
resting stop-limit sell immediately on fill, limit = `stop × 0.995`. Triggers
**intraday**. The stop does not trail.

**(b) Donchian exit**
Checked daily at the close. Exit if `close ≤ 10-bar Donchian low`. Sells the
full position at market (model as next-bar open) and cancels the resting stop.

Backtest resolution when both could fire on the same bar: assume the intraday
stop filled first (conservative). Model gap-throughs at the open, not the stop
price.

The original Turtles used the S1 exit channel for S1 entries and the S2 exit
channel for S2 entries. **We use the 10-bar exit for everything**, including
S2 entries. That is a deviation — see §7.5.

### 3.7 Portfolio Constraints

| Constraint | Value | Enforced where |
|------------|-------|----------------|
| Risk per full unit | 0.5% of `ACCOUNT_SIZE` | sizing |
| Risk per half unit | 0.25% of `ACCOUNT_SIZE` | sizing |
| Max portfolio heat | 20% of `ACCOUNT_SIZE` ($4,000) | trade sheet + executor |
| Max notional per order | 20% of `ACCOUNT_SIZE` ($4,000) | trade sheet + executor |
| Max units per asset | 1 (no pyramiding) | skip-if-held |
| Max orders per day | 30 | executor |
| Total deployment cap | **none** | removed by design |

Heat is the sum of `risk_usd` across open positions. When it would be
exceeded, the lowest-ranked orders are dropped.

There is deliberately **no cap on total capital deployed**. Rationale: this is
unleveraged spot, so idle cash earns nothing and the loss ceiling is the stop,
not a margin call. In practice the book has run 88–98% deployed. Whether this
is correct is a research question (§7.3).

There are **no correlation or sector limits**. The Turtles capped units at
6 / 10 / 12 across closely-correlated, loosely-correlated, and single-direction
groups. We have one blunt heat cap instead. Given that crypto correlations
converge toward 1 in drawdowns, this is probably the single largest known
weakness in the design (§7.6).

### 3.8 Parameter Reference

| Parameter | Value |
|-----------|-------|
| `ACCOUNT_SIZE` | 20000 |
| `RISK_PER_UNIT` | 0.005 |
| `STOP_LOSS_ATR_MULTIPLE` | 2 |
| `MAX_PORTFOLIO_HEAT` | 0.20 |
| `MAX_ORDER_NOTIONAL_USD` | 4000 |
| `MAX_DAILY_ORDERS` | 30 |
| `SYSTEM1_ENTRY_PERIOD` | 20 |
| `SYSTEM1_EXIT_PERIOD` | 10 |
| `SYSTEM2_ENTRY_PERIOD` | 55 |
| `SYSTEM2_EXIT_PERIOD` | 20 (computed, unused for exits) |
| `ATR_PERIOD` | 20 |
| `MIN_24H_VOL_USD` (execution) | 100000 |
| `SCANNER_MIN_24H_VOL_USD` (universe) | 50000 |
| `MAX_ATR_PCT` | 12.0 |
| `STRONG_TREND_55D_RETURN` | 0.05 |
| `S2_EXTENDED_CHANNEL_PCT` | 140 |
| `WATCH_CHANNEL_PCT` | 75 |
| `HALF_UNIT_MULTIPLIER` | 0.5 |
| `STOP_LOSS_SLIPPAGE` | 0.005 |
| `MIN_CANDLES_REQUIRED` | 56 |

### 3.9 Spec-vs-Code Discrepancies

Please model §3.1–3.7, not the raw config file. Four constants in
`config.py` are dead and will mislead you:

- `MAX_DEPLOYMENT_PCT = 0.60` — **unused.** The deployment cap was removed;
  it is no longer imported by `trade_sheet.py`.
- `MAX_NOTIONAL_PER_ORDER_USD = 1500` — **unused.** The live per-order cap is a
  hardcoded `0.20 × account_size` literal in `trade_sheet.py:373`.
- `MAX_UNITS_PER_MARKET = 4` — **unused.** No pyramiding is implemented;
  effective value is 1.
- `SYSTEM2_EXIT_PERIOD = 20` — computed and reported, but exits use the
  10-bar channel for all positions.

---

## 4. Data Requirements

| Item | Notes |
|------|-------|
| Daily OHLCV, all Coinbase USD pairs | Including delisted; full history to listing date |
| Listing / delisting dates | Required for point-in-time universe reconstruction |
| Daily 24h quote volume | Drives filters 4–5; must be point-in-time, not current |
| `base_increment`, `base_min_size` per pair | For size rounding; current values acceptable |
| Fee schedule history | 0.60% taker assumed; verify and use actual tier |
| Supply-event log | Rebases, redenominations, forks — see §2.3 |

Coinbase's public candles endpoint is the live source. History depth is
limited; you may need a vendor for the full series.

---

## 5. Execution Model

| Parameter | Assumption |
|-----------|------------|
| Bar | Daily |
| Signal evaluation | At the close |
| Entry fill | Next bar open |
| Donchian exit fill | Next bar open |
| Stop fill | At stop price if `low ≤ stop`; at open if the bar gaps through |
| Slippage | 0.5% each way (sensitivity: 0.1% / 0.5% / 1.0%) |
| Commission | 0.60% taker each way |
| Starting capital | $20,000 |
| Rebalance | Daily, after 00:00 UTC |
| Direction | Long only |

Note that the live system executes against `XXX-USDC` pairs while computing
signals on `XXX-USD` (the portfolio holds USDC). USDC is 1:1 and the pairs
track closely; model on USD and ignore this.

---

## 6. Live Implementation Notes

Behaviours observed in production that a faithful backtest should reproduce —
or that you should tell us to fix.

1. **Fill quantity ≠ computed quantity.** Market buys are submitted with
   `quote_size` (a USD amount), so the exchange determines base quantity after
   fees. Actual fills run slightly under the sized quantity, which caused
   stop-placement failures until we re-read balances before placing stops.
   Minor for backtesting; relevant to sizing precision.

2. **Held positions exit the universe.** Assets whose volume drops below the
   filter disappear from the daily scan. We patched this (candles are now
   fetched directly for any held position), but the underlying dynamic is real:
   liquidity in these names is not stable. Model exit slippage as a function of
   position size vs. volume if you can.

3. **Classification mix skews to half-units.** In the 2026-04-19 sample, 10 of
   18 entries were half-unit (`S1 WEAK` / `S2 EXTENDED`). The effective average
   risk per position is therefore well below 0.5% — closer to 0.36%. Realised
   portfolio heat has run 4.5–12% against a 20% cap, i.e. **the heat cap has
   never bound.** Verify whether this holds over a full sample; if it never
   binds, the ranking function (§3.5) is also never exercised, and neither
   feature is doing any work.

4. **Two full sessions of live data exist** (2026-04-14 through 2026-04-19,
   27 positions, closed flat at roughly break-even, +2.7% peak). Far too short
   to infer anything. `data/daily_log.csv` and the SQLite audit DB
   (`data/turtle_audit.db`, every order intent and response) are available for
   reconciliation against your backtest engine.

---

## 7. Research Agenda

Ordered by expected information value.

### 7.1 Does the system work at all, net of costs?

Baseline run of §3 as specified. Report the metrics in §8. **Report average
holding period first** — per §2.1, if it is under ~20 days the fee drag likely
dominates any edge and the rest of the agenda is moot.

### 7.2 Fixed vs. compounded sizing

`ACCOUNT_SIZE` is currently a constant. Compare:
- Fixed at initial equity (as-implemented)
- Marked to market daily
- Marked to market with a high-water-mark floor

Hypothesis: compounding improves CAGR and worsens max drawdown. We want the
MAR / Calmar comparison, not just CAGR.

### 7.3 Risk per unit and the deployment question

Sweep `RISK_PER_UNIT` in {0.0025, 0.005, 0.01, 0.02} crossed with heat cap
in {10%, 20%, 40%, uncapped}.

The live system deploys 88–98% of capital because we removed the deployment
cap on the reasoning in §3.7. Test that reasoning: is there a return-to-cash
regime where holding dry powder beats full deployment? Report the deployment
distribution alongside returns.

Also: Kelly framing. For observed win rate `p` and payoff `b`, compute full
Kelly and report where the chosen sizing sits as a Kelly fraction. Note that
naive Kelly assumes independent bets — see 7.6.

### 7.4 Is the ranking function worth anything?

Compare four selection rules under a binding capital constraint:
- Composite `rank_score` (as-implemented)
- S1/S2 channel % only
- 55-day return only
- Random selection

If the composite does not beat random by a meaningful margin, delete it.
Note the constraint may rarely bind (§6.3) — if so, construct an artificially
tight cap so the comparison is actually informative.

### 7.5 Rule ablation

One at a time, hold everything else fixed:

| Ablation | Question |
|----------|----------|
| S1 only / S2 only / both | Does running both systems add anything? |
| Drop the 55d > 5% filter | Does the strong/weak split earn its complexity? |
| Drop the half-unit tier (all full) | Is size differentiation useful? |
| Drop the 140% S2-extended rule | Does chasing extended breakouts hurt? |
| Exit: 10-bar vs 20-bar vs system-matched | We use 10 for everything; Turtles matched exits to systems |
| Stop: 2N vs 3N vs Donchian-only (no hard stop) | Does the hard stop help or does it just harvest noise? |
| ATR: simple mean vs Wilder | We deviate from the original here |
| Universe: vary vol floor {0, 50k, 100k, 500k} and ATR ceiling {8%, 12%, 20%, none} | Are these filters earning their exclusions? |

### 7.6 Correlation — the known structural gap

The Turtles' correlation caps exist because unit-count limits are meaningless
if all your units are the same bet. We have no equivalent. In a crypto
drawdown, 25 alt positions are approximately one BTC-beta position.

Requested:
- Realised pairwise correlation of the held book over time
- Effective number of independent bets (e.g. `N_eff` from the eigenvalue
  spectrum of the correlation matrix, or `1/sum(w^2)` on PCA weights)
- Whether a correlation-adjusted heat cap (Transtrend-style risk budgeting —
  scale position risk by `sqrt(N_eff / N)`) improves risk-adjusted return
- Whether a simple BTC-beta cap captures most of that benefit at a fraction of
  the complexity

This is the change most likely to matter. Please prioritise it above the
cosmetic ablations in 7.5 if time is limited.

### 7.7 Regime dependence

Segment results by BTC trend regime (e.g. above/below its own 200-day MA) and
by realised cross-sectional dispersion. Trend systems are regime-dependent by
construction; we want to know the shape of the bad regime, not just its
existence. Report worst-case drawdown duration in the unfavourable regime.

### 7.8 Rebalance timing sensitivity

We run daily after 00:00 UTC. Test sensitivity to evaluation time — if results
swing materially on the hour chosen, that is evidence of fragility rather than
edge.

---

## 8. Metrics & Benchmarks

Report for every configuration:

**Return** — CAGR, total return, monthly return series
**Risk** — annualised vol, max drawdown, drawdown duration (max and median),
downside deviation, worst month
**Risk-adjusted** — Sharpe, Sortino, MAR / Calmar
**Trade-level** — win rate, average win/loss in R, payoff ratio, expectancy in
R, **average holding period**, trade count, turnover
**Cost** — total fees paid, fees as % of gross P&L, slippage as % of gross P&L
**Exposure** — mean / median / max deployment %, mean position count, realised
heat distribution
**Attribution** — P&L by classification (S2 LONG / S2 EXTENDED / S1 LONG /
S1 WEAK) and by exit reason (2N stop / Donchian / delisting)

Benchmarks: BTC buy-and-hold; equal-weight top-10 by market cap, monthly
rebalanced; 100% cash. All net of the same fee assumptions.

Please also report the **1st-percentile and 5th-percentile equity paths** from
a block-bootstrap or trade-order-shuffle resample. Point estimates on a single
historical path are not decision-grade.

---

## 9. Deliverables

1. Baseline result for §3 as specified, net of costs, with §8 metrics.
2. Average holding period and total fee drag — deliver these first,
   independently of everything else.
3. Ablation table for §7.5.
4. Correlation analysis for §7.6 with a concrete recommendation.
5. Parameter sensitivity surfaces for §7.3.
6. A written view on whether to keep running this, and if so what to change.

Assume nothing in §3 is sacred except the no-look-ahead channel construction.
If the honest answer is that the edge does not survive fees, say so plainly —
that is a useful result and we would rather find it in research than in the
account.

---

## 10. Reference

- Code: `github.com/ytfloyd/live_turtle`, branch `claude/turtle-trading-coinbase-jGp0P`
- Rules implementation: `src/turtle_crypto/scanner.py` (indicators),
  `src/turtle_crypto/trade_sheet.py` (classification, sizing, caps)
- Live audit trail: `data/turtle_audit.db` (SQLite — every order intent,
  response, and rejection since inception)
- Daily equity log: `data/daily_log.csv`
