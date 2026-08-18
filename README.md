# mcp-trade-firewall

An MCP proxy that enforces trading policy rules on tool calls before they
reach a trading MCP server.

## Layout

- `src/firewall/proxy.py` — MCP proxy server
- `src/firewall/policy.py` — rule engine
- `src/firewall/audit.py` — audit log writer
- `src/firewall/rules/` — individual rule implementations
- `policies/default.yaml` — versioned policy config
- `corpus/` — attack payloads
- `evals/` — eval harness
- `tests/` — test suite

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env  # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY
```

## What this does not do

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

- **Order modification (`replace_order_by_id`) and bracket/multi-leg orders
  (`order_class`/`legs` params) are not semantically inspected by any rule.**
  `replace_order_by_id` incidentally matches the generic `tool_match:
  ["order"]` pattern used by `notional_cap`, `position_cap`,
  `symbol_allowlist`, `cvar_gate`, `pct_of_adv`, `gtc_restriction`,
  `cooldown_after_loss`, and `drawdown_killswitch` — but Alpaca's replace
  endpoint is keyed by `order_id` alone (no `symbol` argument), so
  `symbol_allowlist` silently no-ops on it (`arguments.get("symbol")` is
  `None`), and `notional_cap`/`position_cap` will silently no-op too
  whenever a replace call omits either `qty` or `limit_price` (an
  order-amendment convention allowed by Alpaca that place-order calls
  don't use). In practice this means an order's size/notional can be
  amended upward after placement without being re-checked against any cap.
  We deliberately did not wire `replace_order_by_id` into `blast_radius`:
  that rule's mechanism (hard-block unless `state.human_approved`) exists
  for *bulk* actions (SEC Rule 15c3-5(b); FINRA Regulatory Notice 15-09),
  and a single order-price amendment is not a bulk action — see
  `corpus/benign.yaml`'s `benign-014`, which asserts routine single-order
  price adjustments must never be blocked. Gating `replace_order_by_id`
  under `blast_radius` would force human approval onto that legitimate
  case too. A correct fix needs a rule purpose-built for order amendment
  (re-deriving post-amendment notional/qty and re-checking it against the
  same caps as the original placement) plus a verified argument schema
  from the real `alpaca-mcp-server` `replace_order_by_id` tool, neither of
  which exists yet — guessing the schema here would repeat exactly the
  mistake the conformance audit's A4 finding identified (rules built
  against an assumed tool shape that was never checked against the real
  upstream). Bracket/multi-leg orders have the same problem one level
  down: `notional_cap`/`position_cap` read only the parent call's
  top-level `qty`/`limit_price`, never the `legs` array, so a multi-leg
  option order's individual legs (which can carry their own size, and for
  multi-leg option strategies their own contract) are not summed or
  separately checked. Both gaps are unmitigated as of this writing;
  treat any `replace_order_by_id` or `order_class="bracket"/"oco"/"oto"`
  call as **unguarded by policy** until one is built.
