# Binance Spot Testnet Trading Agent (V0.1)

> **Live trading is not available in this version.** V0.1 supports exactly
> three execution modes: `backtest` (simulated fills on historical data),
> `testnet` (real orders against the Binance **Spot Testnet** only, using
> play money), and `shadow` (forward-only, real-market simulation of one
> frozen research candidate - see [Shadow mode](#shadow-mode-forward-only-observation-of-multitimeframe_breakout_e1_round3)
> below). There is no `live` mode, no production Binance endpoint anywhere
> in the order-submission code path, and no way to configure one - see
> [SECURITY.md](SECURITY.md) for how that is enforced and tested.

A modular, safety-first prototype for a multi-market trading agent. This
version trades BTC/USDT spot only, on 4-hour candles, long-or-cash only -
no margin, no leverage, no shorting, no futures, no withdrawals.

## Project status

This is a research prototype (V0.1). It has not been tested with a
meaningful amount of live capital because it cannot trade with real money
at all in this version. Backtest results in this repository are not a
claim of profitability - see [STRATEGY.md](STRATEGY.md).

## Architecture at a glance

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full picture. In short: the
strategy never talks to a broker. Every proposed trade flows
`strategy -> position sizer -> risk engine -> order validator -> broker
adapter`, and the broker adapter for real orders is hard-coded to the
Binance Spot Testnet host.

## Installation

Requires Python 3.11+.

```bash
git clone <this-repo-url>
cd binance-testnet-trading-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

Two separate things are configured differently, on purpose:

- **Non-secret settings** (mode, symbol, interval, strategy parameters,
  risk limits, fees/slippage assumptions, file paths) live in
  `config/default.yaml`. Copy it and edit a copy if you want different
  values; pass it with `--config path/to/your.yaml`.
- **Secrets** (Testnet API key/secret) live only in environment variables,
  loaded from a local `.env` file that is never committed. Copy
  `.env.example` to `.env` and fill in your **Spot Testnet** credentials
  (get them by logging in with GitHub at
  https://testnet.binance.vision/ - do **not** use production API keys,
  and never grant withdrawal permission on any key used here).

```bash
cp .env.example .env
# edit .env with your testnet key/secret
```

Validate your configuration at any time:

```bash
trading-agent config-check
trading-agent --mode testnet config-check   # override the mode for one command
```

## Running the tests

```bash
pytest
ruff check src tests
```

No test requires real credentials or network access - everything talks to
mocked HTTP responses. See [TESTING.md](TESTING.md) for what is covered.

## Historical data acquisition

Historical candles for backtesting come from Binance's public,
**unauthenticated, read-only** market-data endpoint
(`api.binance.com/api/v3/klines`) - not the Testnet, whose own history is
unreliable because it resets periodically and is a separate matching
engine from real markets. No API key is used or needed for this, and the
client used for it has no order-placing capability at all (see
`src/trading_agent/data/market_data_public.py`).

```bash
trading-agent fetch-data --limit 1000
```

This fetches the most recent completed candles for the configured symbol
and interval and stores them in the local SQLite database
(`config.paths.db_path`).

For a specific date range - including multiple years of history, which
needs more than one 1000-candle request - use `--start`/`--end`:

```bash
trading-agent fetch-data --start 2020-01-01 --end 2024-01-01
```

`--start`/`--end` define a genuine half-open `[--start, --end)` range:
`--end` is EXCLUSIVE, so a candle opening exactly at `2024-01-01T00:00:00Z`
above is never fetched or stored - this holds even though Binance's own
API treats its `endTime` kline parameter as inclusive (see
`src/trading_agent/data/historical_fetch.py`'s module docstring for a real
incident this is enforced against: an earlier version of this command
stored one candle exactly at a requested `--end` date, one instant inside
the immutable research cutoff).

This pages through the full range automatically, with bounded retries and
backoff on rate limits, and de-duplicates overlapping candles. Real
multi-year history occasionally has a genuine gap - a candle the exchange
itself never recorded. This is never fabricated or interpolated over:
every gap is detected, given one focused narrow-range retry to rule out a
pagination artifact or a transient API response, and - if still missing -
CONFIRMED and recorded in a durable gap manifest alongside every valid
candle around it (see `src/trading_agent/data/historical_fetch.py` and
`data/gap_detection.py`). The command reports what it found:

```
Stored 43811 completed candles with 1 confirmed historical gap. No candles were fabricated.
  confirmed gap: expected_open_time_ms=1582113600000 previous_open_time_ms=1582099200000 next_open_time_ms=1582128000000 missing_intervals=1
```

Re-running the same download is idempotent - candles and gap records are
both keyed for upsert, so nothing is duplicated. This gap tolerance is
**historical-research-only**: live/Testnet signal generation
(`execution/live_runner.py`) always rejects any gap outright via a
completely separate, unmodified validation path - see ARCHITECTURE.md.

### Optional gap forensics and recovery (read-only, then explicitly confirmed)

A confirmed 1h gap above is preserved, never filled - but it can
sometimes still be reconstructed from Binance's own official 1-minute
klines, if all 60 expected 1-minute candles for that hour actually exist.
This is a separate, OPTIONAL, two-step tool (`src/trading_agent/data/
gap_recovery.py`) - never automatic, and never touching any
strategy/candidate/parameter/scorecard/risk/fee/slippage/sizing/execution
logic or running a candidate evaluation:

```bash
trading-agent research-gap-audit
```

READ-ONLY. For every confirmed 1h gap already in your database, queries
Binance's 1-minute klines for each missing hour and classifies it
`FULLY_RECOVERABLE` (all 60 minutes present, continuous, and validated),
`PARTIALLY_RECOVERABLE` (some but not all 60), `GENUINE_NO_DATA` (the
exchange has nothing at all - a real outage), or `UNRESOLVED` (a
validation failure, or a candle at/after the immutable research cutoff,
which is never even fetched). Reports the resulting gap-free segment
lengths and the Round-3 complete-block count if every fully-recoverable
hour were stored - nothing is written to any database by this command.

```bash
trading-agent research-gap-recover --confirm
```

Runs the identical analysis and prints the identical report, then - ONLY
because `--confirm` was passed - stores every `FULLY_RECOVERABLE`
candle (aggregated exactly as Binance's own 1h candles are built:
`open`=first open, `high`=max high, `low`=min low, `close`=last close,
`volume`=sum of volumes; never interpolated, never fabricated) atomically
alongside a freshly recomputed gap manifest. Without `--confirm`, this
command performs the exact same analysis and stores nothing at all -
`--confirm` is the only thing that makes it write. Both commands require
`market.interval: "1h"` (see `config/round3_1h.yaml`) and print full
provenance (source, retrieval time, component count, first/last
timestamp, validation result, deterministic SHA-256 content hash) for
every candle they classify as recoverable.

**Bounded, observable, and resumable** (post-incident fix - a real run
once appeared to hang for 90 minutes with zero output; see CHANGELOG.md's
`[0.6.1]` entry for the full root cause): every HTTP request has an
explicit, bounded connect/read timeout and at most 3 attempts with capped
backoff; each gap's entire missing range is fetched in as few batched
requests as possible (never one request per hour or minute); each gap
also has its own wall-clock budget (`--max-seconds-per-gap`, default 60s)
- exceeding it marks that gap's remaining hours `UNRESOLVED` rather than
waiting further. Both commands print live, immediately-flushed progress
(`gap 3/28: ...`, each fetch attempt with its outcome) and checkpoint
every completed gap to `<data_dir>/gap_audit_checkpoint.json`: Ctrl+C
exits cleanly with a partial summary, and re-running later resumes
without re-downloading anything already audited (pass `--reset-checkpoint`
to force a full re-audit instead). `research-gap-recover --confirm`
refuses to store anything at all if the audit was interrupted before
finishing every confirmed gap - recovery is impossible without a
completed audit.

## Backtesting

```bash
trading-agent backtest
```

Runs the baseline strategy candle-by-candle over the stored history with no
look-ahead (see [STRATEGY.md](STRATEGY.md)), routes every proposed trade
through the same risk engine and order validator a live run would use, and
prints **two independent evaluations** every time (see
`backtest/engine.py`'s module docstring for the full design):

1. **The continuous operational simulation** (`run_backtest`) - what would
   actually have happened if the system started trading at the first
   candle and kept its risk state (peak equity, drawdown, cooldowns, daily
   counters) running continuously. When the stored history has no
   confirmed gap, this still prints the familiar chronological
   `train`/`validation`/`test`/`overall` labels - fixed strategy
   parameters throughout (see config/default.yaml), never refit per
   window - but these are timeline slices of **one uninterrupted run**,
   not independent evaluations. A risk shutdown latched during the
   "train"-labeled portion mechanically carries into everything labeled
   "validation"/"test" after it, because it is the same simulation. A
   `diagnostics` block is always printed alongside so this is never left
   to be inferred: exact BUY/EXIT signal counts, executed entries vs.
   strategy exits vs. stop-loss exits (counted separately), every rejected
   entry grouped by its exact reason code, the first and last executed
   trade timestamps, the maximum-drawdown value AND timestamp, and for
   every risk-gate shutdown that ever activated: when it first triggered,
   the equity/drawdown at that moment, how many otherwise-valid BUY
   signals it blocked, whether it stayed latched for the rest of the run,
   and the ending cash/asset quantity/marked-to-market equity.
2. **The independent fixed-parameter holdout evaluation**
   (`run_independent_holdout_evaluation`) - printed under a banner that
   says exactly that, **not walk-forward optimization**. Train,
   validation, and test each run with the *same* fixed strategy
   parameters but start from a completely fresh configured starting
   balance and fresh risk state (peak equity, drawdown, cooldowns, and day
   counters all reset). A window may look back at preceding candles from
   its own gap-free segment for indicator warm-up only - those warm-up
   candles never generate a trade, never contribute to the reported
   performance, never reach across a confirmed gap, and no candle beyond
   the window's own end is ever visible to it. A position or pending
   signal open at a window's end is reported, never carried into the next
   window. This directly answers what validation/test look like on their
   own merits, without inheriting whatever risk state train's own run
   happened to end in.

Both reports show **starting AND ending equity** for every window/segment
(`config.backtest.starting_equity`, default `50.0` - previously a hardcoded
constant, now validated configuration) and a buy-and-hold comparison
computed over the *exact* same candle range as the report it sits next to:
same start/end timestamps, one documented buy-side transaction cost, marked
to market at the final available candle's close, its own maximum drawdown,
and never bridged across a confirmed gap. Each report covers total and
annualized return, max drawdown, volatility, Sharpe/Sortino (assumptions
documented), win rate, profit factor, exposure, turnover, and trade count.
A warning is printed whenever a window/segment has too few trades to be
statistically meaningful. **This is a research report, not investment
advice, and past simulated performance does not indicate future results.**

If the stored history contains a confirmed gap, `config.backtest.gap_policy`
(default `"segment"`) splits the backtest into independent contiguous
segments at each gap rather than discarding the whole series - each
segment gets its own fresh indicator warm-up, portfolio, and day/cooldown
state, so nothing carries across the gap. A signal still queued at a
segment's end is cancelled, never carried into the next segment; a
position still open at a segment boundary caused by a gap is marked an
unresolved research condition (no exit price is ever invented for it) and
is, by default, excluded from the aggregate trade statistics
(`exclude_open_position_segments`). Because each segment restarts from the
same baseline `starting_equity` rather than continuing the previous
segment's ending balance, **their equity curves are never naively
concatenated into one "overall" return/drawdown/Sharpe/Sortino** - when
more than one segment actually ran, `result.reports` is empty and each
segment gets its own complete, independent `PerformanceReport`
(`segments[i].performance`), alongside an explicitly-labeled
`aggregate_trade_stats` containing only the trade-level figures that
remain mathematically valid to sum across independent segments (total
trades, total realized PnL in quote currency, overall win rate - never a
percentage return or drawdown). The command reports the breakdown:

```
gap_policy=segment  segments=2  confirmed_gaps=1
  segment 0: 2020-01-01 to 2020-02-19 (312 candles, 4 trade(s))
    starting_equity=50.0 ending_equity=54.12 total_return_pct=8.24 max_drawdown_pct=3.10 ...
  segment 1: 2020-02-19 to 2024-01-01 (43495 candles, 61 trade(s))
    starting_equity=50.0 ending_equity=71.30 total_return_pct=42.60 max_drawdown_pct=11.02 ...
WARNING: results across gaps are NOT one continuous tradable equity history - ...
--- aggregate_trade_stats (trade-level ONLY, see note) ---
segments_included=2 total_trades=65 total_realized_pnl_quote=25.42 win_rate=44.6 ...
```

Set `gap_policy: reject` to restore the original strict behavior (any gap
raises and aborts the backtest) if you would rather investigate a gap
manually before trusting a segmented result.

### Extended diagnostics on every window/segment

Every window (a holdout window, a continuous-mode split, or a gap segment)
also prints an extended diagnostics block - `metrics/extended_report.py` -
built purely from that window's own already-computed trades and equity
curve, never re-simulating or tuning anything:

- **Accounting identity**: `ending_equity = ending_cash + ending_base_quantity
  * final_mark_price` is computed and compared explicitly, not assumed.
- **PnL breakdown**: realized closed-trade PnL, unrealized PnL on an ending
  open position (marked at the final available price - no exit is ever
  invented for it), total mark-to-market PnL, entry/exit fees split apart,
  a note on why backtest fees are always simulated estimates (never
  exchange-derived - this code path never talks to a real exchange), and
  total slippage cost.
- **Plain-language explanations**, backed by the same evidence as the
  diagnostics above: why a window ends with an open position, why executed
  entries can exceed closed-trade counts (an entry with no exit yet isn't a
  closed trade), and why trading stopped - naming the exact latched
  risk-gate shutdown when there is one, or noting that the strategy itself
  simply produced no further signal when there isn't.
- **Time-based performance**: CAGR, a monthly return series, % of positive
  months, the longest underwater period, an exposure-adjusted return, and a
  Calmar ratio - each with a note on when it is mathematically undefined.
- **Trade-distribution diagnostics**: median trade return, average/largest
  winner and loser, the best trade's contribution to total PnL and the
  result excluding it, consecutive win/loss streaks, and the holding-period
  distribution.
- **A deterministic (fixed-seed) bootstrap confidence interval** over the
  window's own closed trades - always printed with a prominent caveat that
  it does not preserve chronological/market-regime ordering and is not
  evidence of future profitability.
- **Chronological rolling-window diagnostics** (fixed trade-count groups,
  using the same already-configured strategy parameters throughout) -
  purely for inspection; nothing here ranks, optimizes, or selects a
  configuration.
- A **test window is explicitly marked already-consumed** once its results
  have been reported, with a warning against reusing it as an untouched
  final holdout for any future strategy selection.

## Research phase: leakage-resistant strategy development

The v0.1 `ema_crossover_v0_1_rejected` baseline (`strategy/trend_baseline.py`,
frozen at ema_fast=20/ema_slow=50) was formally REJECTED against its
observed test window (headline return -1.22%, closed-trade bootstrap mean
-9.67%, max drawdown 14.07%, max consecutive losses 7 - see
`research/frozen_baseline.py::FROZEN_BASELINE_VERDICT`). It is preserved
exactly, unmodified, as a frozen regression point - never repaired, tuned,
or reconsidered.

```bash
trading-agent research-backtest
```

This runs two things, in order, over the stored history:

1. **Reproduces the frozen baseline's report** (`run_backtest` +
   `run_independent_holdout_evaluation`, unchanged) over the FULL stored
   history, including the already-observed 2025-05-16..2026-09-04 period -
   the only thing in this codebase permitted to touch that period, since
   it takes no candidate parameter at all.
2. **Walk-forward develops nine declared candidates** - three simple,
   economically-defensible families (trend-following with a volatility/
   regime filter, breakout with volatility-normalized entry, conservative
   mean reversion restricted to non-trending regimes -
   `research/candidates/`) across a small, FIXED set of configurations
   (`research/candidate_registry.py`) declared in code before any of them
   ever sees data - never a grid search, optimizer, or ML selection - using
   ONLY data strictly before the **immutable research cutoff**
   (`research/cutoff.py::RESEARCH_CUTOFF_MS`, 2025-05-16T00:00:00Z). Any
   attempt to develop or score a candidate on data at or after that
   timestamp raises `ResearchCutoffViolation` outright.

Each candidate is evaluated with a **BLOCKED CHRONOLOGICAL EVALUATION**
(`research/blocked_chronological_evaluation.py`) - renamed from
"walk-forward" as a pre-real-evaluation code review correction, because
that is NOT what this does: walk-forward optimization re-fits or
re-selects a model on each successive expanding window, and nothing here
fits, trains, or selects anything per block. Every candidate's parameters
are fixed in `candidate_registry.py` before this module ever runs and stay
identical across every block; the same one fixed candidate is simply
independently re-run, unchanged, over successive non-overlapping
chronological blocks of the same pre-cutoff data, purely as a robustness
check. A confirmed historical gap always starts a new segment, blocks
never cross it; each block's own indicator warm-up precedes it and never
trades; no position or risk state of any kind carries from one block to
the next (every block is its own independent call into the SAME
`run_segment` primitive the frozen baseline uses, so a candidate has no
way to reach or influence the broker, fill assumptions, the risk engine,
or accounting - it only ever returns a `Signal`). Every block is reported,
including zero-trade and skipped blocks - never only the best one.

Every candidate is also gated by a **fixed minimum 1:2 planned reward/risk
policy** (`backtest/risk_reward.py`), a user-mandated pre-real-evaluation
risk policy applied identically to all nine without changing any declared
signal parameter: each planned entry is sized so its planned NET loss
(after estimated entry fee, stop-exit fee, and adverse slippage) is at
most 1% of current equity, and the take-profit target is SOLVED
ALGEBRAICALLY - not fixed at a flat multiple of the stop distance - so the
planned NET reward (after entry fee, target-exit fee, and adverse
slippage) comes out to exactly the fixed 2.0 minimum before tick-size
rounding. This means the GROSS (pre-cost) reward/risk ratio is cost-
adjusted upward to compensate for round-trip costs - exactly 2.0 only when
fees and slippage are both zero, and slightly ABOVE 2.0 whenever either is
positive - while the NET ratio stays pinned at the fixed 2.0 floor. Both
ratios are computed with exact Decimal arithmetic (no floating-point
tolerance) and reported per entry. The target is then rounded to the
exchange's price tick in the direction that can only increase the reward,
and the net ratio is re-checked against the rounded target before
approval - an entry is REJECTED outright only if a valid tick-aligned
target still can't clear 2.0, or if a risk-safe size cannot satisfy the
exchange's minimum notional/lot-size (never satisfied by increasing risk).
After the simulated fill, the whole plan is rebuilt and RE-VALIDATED from
the real fill price; if either the 1%-of-equity risk cap or the 2.0 net
floor no longer holds, the position is never created (fail closed) rather
than left open unprotected. No leverage, one open position maximum, and
fees are reserved so a fill can never overspend available balance. A
normal trade with realistic nonzero fees and slippage is approvable under
this policy - see that module's docstring for the full algebra.

Each candidate then gets a **scorecard** (`research/scorecard.py`) - a
rule-based, pre-declared pass/fail test, scored on REALIZED closed-trade
PnL only (normalized by each block's own starting equity) and never on
marked-to-market total return, so an unfinished open position can never by
itself make a block - or a candidate - pass: at least 30 total closed
trades across at least 4 evaluated blocks, a positive median block
realized return, a positive aggregate realized PnL, no materially negative
block (worst block realized return >= -10%) even if the aggregate is
positive, a max drawdown across blocks no worse than 15%, limited
dependence on its single best trade (<= 50% of a positive block's PnL,
and an undefined ratio never auto-passes), and at least 60% of blocks with
positive realized PnL. Marked-to-market total return and its excess over
buy-and-hold are still reported for every block for visibility, but are
never a pass/fail input - beating buy-and-hold in every bullish block is
never required; absolute profitability and drawdown control remain the
primary bar. None of this is a "pick the best-performing candidate"
ranking. The final status is always exactly one of:

- **`REJECTED`** - failed at least one criterion (see the printed reasons).
- **`RESEARCH_SURVIVOR`** - passed every criterion. **This is never a claim
  of profitability or approval for live/Testnet trading.** It must be
  frozen (`research/freeze.py`) before any further test - and it always is,
  automatically, by this command - and its only valid next test is
  genuinely new candles that arrive after this evaluation; previously
  observed data (development or already-consumed) can never become a new
  "untouched" holdout for it.
- **`INSUFFICIENT_EVIDENCE`** - too few trades occurred to judge either way.

A **multiple-testing warning** is always printed alongside the scorecard:
evaluating nine candidates and reporting whichever looks best after the
fact is a classic selection-bias trap - the scorecard's pass/fail
thresholds are fixed and declared before scoring specifically to guard
against this, and more than one (or none) of the nine may become a
`RESEARCH_SURVIVOR`.

This phase does not implement Testnet BUY, production execution,
scheduling, leverage, futures, short selling, forex, machine learning,
news trading, or copy trading - it is a backtest-only research tool.

### Candidate post-mortem report (read-only)

```bash
trading-agent research-postmortem
```

After `research-backtest` has run and reported REJECTED/INSUFFICIENT_EVIDENCE
(or RESEARCH_SURVIVOR) for the declared candidates, this command builds a
detailed, READ-ONLY post-mortem for every one of them (`research/
post_mortem.py`) - pure aggregation math over the SAME deterministic
pre-cutoff evaluation, with no new simulation, no database write, and no
candidate search. It changes nothing about any strategy, parameter,
threshold, risk/reward rule, fee, slippage, sizing, or execution behavior.

For each candidate it reports: trade counts and win rate; average/median
net PnL, average winner/loser, realized payoff ratio; expected value per
trade (in quote currency, as a % of starting equity, and in R-multiples);
profit factor; an exit-reason breakdown (take-profit, stop-loss, strategy
exit, and a gap-through-stop subset of stop-loss) with win rate/expectancy
per reason; the realized R-multiple distribution (min/median/mean/max, %
achieving at least +2R, % losing more than -1R due to gaps/costs); planned
vs. realized R/R; total fees and total modeled slippage; results excluding
the best trade / best 3 trades / best 5% of trades (reported specifically
for breakout-family candidates, as required); PnL concentration (top 1/3/5
trade share of gross winning PnL, and how many trades are needed to reach
50%/100% of net profit); chronological stability (per-block, per-calendar-
year, longest losing streak, longest underwater period on a clearly-
labeled cumulative-PnL curve - never an equity curve, first-half vs.
second-half); and the fixed risk/reward policy's own rejection and 1%-risk-
compliance diagnostics.

Every aggregate PnL figure is explicitly labeled as the **sum of PnL across
independently restarted $50 blocks** - never presented as, or confused
with, a continuous compounding equity curve (no such continuous account
ever existed in this evaluation). This report generates **no rankings and
selects no candidate** - each candidate ends with exactly one evidence-only
diagnosis, from a fixed, disclosed rule applied identically to all of them:
`broad positive expectancy`, `concentrated/fragile positive expectancy`,
`negative expectancy`, or `insufficient evidence`. These are never
"profitable", "approved", or "rejected" - that vocabulary remains
`research-backtest`'s own, separate scorecard.

### Duration-normalized sensitivity report (read-only)

```bash
trading-agent research-sensitivity
```

Round 1's real evaluation surfaced a methodology finding: the original
blocked chronological evaluation splits every gap-free segment into a
FIXED NUMBER of blocks (`block_count=5`) by candle count, so a tiny
fragment segment gets the same five voting blocks as a multi-year dominant
segment. This command re-scores all nine ORIGINAL, UNMODIFIED candidates
using fixed **365-day-duration** blocks instead (`research/
fixed_duration_evaluation.py`) - a segment too short for even one complete
year gets **zero** voting blocks (reported as an insufficient-duration
fragment), never five negative zero-trade votes; any leftover sub-year
tail is reported separately and excluded from every pass/fail calculation.
Both the original and duration-normalized scorecards are printed side by
side, using the exact same, unmodified `research/scorecard.py` thresholds
for both. **This never changes any original result, scorecard, diagnosis,
or frozen artifact** - `round_1_original_evaluation` is a byte-for-byte
reproduction, and `duration_normalized_sensitivity` is explicitly
non-binding: it never overrides an original verdict and never
retroactively creates a `RESEARCH_SURVIVOR`.

### Round 2: one result-informed hypothesis (read-only, pre-cutoff only)

```bash
trading-agent research-round2
```

Round 1 showed `breakout_B1` had broad trade-level profitability but
sustained losses during an unfavorable 2021-2022 regime. This command
evaluates exactly **one** new, explicitly RESULT-INFORMED candidate -
`breakout_regime_D1_round2` (`research/candidates/breakout_regime_gate.py`)
- never presented as an untouched, pre-registered test. D1 preserves
`breakout_B1`'s own breakout/channel-breakdown signal and parameters
exactly (channel_period=20, atr_period=14, breakout_atr_multiple=0.25) and
adds exactly one causal gate in front of a would-be BUY: the signal
candle's close must be above a 200-period EMA, and that EMA must be
strictly above its own value 20 completed candles earlier (a rising
long-term average) - both conditions on the same completed candle, no
look-ahead. Nothing about B1's exit, stop-loss, take-profit, sizing, fees,
or slippage changes.

D1 is evaluated **only** on pre-cutoff data using the same
duration-normalized blocks above - never the consumed post-cutoff period -
and scored against the exact same conservative scorecard thresholds as
round 1 (nothing loosened or tightened). The report discloses the round
number, the cumulative candidate configurations examined across both
rounds (**10**), and a permanent multiple-testing warning. It reports
every full-duration block (including any that failed), the percentage of
breakout signals the EMA200 gate blocked, and a side-by-side comparison
against the original `breakout_B1` re-run on **identical** block dates for
a fair comparison - without altering B1's own round-1 status. **Even a
`RESEARCH_SURVIVOR` verdict for D1 is not a claim of profitability and not
approval for live or Testnet trading.**

### Round 3: a multi-timeframe hypothesis (read-only, pre-cutoff only, 1h data)

```bash
trading-agent --mode backtest research-round3
```

D1's OFFICIAL round-2 verdict was REJECTED - already observed, and
preserved unchanged (this command never touches `research/
candidate_registry_round2.py`, `research/candidates/breakout_regime_gate.py`,
or `research/round2_report.py`). Round 3 evaluates exactly **one** new
candidate - `multitimeframe_breakout_E1_round3` (`research/candidates/
multitimeframe_breakout.py`) - built on three timeframes: a **weekly**
regime gate (last completed weekly close above a rising 40-period weekly
EMA - BUY is categorically prohibited otherwise), D1/B1's own **4h**
breakout+EMA200-regime setup (identical parameters, unchanged), which arms
a 4-completed-1h-candle entry window and then expires unrenewed, and a
**1h** confirmation layer - the first completed 1h candle within that
window closing above both the triggering breakout level and its own open
produces the one entry that setup will ever produce. Weekly and 4h
candles are never fetched separately; they are derived by aggregating
whatever 1h candles the strategy is given, and a real gap or misalignment
simply excludes that bucket rather than fabricating one.

This command requires **1h candles specifically** - it overrides the
market interval to `1h` regardless of your config file's own default, and
reads only `interval="1h"` rows from your candle database (multiple
intervals of the same symbol can coexist there; fetch them first with a
config whose `market.interval` is `1h`). E1 is evaluated ONLY on
pre-cutoff data via the same duration-normalized blocks and scorecard
thresholds rounds 1-2 use - nothing loosened or tightened. The report
discloses the round number, the cumulative candidate configurations
examined across all three rounds (**11**), and E1's own funnel
diagnostics (weekly-filter rejections, 4h setups detected/armed/expired,
1h confirmations, entries) alongside the same detailed post-mortem every
other candidate gets. A full multi-year 1h evaluation can take a
considerable amount of time (E1's weekly-EMA-40 warm-up alone needs
roughly 45 weeks of 1h candles, re-derived on every single decision) -
an accepted cost of the required multi-timeframe design. **Even a
`RESEARCH_SURVIVOR` verdict for E1 is not a claim of profitability and
not approval for live or Testnet trading.**

**Fetching the 1h data this command needs.** A dedicated pre-evaluation
data-integrity audit (see the `[0.5.1]` entry in
[CHANGELOG.md](CHANGELOG.md) and
`tests/unit/test_data_integrity_round3_audit.py`) proved that
`data/storage.py`'s candle schema is already fully interval-aware - its
`PRIMARY KEY (symbol, interval, open_time_ms)` and every read (`get_candles`,
`latest_close_time_ms`) filtering on `interval` explicitly means 1h and 4h
candles for the same symbol coexist safely in **the same** database file;
switching `market.interval` can never overwrite, mix with, or leak into
the other interval's rows or queries. No schema migration and no separate
database file is structurally required. Use the provided
`config/round3_1h.yaml` (identical to `config/default.yaml` except
`market.interval: 1h`, and deliberately pointed at the SAME `db_path`) to
fetch exactly the pre-cutoff 1h history E1 needs:

```bash
trading-agent --config config/round3_1h.yaml fetch-data --start 2017-08-17 --end 2025-05-16
```

`--start 2017-08-17` is BTCUSDT's own Binance listing date (harmless to
start earlier than the exchange's actual history - the fetch simply
returns nothing before it). `--end 2025-05-16` is exact, not approximate:
`fetch-data` fetches candles opening in `[start, end)` (a half-open
interval - see `data/historical_fetch.py::fetch_historical_range`), so
this end date excludes every candle opening at or after
`2025-05-16T00:00:00Z` - precisely the immutable research cutoff
(`research/cutoff.py::RESEARCH_CUTOFF_MS`) - without fetching a single
candle you are not allowed to develop or score against. Then run:

```bash
trading-agent --config config/round3_1h.yaml research-round3
```

## Running on the Spot Testnet

```bash
trading-agent --mode testnet run
```

**Testnet operation is OBSERVATIONAL, not a general trading path.** Every
cycle: reconciles any unresolved order from a previous run and checks that
local and exchange balances still agree (either check failing blocks ALL
new order submission this cycle - BUY and EXIT alike, since an untrusted
local balance is exactly as unsafe for sizing a SELL as a BUY), fetches the
latest completed candles from the Testnet, and generates a signal. A BUY
signal is always logged and reported, never acted on - **this agent cannot
initiate a position on Testnet at all.** Only for an EXIT does it size,
risk-check, validate against live exchange filters, and (if every check
passes and the [kill switch](RISK_POLICY.md#kill-switch) is not engaged)
place one real (but play-money) order - and only to close, or help
recover, a position that already exists and has been fully reconciled
against the exchange. It is meant to be invoked once per completed 4h
candle, e.g. by a cron job or scheduled task:

```cron
# Run 5 minutes after every 4h candle closes (server time is UTC)
5 0,4,8,12,16,20 * * * cd /path/to/repo && .venv/bin/trading-agent --mode testnet run >> logs/cron.log 2>&1
```

> **A BUY signal is currently never acted on automatically on Testnet.**
> Automatic entry is disabled pending a verified exchange-resident
> protective stop order - see [RISK_POLICY.md](RISK_POLICY.md#protective-exits-why-max_risk_per_trade_pct-now-means-what-it-says-and-why-testnet-entry-is-disabled)
> for why. The `run` command will log and report a suppressed BUY signal
> rather than silently ignoring it. EXIT (closing a position you opened
> manually via the Testnet UI, or that a previous cycle opened) still
> works normally, provided local and exchange balances agree.

On its very first run, the agent reconciles its starting portfolio from
your actual Testnet account balance. If that account already holds a
nonzero base-asset balance, the agent refuses to start rather than guess a
cost basis - flatten the position manually via the Testnet UI first.

### Read-only Testnet connectivity check

```bash
trading-agent --mode testnet testnet-health
```

Before running `run` for the first time (or after rotating credentials, or
just to check things are working), this command verifies connectivity and
credentials **without ever placing, canceling, or modifying anything**:
server time, clock sync, BTCUSDT exchange filters, a signed account-info
call, and an open-orders query - all GET requests, nothing else. It also
reports (never modifies) local execution-state presence, a balance
comparison against the exchange when local state exists, and any
unresolved local pending orders. Exits non-zero on any failure - invalid
credentials, excessive clock drift, or a malformed/incomplete exchange
response all fail closed rather than reporting a false pass. Never prints
API keys, secrets, signatures, or signed query strings. See
[SECURITY.md](SECURITY.md) and [RISK_POLICY.md](RISK_POLICY.md) for the
full list of structural guarantees, and `tests/unit/test_testnet_health.py`
for their proofs.

```
[PASS] server_time: serverTime=1700000000000
[PASS] clock_sync: offset_ms=12
[PASS] exchange_info: tick_size=0.01 step_size=0.00001 min_qty=0.00001 min_notional=5
[PASS] account_info: signed GET /api/v3/account succeeded
[PASS] balances: BTC: free=0 locked=0; USDT: free=50 locked=0
[PASS] open_orders: 0 open order(s): none
[PASS] local_state: no local execution-state database present
overall: PASS
```

### Kill switch

```bash
trading-agent kill-switch engage --reason "reviewing strategy behavior"
trading-agent kill-switch status
trading-agent kill-switch disengage
```

Engaging the kill switch halts **all** order submission - both new
entries and exits - until manually disengaged. See
[RISK_POLICY.md](RISK_POLICY.md) for why exits are also blocked.

### Status

```bash
trading-agent status
```

Shows the current mode, kill switch state, and portfolio state (never
secrets).

## Shadow mode: forward-only observation of `multitimeframe_breakout_E1_round3`

Shadow mode continuously simulates the frozen, already-evaluated E1
candidate (`research/candidates/multitimeframe_breakout.py`) against real,
completed Binance BTCUSDT 1h candles going forward from a fixed start
boundary - **`2026-09-06T00:00:00Z`, and never earlier** (`shadow/boundary.py`).
It exists to observe how E1 would have performed on data it has never
seen, without a single dollar - real or Testnet - ever at risk. **It never
places an order of any kind.** See `shadow/engine.py` for the full design.

Everything about E1 itself - its rules, its 2R-net risk/reward policy, its
1h/4h/weekly logic, fees, slippage, sizing, and its already-published
research results - is completely unchanged by shadow mode. Shadow mode
only ever adds a new, independent, forward-only observation of it, in its
own database (`data/shadow_agent.db`, separate from every other database
in this project) and its own config (`config/shadow.yaml`).

### Exact local setup

```bash
git clone <this repository> && cd binance-testnet-trading-agent
git checkout claude/binance-testnet-trading-agent-h9lx17   # or your merged main branch
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q            # 727 tests should pass
ruff check .                   # should report no issues
mypy src                       # should report no issues
```

### First-run commands

```bash
# 1. Sanity-check the config and confirm the store is empty (fully local, no network call):
trading-agent --config config/shadow.yaml shadow-status

# 2. Run one cycle by hand to confirm connectivity (reads Binance's public,
#    unauthenticated market-data endpoint only - no API key is used or needed):
trading-agent --config config/shadow.yaml shadow-run

# 3. Schedule it to run once per completed 1h candle (5 minutes after each
#    hour, server time is UTC) - e.g. via cron:
crontab -e
# 5 * * * * cd /path/to/binance-testnet-trading-agent && .venv/bin/trading-agent --config config/shadow.yaml shadow-run >> logs/shadow_cron.log 2>&1

# 4. Check progress at any time (both are fully local/read-only, no network call):
trading-agent --config config/shadow.yaml shadow-status
trading-agent --config config/shadow.yaml shadow-report

# 5. Pause shadow observation at any time without losing any state:
trading-agent --config config/shadow.yaml shadow-kill-switch engage --reason "..."
trading-agent --config config/shadow.yaml shadow-kill-switch status
trading-agent --config config/shadow.yaml shadow-kill-switch disengage
```

`shadow-run` is idempotent and crash-recoverable: it re-derives its entire
result from the immutable candle history plus the frozen E1 strategy on
every invocation and persists only what is new since the last successful
cycle, in one atomic transaction, keyed so a completed candle can never be
scored twice - running it twice in the same hour, or resuming after a
crash mid-cycle, is always safe. A file-based lock
(`data/shadow.lock`) additionally refuses to let two `shadow-run`
processes execute at the same time.

**E1 requires ~7,564 completed 1h candles (~315 days) of warm-up before it
can generate its first signal.** Every `shadow-run` cycle before then
correctly reports `INSUFFICIENT_DATA` and simply keeps accumulating
history - this is expected, not an error. A promotion review requires at
least 30 CLOSED forward trades (`shadow-report` discloses progress toward
this on every run) and is, in any case, a separate manual decision this
tool does not make. **No output of this tool is ever a claim of
profitability, and nothing it produces authorizes Testnet or live
trading.**

## Documentation index

- [ARCHITECTURE.md](ARCHITECTURE.md) - module boundaries and data flow
- [RISK_POLICY.md](RISK_POLICY.md) - every configurable risk control and why it exists
- [STRATEGY.md](STRATEGY.md) - the baseline strategy's exact rules and limitations
- [TESTING.md](TESTING.md) - what is tested and how
- [SECURITY.md](SECURITY.md) - secret handling, endpoint restrictions, reporting
- [SCHEDULING_DESIGN.md](SCHEDULING_DESIGN.md) - the overlap-guard design required before any automatic scheduling is built (not yet implemented)
- [CHANGELOG.md](CHANGELOG.md) - version history

## Official Binance documentation consulted

- https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md
- https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md
- https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- https://github.com/binance/binance-spot-api-docs/blob/master/filters.md
