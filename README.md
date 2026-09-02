# mcp-trade-firewall

## Pending-order reconciliation

Every stock submission is evaluated against a fresh positions/open-orders
snapshot inside the serialized proxy mutation gateway. Outstanding quantities
are included before sizing; reconciliation fails closed when either broker read
is unavailable or changes during the check. Caller-provided target quantity is
bounded independently by the configured server-side per-symbol dollar and
equity limits. A caller fingerprint is not treated as an authorization secret.

An MCP proxy that enforces trading policy rules on tool calls before they
reach a trading MCP server.

## Layout

- `src/firewall/proxy.py` — MCP proxy server
- `src/firewall/policy.py` — rule engine
- `src/firewall/audit.py` — audit log writer
- `src/firewall/rules/` — individual rule implementations
- `src/core_strategy.py` — separate, non-firewall module that generates
  real demonstration trading activity through the proxy (see below) —
  not part of the firewall, makes no risk decisions
- `policies/default.yaml` — versioned policy config
- `corpus/` — attack payloads
- `evals/` — eval harness
- `tests/` — test suite

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env  # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY
```

## core_strategy (demonstration trading activity, not part of the firewall)

`src/core_strategy.py` is a small, separate module that generates real
trading activity for the governance layer (this firewall) to manage — it
is **not** part of the firewall and makes **no** risk decisions itself.
Every order it proposes goes through `place_stock_order` via the exact
same `firewall.proxy.build_proxy()` MCP proxy path every other caller
uses; there is no separate execution route and no exception carved out
from `notional_cap`/`position_cap`/`symbol_allowlist`/any other rule. This
module proposes, the existing firewall governs, exactly like every other
caller.

**Basket:** an expanded, fixed, disclosed basket of 11 liquid US tickers
hardcoded in `core_strategy.BASKET`: the 4 core assets (`AAPL`, `MSFT`, `SPY`,
`QQQ`) plus 7 second-order federal IT and defense contractors with verified,
high-dollar subaward linkages to Microsoft Corporation on USASpending.gov
(`GD`, `CACI`, `ACN`, `LDOS`, `NOC`, `BAH`, `J`). Reused verbatim from
`symbol-allowlist`'s own `allowed_symbols` in `policies/default.yaml`, running
against the unmodified default policy. `ASGN` was evaluated and dropped after
failing Alpaca free-tier 90-day bars availability. **Explicit scope boundary:**
"the MSFT->federal-contractor link was validated by hand for this specific pair,
not by a general-purpose resolution engine." AAPL/MSFT and the 7 contractors are
equities; SPY/QQQ are large-cap index ETFs — stated plainly.

**Allocation Decision:** all 11 names sit alongside each other in a unified
11-asset inverse-volatility risk-parity basket deploying 90% NAV with a 10% cash
buffer, governed under the same dynamic 25%-of-live-equity position cap
(approximately $25,000 in the historical ~$100k-account run) and $5,000
notional chunking rules. Chosen over a separate sub-allocation sleeve or a
replacement of the original four: a unified basket needs zero new risk
parameters — the same `position_cap`/chunking/rebalancing logic already
verified across AAPL/MSFT/SPY/QQQ governs all 11 names unmodified — and the
diversification effect is a real, observed one, not an asserted one: SPY's
raw inverse-vol weight dropped from ~44% in the 4-name basket to ~20.08% in
the paper-account 11-name sizing run (see the verified table below) purely from the
formula now averaging across seven more, generally lower-volatility names.

**Fresh-account submission milestone (2026-09-02):** the documented 90%-NAV
strategy ran against a pinned, previously empty Alpaca paper account with the
options overlay disabled. Alpaca independently reports 10 filled orders creating
9 basket positions (`AAPL`, `MSFT`, `SPY`, `GD`, `ACN`, `LDOS`, `NOC`, `BAH`,
and `J`), with zero open orders and position quantities matching the fills.
Thirteen other chunks were denied by the fail-closed `pending-order-exposure`
rule when the broker position/open-order snapshot was unavailable. Every blocked
chunk failed closed rather than guessing, which is why the account holds nine
correct positions instead of eleven uncertain ones. This is **real Alpaca
paper-account trading history**, not live-money trading history. The generated
run evidence is the dry-run artifact `data/cycles/20260902T172309369269Z.json`,
the execute artifact `data/cycles/20260902T172637036914Z.json`, Alpaca's own
order/position records, and the 1,726-record verified `audit.jsonl` chain.

**Position sizing & entry logic:**
> **"position sizes are set by inverse-volatility weighting using trailing realized volatility -- this allocates risk, not conviction, and makes no claim about expected returns or direction."**

> **"this entry logic makes no claim of predictive edge; it exists to generate real positions for the risk-governance layer (caps, CVaR, the hedge-trigger) to manage and demonstrate against."**

It is not a technical indicator, not a signal, and not a forecast of any
kind — deliberately the least sophisticated defensible option, consistent
with every other decision this project has made about not overclaiming (see
"What this does not do" below).

**Volatility pipeline:** Trailing realized volatility is computed by
**reusing `cvar_gate`'s existing historical-bars fetch (`fetch_daily_bars`)
and log-return calculation (`_log_returns`)** — no second volatility pipeline
is built. Standard deviation of daily log returns over the lookback window
(default 90 days, matching `cvar_gate`) determines each name's volatility
$\sigma_i$, giving target weights:
$$w_i = \frac{1/\sigma_i}{\sum_{j \in \text{basket}} 1/\sigma_j}$$
so each position contributes roughly equal risk to the basket rather than
equal dollars.

**Fixed-schedule drift-based rebalancing:** Rebalances on a fixed cadence
(`DEFAULT_INTERVAL_SECONDS`, default 24h). Only places rebalancing orders if
a position's current portfolio weight has drifted beyond a configured
threshold (`DEFAULT_DRIFT_THRESHOLD`, default 5%) from its target weight —
avoiding unnecessary turnover and unnecessary firewall load. This rule is
direction-neutral: a falling position that becomes underweight can generate a
buy back toward target, so drift rebalancing is **not** the system's automatic
crash de-risking mechanism. The only automatic crash de-risking path is a
strategy-generated sell passing through the drawdown killswitch's narrow
deleveraging exception after cumulative session P&L breaches -$1,000: while
risk-adding orders remain blocked, a plain-equity sell may proceed only when a
fresh authoritative broker snapshot proves that the order cannot exceed the
held quantity. The exception permits a qualifying sell; it does not itself
invent or size one.

> **Current options status (supersedes the historical implementation notes
> below): option execution is disabled pending an upstream schema and
> risk-control rebuild.** `PolicyEngine.evaluate()` hard-blocks option-order
> tool names, OCC option-symbol payloads, `mleg`, and any payload containing a
> `legs` structure before configured option rules run. Bracket/OCO/OTO,
> `take_profit`, and `stop_loss` shapes are likewise hard-blocked by structural
> presence, including empty or null values. The overlay may still compute and
> audit a proposal, but no option order is forwarded. Descriptions below of
> contract selection and downstream option rules document dormant proposal/
> rule machinery, not a currently functional execution overlay.

**Scheduled options overlay — two audit records, not one:** the reactive
hedge trigger (above) never places an order, so it only ever needs one
audit record (`rule_id: hedge-proposal`). The scheduled overlay is
different — it submits a real `place_option_order` call, so it produces
**two** separate records in `audit.jsonl`: a provenance record
`core_strategy.place_basket_orders` writes directly, before submitting
(`tool_name: "scheduled_overlay:proposed"`, `rule_id:
"scheduled-options-overlay"`, `verdict: "info"`), and the ordinary record
`PolicyEngine.evaluate()` writes for that same call. Its current `rule_id` is
always `option-orders-disabled`; historical downstream examples included
`option-spread-guard`, `hedge-cost-cap`, and `net-delta-floor`. The
provenance record is unconditional and written first, so it survives even
when the real evaluation hard-blocks the order — a dashboard reading
`audit.jsonl` can tell "this option order came from the scheduled overlay"
and "this is what the firewall's own rules decided about it" apart, without
either fact silently swallowing the other.

**Scheduled overlay contracts are resolved from Alpaca's real chain, not
asserted from arithmetic.** Earlier, `compute_scheduled_overlay` fed
`_mechanical_strike`/`_mechanical_expiry`'s raw output straight into
`format_occ_symbol`, producing an OCC-format string for a strike/expiry
combination that was never checked against what Alpaca actually lists —
in practice this almost always names a contract that does not exist, and
every submission hard-blocked with `option-spread-guard`'s "no snapshot
returned for `<symbol>`". **That failure was, at the time, misdiagnosed as
a free-tier market-data limitation — it was not.** Verified directly: the
same `indicative` feed (`DEFAULT_OPTION_FEED`, unchanged, never the bug)
returns real, live quotes for real listed contracts on the same
underlying — confirmed by querying Alpaca's chain snapshot endpoint
directly and seeing populated bid/ask data for genuinely listed strikes.
The actual defect was upstream of the fetch: a strike/expiry pair can be
mechanically "correct" (right OTM%, right day-count window) while
matching no contract Alpaca has ever listed. `resolve_listed_contract`
(`src/firewall/market_data.py`) fixes this — it calls Alpaca's real
options chain (`GET /v2/options/contracts`, the Trading API, a different
host/surface from the quote/bars endpoints and one that carries no `feed`
parameter at all) and snaps the mechanical target to the nearest contract
Alpaca actually lists: nearest listed expiry to the target (restricted to
expiries satisfying `option_expiry_floor`'s own 7-day DTE minimum, so a
resolved contract is never one that rule would hard-block on arrival
anyway — if the literal-nearest expiry violates the floor, this snaps to
the nearest one that also satisfies it, not the next-nearest ignoring the
floor). The OTM%/target-date heuristic itself is unchanged — still fully
mechanical, still disclosed, no forecasting; only the final step changed.
`ScheduledOverlayProposal`'s `strike`/`target_expiry`/`occ_symbol` now
reflect the RESOLVED contract, with the original mechanical target
preserved in the audit reason text for transparency. If no real contract
resolves at all, `compute_scheduled_overlay` returns `None` (the same
outcome as its pre-existing "no positions to hedge" case) rather than
ever asserting a phantom symbol.

**Strike selection is by DELTA, not price.** At the chosen expiry,
`resolve_listed_contract` does not pick the strike nearest the mechanical
OTM% target by price — it pulls real deltas (via `fetch_option_quotes`,
the same batched snapshot fetch `option_spread_guard`/`net_delta_floor`/
`hedge_cost_cap` already use) for the strikes nearest that target, and
picks whichever has a delta closest to `DELTA_CORRIDOR_CENTER` (0.325,
the midpoint of `net_delta_floor`'s real enforced `structural_delta_floor`
of 0.15 and a disclosed, non-enforced upper anchor of 0.50). The OTM%
target is only a rough starting anchor for where to look in the chain,
never the final criterion — selection by a measured Greek, not a market
view, consistent with the rest of this corridor. The search window
widens adaptively (starting at `DEFAULT_STRIKE_SEARCH_COUNT`, doubling,
re-querying only newly-added strikes) until the searched deltas straddle
the corridor center on both sides or the full listed chain at that
expiry is exhausted — a fixed narrow window can otherwise silently settle
for "closest available in an arbitrarily small slice" rather than the
true closest-to-center strike; verified directly against a real chain
(SPY, whose delta moves slowly per dollar of strike near this OTM range,
needed a search several times wider than the 20-strike starting point
before reaching a strike anywhere near the corridor center at all).
Resolution fails closed if even the best strike found after exhausting
the chain still can't clear `DELTA_CORRIDOR_FLOOR` — the one part of the
corridor a rule (`net_delta_floor`) actually enforces; the ceiling is
never a fail-closed reason, since no rule blocks on |delta| being too
high.

**Why every order is a plain market order (`qty` only, never
`limit_price` or `notional`):** live-verified directly against Alpaca's
real paper API — a `place_stock_order` call with `type: "market"` and a
`limit_price` set is unconditionally rejected by Alpaca itself with HTTP
422 (error code 40010001, "market orders require no stop or limit
price"), independent of anything the firewall does. `limit_price` had
briefly been attached to these orders to give `notional_cap`/
`position_cap` something to compute a notional from directly; that
approach cannot work against the real API and was reverted. The target
dollar allocation is instead converted to an integer share count locally
(`core_strategy.compute_target_quantities`, sized off a recent close via
the same `firewall.market_data.fetch_daily_bars` helper `cvar_gate`/
`pct_of_adv` already use), and `notional_cap`/`position_cap` independently
fetch their **own** reference price for any plain qty-only stock order
(see "Dynamic, capped risk-parity allocation" below) rather than trusting
a client-supplied price field. Every dollar figure a rule enforces is
therefore derived from a price the rule itself fetched, not one the order
happened to carry.

**Dynamic, capped risk-parity allocation, chunking, and throttle-safe
pacing.** The basket budget, the per-order and per-symbol caps, and the
pacing all read live off the real account and the real loaded policy —
none of it is a flat dollar figure sized to make a demo look good.

- **Basket budget** is `account_equity * 90%`
  (`DEFAULT_BASKET_PCT_OF_NAV`, `compute_total_budget_usd`) — equity read
  from the same `GET /v2/account` fetch `state["account_equity"]` already
  uses, not a second call. 90%, not 100%, deliberately leaves margin
  headroom rather than targeting full account utilization exactly.
- **`notional_cap`** (`policies/default.yaml`) is `max_pct_of_equity:
  0.05` — 5% of live equity, with the pre-existing `max_usd: 5000` kept as
  a static fallback for missing equity. At this account's real ~$100k
  equity the two numbers coincide almost exactly ($4,998.88 vs $5,000);
  that is a real, live-verified coincidence of this account's current
  scale, not a value chosen to make any specific order pass — chunking
  below exists precisely because a single order routinely exceeds it.
- **`position_cap`** is `max_pct_of_equity: 0.25` — 25% of live equity
  (the tighter end of a considered 25–30% range), static fallback
  `max_usd_per_symbol: 20000` unchanged. Chosen deliberately tight enough
  that the basket's real dominant name (SPY, see below) actually clips
  against it — "a limit that's never reached is not a meaningfully
  verified limit."
- **Weight clipping, not redistribution**
  (`core_strategy.clip_weights_to_position_cap`): after computing raw
  inverse-vol weights, any name whose target notional
  (`weight * total_budget_usd`) would exceed `position_cap`'s real ceiling
  (`account_equity * 0.25`, the *same* figure the firewall itself
  enforces, read off the live rule instance — see
  `_read_dynamic_policy_config` — never a duplicated constant) has its
  weight clipped down to exactly that ceiling. The clipped-off amount is
  **left as uninvested cash**, not redistributed to the other names, and
  every clip writes a dedicated, non-blocking audit record
  (`basket_rebalance:weight_clipped`, `verdict: "info"`) disclosing the
  symbol, raw weight, capped weight, and ceiling — visible on the
  dashboard, not a silent truncation.
- **Notional-cap-aware chunking**
  (`core_strategy.split_order_into_chunks`): any resulting order whose
  notional would exceed `notional_cap`'s real effective ceiling is split
  into `floor(ceiling / price)`-share chunks (the last chunk carrying the
  remainder), each submitted sequentially through the *same* firewall
  path as a single order would be — no separate execution route. Every
  multi-chunk order writes a `basket_rebalance:order_chunked` audit
  record disclosing the real chunk count and every chunk's size/notional.
- **Throttle-safe pacing** (`core_strategy.ThrottlePacer`): chunking can
  turn one logical order into several real ones, which can approach
  `order_rate_throttle`'s real 20-orders-per-60-seconds limit on its own —
  a rule that, once tripped, "stays paused until a human explicitly
  resets it" (see that rule's entry below), making an accidental trip
  expensive to recover from. The pacer tracks real submission timestamps
  in a sliding window and sleeps before any submission that would exceed
  a **75% safety margin** of the real configured limit (15 of 20,
  `order_rate_max_orders`/`order_rate_window_seconds` read off the same
  live rule instance as the caps above) — never the raw limit itself,
  so normal jitter in submission timing can't tip it over the real edge.
- **`drift_threshold` stays at its existing 5% absolute-weight band**
  (`DEFAULT_DRIFT_THRESHOLD`, unchanged), a deliberate decision made
  independent of any single run's outcome: a no-trade tolerance band
  around target weight is standard practice for tolerance-band rebalancing
  (avoiding turnover from noise-level fluctuations that carry no material
  risk consequence), not a threshold tuned to make a particular cycle's
  order count look larger or smaller.

**Paper-account verified sizing snapshot, 2026-08-30 (equity $99,977.54,
`python -m core_strategy`, 11-asset expanded risk-parity basket, real current positions, real
market prices):** target budget $89,979.79 (90% NAV), $11,625.08 cash buffer (11.63% NAV),
25% position cap ($25,000 ceiling), $5,000 per-chunk ceiling with `ThrottlePacer` spacing:

| symbol | 90d realized vol | target weight | chunks | shares | price | deployed notional |
|---|---|---|---|---|---|---|
| **SPY** | 0.86% | 20.08% | 4 | 21 | $769.28 | $16,154.88 |
| **GD** | 1.41% | 12.29% | 3 | 29 | $379.32 | $11,000.28 |
| **QQQ** | 1.62% | 10.67% | 2 | 11 | $716.44 | $7,880.84 |
| **J** | 1.71% | 10.10% | 2 | 59 | $152.03 | $8,969.77 |
| **NOC** | 1.84% | 9.43% | 2 | 15 | $545.46 | $8,181.83 |
| **AAPL** | 2.03% | 8.54% | 2 | 22 | $319.58 | $7,030.76 |
| **LDOS** | 2.53% | 6.83% | 2 | 43 | $140.52 | $6,042.36 |
| **BAH** | 2.73% | 6.34% | 2 | 75 | $75.25 | $5,644.12 |
| **MSFT** | 2.73% | 6.34% | 1 | 9 | $513.67 | $4,623.03 |
| **CACI** | 3.48% | 4.97% | 1 | 7 | $627.84 | $4,394.85 |
| **ACN** | 3.93% | 4.40% | 1 | 20 | $189.59 | $3,791.80 |

All 22 chunked stock orders cleared the real firewall
(`verdict: "allow"`, `upstream_status: "ok"`) with zero rule violations
and zero `order_rate_throttle` engagement (pacer submitted 15 chunks, safely
slept ~53s, and submitted the remaining 7 chunks); the scheduled options overlay
(`SPY260930P00758000`, delta -0.3225) was proposed and correctly evaluated by
`net_delta_floor`. Every order and decision above is recorded in `audit.jsonl`
with cryptographic hash-chain integrity verified (`verify_chain(audit_path)` passes).

**Running it:**

```bash
python -m core_strategy            # one cycle: inverse-vol rebalance check
python -m core_strategy --loop     # repeat every --interval-seconds (default 24h)
python -m core_strategy --loop --cycles 3 --interval-seconds 60
```

Requires the same `.env` (`ALPACA_API_KEY`/`ALPACA_SECRET_KEY`) and
`ALPACA_PAPER_TRADE=true` as the proxy itself — `build_proxy()` refuses to
start otherwise (see `_require_paper_trade_mode`). Tested end-to-end
in-process against a fake upstream and the real, unmodified
`policies/default.yaml` (`tests/test_core_strategy.py`), not just unit
tests of the sizing/payload helpers in isolation.

**Not built (deliberately out of scope):** a technical-indicator variant,
an LLM-driven decision loop, or any new risk logic of its own — this
module needs none of `firewall.rules`' existing types extended or
duplicated.

## Hedge proposal (detection + audit only)

`hedge-proposal` (`src/firewall/rules/hedge_proposal.py`) is a single new
triggered action, not a new pillar: when `cvar_gate`'s CVaR estimate or
`drawdown_killswitch`'s session PnL crosses a configured early-warning
threshold (`cvar_trigger_pct_of_max_loss`/`drawdown_trigger_pct_of_threshold`,
both a fraction of each rule's *own* already-configured cap — see
`policies/default.yaml`), it computes ONE defined protective structure — a
protective put on the single position contributing most to the flagged
figure — via a disclosed, mechanical formula
(`_mechanical_strike`/`_mechanical_expiry`/`_mechanical_contracts`): strike
N% out-of-the-money, a target expiry date at the midpoint of a configured
day-count window, and a contract count covering a configured percentage of
the flagged position's notional. **This is a defensive response to an
already-measured risk number, never a market view or a forecast** — every
proposal's audit reason states this explicitly, and every place this
feature is described must describe it the same way.

**Detection + audit only.** `compute_proposal()` never calls any
order-placement tool; nothing is ever submitted. This firewall has no
approval-token preview → token → submit flow for *any* order today (see
"What this does not do" below) — verified against the entire codebase and
git history before this feature was built, not assumed. A hedge proposal
must go through that exact flow once it exists, with no exception for
being "protective" — since it doesn't exist, this feature can only ever
report a proposal to the audit log, never place one.

A triggered proposal is written directly to the audit log
(`rule_id: hedge-proposal`, `verdict: soft_block`), bypassing
`PolicyEngine`'s normal `RuleOutcome` → `Warning` → audit pipeline on
purpose: that pipeline silently drops a soft rule's warning whenever the
same call *also* hard-blocks (`record_call_pending`/`record_call_outcome`
both no-op on `hard_block` — see `policy.py`), and `cvar_gate`
hard-blocking on a large tail-loss estimate is exactly the moment a hedge
proposal matters most. `HedgeProposalRule.check()` is therefore
intentionally always a no-op (`RuleOutcome(False)`) — it exists purely so
its own config loads through the same `_Params`/`RuleConfig` machinery
every other rule uses; `FirewallMiddleware` calls `compute_proposal()`
directly and writes the record itself
(`tests/test_proxy.py::test_hedge_proposal_survives_even_when_the_same_call_hard_blocks`
proves this survives a same-call hard block).

Reuses state, not a second risk calculation: the `cvar_gate` trigger calls
`compute_cvar`/`_log_returns` against the *live* `CVaRGateRule` instance's
own `.cfg` and `._bars_fetcher` (same pattern `sizing_resolver.py` already
established); the `drawdown_killswitch` trigger reads
`state["session_pnl_usd"]` against the live `DrawdownKillswitchRule`
instance's own `.cfg.session_pnl_threshold_usd`. Since
`drawdown_killswitch`'s PnL is a single account-wide scalar with no
per-symbol breakdown, "the position contributing most" for that trigger is
approximated from `order_history` (largest absolute net notional across
recorded, still-live order events) — explicitly not a per-symbol
realized-loss attribution, documented as such in
`hedge_proposal.py`'s module docstring.

**Formerly a disclosed gap, now closed:** the `cvar_gate` trigger needs
`state["account_equity"]`. `FirewallMiddleware._populate_account_state`
(`src/firewall/proxy.py`) now sets it on every account-state-relevant
call, read from the same `GET /v2/account` response
`state["session_pnl_usd"]` already used (`firewall/account_data.py`'s
`AccountPnLResult.equity` field) — one fetch, not a second one. Both the
`cvar_gate` and `drawdown_killswitch` hedge-proposal triggers are now
proven end-to-end through the real proxy, not just unit-tested in
isolation. Deliberately omitted from all 5
`preset_*.yaml` files (documented here, not left silent): this is a
detection/reporting feature, not a blocking control, so it doesn't belong
in the strictness-gradient sweep those presets exist for.

**Trigger normalization and release review:** when `cvar_gate`'s or
`drawdown_killswitch`'s trigger condition for an open hedge normalizes
(e.g. session PnL recovers above the trigger threshold, or CVaR falls
back within limits, or the underlying position is closed), the firewall
writes a visible flag to the audit log/dashboard (`"hedge on $X: trigger
condition resolved, review for release"`) and attaches the same note as
plain informational context on the next tool result the agent naturally
receives. This operates strictly in-band on intercepted tool calls without
any background scheduler, injected authoritative instructions, or
fabricated turns.

## What this does not do

- **No order in this system goes through a preview → token → submit
  approval flow.** Every order-placement call today is evaluated once
  (`PolicyEngine.evaluate`) and either forwarded or hard-blocked — there is
  no preview step, no token, and no separate confirmation step for any
  tool, place/replace/cancel or otherwise. The only approval-like primitive
  that exists at all is `blast_radius`'s `human_approved` state flag, and
  it is itself never set anywhere in `src/` (the same "uncomputed state"
  pattern `session_pnl_usd` had before it was wired — see
  `firewall/account_data.py`), so it's also currently inert. This matters
  beyond `blast_radius`: `hedge-proposal` (below) computes protective
  option structures but is deliberately detection + audit only, precisely
  *because* no such flow exists to route a proposal through — verified
  against the full codebase and git history before that feature was built,
  not assumed. Building this flow (who issues a token, what a preview tool
  surface looks like, token expiry/replay/binding to the exact previewed
  order, wiring every existing order tool through it) is a separate,
  foundational piece of work, not a side effect of any rule.

- **`cooldown_after_loss` cannot trip in production: its windowed,
  realized-P&L input (`pnl_history`) has no source.** `drawdown_killswitch`
  (a *scalar*, cumulative session-PnL check) was reactivated by fetching
  `session_pnl_usd` fresh from Alpaca's own `GET /v2/account` on every
  order-related call (`equity - last_equity`, Alpaca's own "day P&L"
  figure — see `firewall/account_data.py`). `cooldown_after_loss` needs a
  different shape of data — a rolling window of individual *realized*
  P&L events, not a single number — and no Alpaca endpoint provides that
  cleanly: verified against the real API docs before ruling this out,
  `GET /v2/account/portfolio/history`'s `profit_loss` is cumulative
  mark-to-market P&L from a period base value (includes *unrealized*
  gains/losses, not realized-only — a real semantic mismatch with this
  rule's own "realized loss" framing, not just a resolution problem), and
  `GET /v2/account/activities`'s FILL records carry no P&L field at all
  (price/qty/side/symbol/order_id only). Getting genuine realized-only
  windowed events would require a locally-maintained cost-basis ledger
  (matching buy/sell fills per symbol) — explicitly rejected as
  disproportionate to what this one rule needs: `cooldown_after_loss`
  stays inert, `self._pnl_history` stays an always-empty `PnLHistory`,
  and this is a deliberate, documented gap, not an oversight. Revisit
  only if a real realized-P&L source becomes available, or if the
  decision to redefine the rule around total (realized + unrealized)
  P&L is deliberately made later — that would change what the rule's
  halt actually means (an open position sitting at an unrealized paper
  loss could trip it with nothing having closed) and needs its own
  explicit sign-off, not a silent wiring choice.

- **The autonomous scheduler defaults to an unbounded cycle count.**
  `run_loop.py` supports `--max-cycles`, and the documented operating commands
  supply it explicitly, but omitting the flag leaves `max_cycles=None` and the
  scheduler continues until it receives a stop signal. Market-clock gating,
  live-mode account pinning, and the single-cycle lock still apply, but they do
  not turn an unlimited run into a bounded one. Treat explicit `--max-cycles`
  plus periodic heartbeat, cycle-state, and broker-order checks as required
  operating procedure; changing the default remains disclosed follow-up work.

- **`cvar_gate` is a noisy tail estimate on short lookback windows.**
  It computes CVaR via historical simulation (empirical daily log returns
  from Alpaca's Market Data API, no parametric distribution assumed), which
  means the tail it estimates is only as good as the number of tail
  observations in the lookback window. At the default 90-day lookback and
  95% confidence, the "worst 5%" being averaged is only ~4-5 data points.
  This is an inherent limitation of historical CVaR, not a bug: widening
  the window trades this noise for staleness (older data reflecting a
  different volatility regime), and no lookback length escapes that
  tradeoff. Treat `cvar_gate` as one signal among several, not a precise
  risk bound.

- **`cvar_gate` is not a delta-normal approximation for options — it does
  not convert an option position to delta-equivalent share exposure at
  all.** A delta-normal model would scale a position to `delta * 100 *
  contracts * underlying price` before applying the underlying's return
  distribution to that figure. `cvar_gate.check()` does none of that: for
  a `place_option_order` call it calls the shared `extract_notional()`
  helper with no `contract_multiplier` override, so `notional` is raw
  `qty * limit_price` — the option's per-share premium, uncorrected for
  the fact that one contract represents 100 shares (the same 100x
  undercount `notional_cap` had before its own, separate 2026-08-24 fix,
  see below — `cvar_gate` was never part of that fix). The underlying's
  daily % returns are then applied directly to that (already 100x too
  small) premium figure via `pnl_series = [notional * r for r in
  returns]`. The result, if this path were ever reached, would be a CVaR
  estimate several orders of magnitude smaller than the option's real
  premium outlay, let alone its real delta-driven sensitivity to the
  underlying — `cvar_max_loss_pct_of_equity`'s threshold would be
  essentially unreachable for any option order through this path,
  regardless of how directional or oversized it actually is.

  **This path is currently structurally unreachable, not just
  under-protective** — `evaluate()` short-circuits at the first triggered
  hard rule (see `policy.py`), and `symbol_allowlist` (line ~205 of
  `policies/default.yaml`) sits before `cvar_gate` (line ~341). Verified
  directly against that config, not assumed: `symbol-allowlist` is
  `enabled: true`, `severity: hard`, and its `allowed_symbols` (`["AAPL",
  "MSFT", "SPY", "QQQ"]`) are all plain tickers, none OCC-format. Every
  single-leg `place_option_order` call carries an OCC-format `symbol`
  (e.g. `"AAPL260918P00220000"`), which can never exactly-match one of
  those, so `symbol_allowlist` unconditionally hard-blocks it before
  `cvar_gate` ever runs. A multi-leg call (no parent `symbol`) reaches
  `cvar_gate`, but `cvar_gate.check()`'s own precondition (`if not symbol:
  return RuleOutcome(False)`) no-ops on it immediately — same gap this
  document's "Order modification... and bracket/multi-leg orders" bullet
  above already discloses for `cvar_gate`/`position_cap`/`pct_of_adv`
  generally. So no option order reaches this weakly-scaled CVaR path in
  the live default policy today; this is dormant, disclosed code, the
  same shape of gap as `hedge_proposal`'s `cvar_gate` trigger (see
  above). That conclusion is scoped to the shipped `policies/default.yaml`
  only — a different policy file that reorders these rules, or allowlists
  an OCC-format string, would reach this path for real.

  A separate rule, `net_delta_floor`, computes real per-contract delta
  (fetched from the same options-snapshot endpoint `option_spread_guard`
  uses) and correctly applies the x100 contract multiplier — but for its
  own, narrower purpose: hard-blocking a single-leg option order whose
  *net portfolio delta* (existing shares + this order's delta
  contribution) would drop below a configured floor, i.e. "does this
  still function as a hedge," defaulting any underlying with no recorded
  share position to 0 shares (so an unbacked option buy always reads as
  purely directional). It does not compute or report a CVaR/tail-loss
  figure, and does not backstop this gap in `cvar_gate` even where
  reachable.

  **Update:** `net_delta_floor` is now registered in
  `firewall.rules.RULE_TYPES` and listed in `policies/default.yaml`
  (`net-delta-floor`, alongside two new siblings, `hedge_cost_cap` and
  `option_sell_guard` — see the next bullet) — it is a live rule,
  evaluated by `PolicyEngine.evaluate()` for every `place_option_order`
  call, not validation embedded only inside `hedge_proposal.py`'s own
  proposal-generation step (which performs no validation of its own at
  all). This closes the exact gap this paragraph used to describe: code
  that existed, was tested, but could not fire for any call at all,
  hedge-triggered or otherwise — the same class of bug conformance-audit
  finding A4's Fix 1 (string-coercion) was built to close, reopened in a
  new corner of the system if left unregistered.

  This does **not** change any allow/block outcome in the shipped default
  policy today, for either shape of call — verified directly (one
  `evaluate()` call against the real loaded policy), not assumed. For
  single-leg: `net_delta_floor` is now live and, with
  `underlying_share_positions` unpopulated (see below), it correctly
  hard-blocks every single-leg option BUY whose net delta reads negative
  against a defaulted-to-zero share position — but `symbol_allowlist`
  hard-blocks every single-leg option order unconditionally regardless
  (OCC symbol never matches a plain ticker), so the final decision is
  still always hard_block either way; registration only changes which
  rule's reason gets reported for the cases `net_delta_floor` catches
  first, same as `option_expiry_floor`/`option_spread_guard`'s own
  placement ahead of `symbol-allowlist`. For multi-leg: `net_delta_floor`
  is single-leg only (same scope boundary as `option_spread_guard` — it
  self-scopes via a valid OCC-format parent `symbol`, which no multi-leg
  call carries), so registering it changes nothing for multi-leg calls
  either — a multi-leg all-buy order on allowlisted underlyings still
  reaches `allow` end-to-end after this change, exactly as before it.

  **Consequently, `net_delta_floor` is now enabled and correctly
  hard-blocks essentially every reachable single-leg option BUY it can
  assess** (the missing `underlying_share_positions` baseline always
  reads as "no offsetting stock position," so a directional-reading net
  delta is the common case, not the exception) — stated here as the
  behavior, not glossed as outcome-neutral: it happens to be moot for
  single-leg only because `symbol_allowlist` already blocks that shape
  unconditionally. A future policy that relaxes `symbol_allowlist` (an
  OCC-format allowlist entry) or reorders rules would make this rule's
  own blocking behavior the live, load-bearing outcome, not just the
  reported reason — this is the conservative, correct-by-default
  direction for a missing baseline (same "over-blocking on incomplete
  data is the correct failure direction" bias `pct_of_adv.py` already
  states), not an oversight.

  `iv_hv_ratio` (implied-vs-historical volatility richness gate) is in the
  identical pre-registration state `net_delta_floor` was in before this
  update — tested code, not yet in `RULE_TYPES` or `default.yaml`. It was
  deliberately **not** part of this remediation (not one of the checks
  this change was scoped to) and remains a separate, disclosed, still-
  dormant gap.

- **`hedge_cost_cap` and `option_sell_guard` are new option-specific hard
  rules, both registered `RULE_TYPES` entries evaluated for every
  `place_option_order` call, regardless of origin.**
  `hedge_cost_cap` (`src/firewall/rules/hedge_cost_cap.py`) caps a
  single-leg option BUY's total premium cost (live `ask` x 100 x `qty`,
  from the same options-snapshot fetch `option_spread_guard`/
  `net_delta_floor` already use) to a configured fraction of account
  equity, reusing `cvar_gate`'s own `account_equity_state_key` — so it
  fails closed on missing/non-numeric equity exactly like `cvar_gate`
  would, but `state["account_equity"]` is now populated on every real
  call (see the hedge-proposal section above) so this is no longer a live
  gap. Explicit, documented decision on what `extract_notional()` computes
  for options (resolved before this rule was written, per that module's
  own docstring): TOTAL PREMIUM — `qty * limit_price * 100` — never
  underlying notional and never an unmultiplied `qty * price`.
  `hedge_cost_cap` protects that exact same quantity, premium dollars,
  against an equity-relative cap instead of `notional_cap`'s flat USD
  cap; it deliberately reads the live `ask` rather than the order's own
  `limit_price` (a market order carries no `limit_price` at all, and
  `ask` is the conservative, market-derived cost estimate for a buy) —
  same protected quantity, different price source, not a divergence.

  `option_sell_guard` (`src/firewall/rules/option_sell_guard.py`) hard
  blocks every option-sell order — single-leg `side == "sell"`, or any
  leg of a multi-leg order not verified as a buy (a leg present but
  missing/unparseable `side` fails closed too) — as a **stated scope
  limitation, not a silent gap**: every check in this corridor
  (`option_expiry_floor`, `option_spread_guard`, `net_delta_floor`,
  `hedge_cost_cap`) prices or sizes an order assuming premium is being
  spent, not received, and none of them — nor `notional_cap`/
  `position_cap` — model the uncapped assignment risk of a short option.
  **Consequence:** any structure with a short leg — a covered call, a
  cash-secured put, a collar (sell call + buy put) — is out of scope for
  this firewall today. Building real collateral/margin/assignment-risk
  controls for short options was explicitly out of scope for this change.

- **`hedge_regime_call_guard` hard blocks buying a CALL while a CVaR/
  drawdown hedge-trigger regime is active — reusing `hedge_proposal`'s own
  trigger detection verbatim, not a re-derived calculation.**
  (`src/firewall/rules/hedge_regime_call_guard.py`) Not redundant with
  `net_delta_floor`: a call has positive delta, so buying one against an
  assumed long stock position pushes net delta MORE positive, which
  `net_delta_floor`'s lower-bound checks (`net_delta < floor`,
  `abs(raw_delta) < structural_floor`) structurally cannot catch — this
  rule catches that failure mode on the option's TYPE (a free OCC-symbol
  parse, end-anchored the same way `parse_occ_underlying`/`parse_occ_expiry`
  already are — verified, not assumed, before writing the new
  `parse_occ_option_type` helper), not its delta magnitude, so it needs no
  live quote at all. Calls `hedge_proposal.compute_proposal()` directly
  with its own `tool_name`/`arguments`/`state`, and the same live
  `CVaRGateRule`/`DrawdownKillswitchRule`/`HedgeProposalRule` instances
  hedge_proposal's own triggers use — read from three new state keys
  (`hedge_proposal_rule`, `cvar_gate_rule`, `drawdown_killswitch_rule`),
  the same established pattern `iv_hv_ratio` already uses for its own
  `cvar_gate_rule` key. **Best-effort detection reuse, not a fail-closed
  market-data check** — a deliberate divergence from
  `net_delta_floor`/`option_spread_guard`/`hedge_cost_cap`'s own
  convention: if that state isn't wired (as of this writing, nothing in
  `src/` wires it — the same disclosed gap `iv_hv_ratio`'s own state key
  has), this rule cannot determine whether the regime is active and does
  **not** block, mirroring `compute_proposal()`'s own "no proposal" result
  for the same missing inputs rather than treating "can't tell" as "must
  block." Of its two trigger paths, only `drawdown_killswitch`'s is
  actually reachable through the real proxy once wired
  (`session_pnl_usd`/`order_history` are already populated on every
  order-related call); the `cvar_gate` path is not, for the same reason
  `cvar_gate`-on-options is already documented above as structurally
  unreachable (this rule reads the option's own OCC `symbol` as
  `cvar_gate`'s `symbol_field`, which its bars fetcher cannot resolve as a
  stock ticker). **Explicitly assumes the flagged position is long
  stock** — the same assumption `hedge_proposal` itself already makes (it
  only ever proposes a protective put) — a short position needing a
  protective call instead is out of scope. Buying a call is always a
  no-op for `side == "sell"` (handled unconditionally by
  `option_sell_guard` instead) and for multi-leg orders (no parent
  `symbol` to parse a type from — a collar's short leg is already blocked
  by `option_sell_guard` regardless).

- **Earnings-driven IV-crush protection is out of scope: no earnings-
  calendar data source exists in Alpaca's MCP server.** Checked directly
  against `alpaca-mcp-server` 2.3.0's bundled OpenAPI spec, not assumed —
  its corporate-actions endpoints (`get_corporate_actions`,
  `get_corporate_action_announcements`) cover exactly 15 capital-
  structure/registrar event types (splits, mergers, dividends, spin-offs,
  name changes, redemptions, etc.), and earnings is not among them; the
  full 47-tool read-only surface has no fundamentals/estimates/earnings-
  calendar tool anywhere. No code exists for this (`iv_crush`/`earnings`/
  `corporate_action` are all zero hits in `src/`) — a missing-input
  problem, not an unresolved scoping one. Worth revisiting only if an
  external earnings-calendar source (e.g. Benzinga, Polygon) is ever
  added to this project; Alpaca alone won't supply it.

- **Order modification (`replace_order_by_id`) and bracket/multi-leg orders
  (`order_class`/`legs` params) have scoped rule coverage.**
  `replace_order_by_id` matches the generic `tool_match: ["order"]` pattern
  used across rules, but Alpaca's replace endpoint is keyed by `order_id`
  alone (no `symbol` argument). `notional_cap` explicitly guards this tool via
  `sizing_tool_match: ["replace_order_by_id"]`, failing closed (`hard_block`,
  reason `"cannot compute notional — incomplete order data, failing closed"`)
  whenever notional cannot be computed from the call's arguments (e.g. a partial
  qty-only amendment where the resting limit price is omitted), and hard-blocking
  whenever computed notional breaches `max_usd`. However, because `replace_order_by_id`
  carries no `symbol`, symbol-dependent rules (`symbol_allowlist`, `position_cap`,
  `cvar_gate`, `pct_of_adv`) skip evaluation rather than looking up the original
  order's symbol from upstream state. We deliberately did not wire
  `replace_order_by_id` into `blast_radius`: that rule's mechanism (hard-block
  unless `state.human_approved`) exists for *bulk* actions (SEC Rule 15c3-5(b);
  FINRA Regulatory Notice 15-09), and routine single order-price amendments must
  not be blocked (see `corpus/benign.yaml` `benign-014`). Bracket/multi-leg orders
  have leg-level scoping boundaries: top-level `qty`/`limit_price`/`notional` are
  checked by `notional_cap` (including the 100x options multiplier for
  `place_option_order`), but individual bracket take-profit/stop-loss legs and
  complex multi-leg strategies are not summed per-leg. Treat `order_class="bracket"/"oco"/"oto"`
  legs and symbol-dependent caps on `replace_order_by_id` as **governed strictly
  at the parent notional level** until dedicated order-state lookup rules are added.

  **Update, 2026-08-24 — partial remediation, `place_option_order`
  specifically:** two of the five checks in this paragraph's own list
  (`symbol_allowlist`, `notional_cap`) were extended for `place_option_order`,
  closing the sharpest part of this gap for that one tool — but not the
  same way. Verified against the live inputSchema (`uvx --offline
  alpaca-mcp-server`) that a multi-leg call carries no parent `symbol` at
  all: `symbol_allowlist` now falls back to inspecting `legs`, checking
  each leg's own OCC-format symbol's underlying (`_util.parse_occ_underlying`)
  against the allowlist, failing closed on any leg it can't parse.
  `notional_cap` does **not** read `legs` at all — it applies a 100x
  contract multiplier (`option_contract_multiplier`) to the *parent's*
  `qty * limit_price` fallback for `place_option_order` specifically —
  `qty` there is a contract count and `limit_price` a per-share
  premium/net-debit, so one contract representing 100 shares was
  previously undercounting real premium by 100x. This is **not** a
  per-leg notional model: `notional_cap` reads only the parent's net
  debit/credit (the correct strategy-level premium figure, just
  previously missing the multiplier), and
  `position_cap`/`cvar_gate`/`pct_of_adv` still no-op on multi-leg calls
  entirely, since all three require the parent `symbol` field a multi-leg
  call never carries — extending them needs a decision this pass
  deliberately didn't make: whether a per-symbol cap or CVaR estimate for
  a multi-leg spread should apply to net premium, gross leg notional, or
  each leg's underlying individually. A new inconsistency this introduces:
  `option_tool_match` (default `["place_option_order"]`) means the same
  economic option order gets the 100x multiplier via `place_option_order`
  but not if later resized via `replace_order_by_id` (already unguarded
  by `symbol_allowlist`/`position_cap`/`cvar_gate`/`pct_of_adv` per this
  paragraph, and now also multiplier-less in `notional_cap`) — the same
  order is sized differently depending on which tool touched it last.
  `replace_order_by_id` and `order_class="bracket"/"oco"/"oto"` remain
  fully unaddressed, as does every other rule/tool combination in this
  paragraph. No corpus payload in `corpus/` exercises `place_option_order`
  (confirmed by grep), so this change does not affect any published eval
  result.

  **Update, 2026-08-28 — `order_class="bracket"/"oco"/"oto"` and
  every payload structurally containing `legs`, `take_profit`, or `stop_loss`
  no longer reaches `notional_cap`
  or any other rule described above at all.** A new upfront check
  (`_is_unsupported_order_shape` in `policy.py`) now hard-blocks those
  shapes outright, before `PolicyEngine.evaluate()`'s rule loop runs —
  see the "what this does not do" bullet below. This supersedes, rather
  than contradicts, this paragraph's "governed strictly at the parent
  notional level" framing for those specific shapes: that framing now
  describes only what's left ungated by the new check —
  `replace_order_by_id` (carries no `order_class`/`legs`/`take_profit`/
  `stop_loss`). `mleg` is also rejected regardless of whether `legs` is
  populated. Shape matching is case- and whitespace-insensitive and rejects
  these structural keys even when their values are empty or null.

- **Bracket, OCO, OTO, and multi-leg order shapes (or any order with `take_profit`/`stop_loss`) are hard-blocked:** bracket/OCO/multi-leg order shapes are not yet risk-assessed by this firewall and are blocked until support exists.


# Competition-safe execution modes

Read-only account verification (never constructs an order cycle):

```bash
python -m run_agent --preflight-only --expected-account-id YOUR_ACCOUNT_ID
```

Full policy evaluation against live paper data with all upstream mutations
suppressed inside the firewall proxy:

```bash
python -m run_agent --dry-run --budget 1000 --no-overlay
```

Omitting both mode flags is also dry-run. Direct paper submission requires
both the explicit execute flag and an exact account pin:

```bash
python -m run_agent --execute --expected-account-id YOUR_ACCOUNT_ID \
  --budget 1000 --no-overlay
```

Bounded autonomous dry-run loop (default), gated by Alpaca's market clock:

```bash
python run_loop.py --max-cycles 5 --interval-seconds 60 --budget 1000
```

Paper execution must be opted into and requires an exact expected account ID:

```bash
python run_loop.py --execute --expected-account-id YOUR_ACCOUNT_ID \
  --max-cycles 5 --interval-seconds 60 --budget 1000
```

Option proposal generation is omitted unless `--include-options` is supplied.
Even with that flag, every option-order submission is hard-blocked at the
policy boundary pending a schema and risk-control rebuild; the flag does not
enable option execution.
