# Backstop

**A policy-governed autonomous trading agent that fails closed.**

Backstop runs an inverse-volatility paper-trading strategy through an MCP
firewall that independently checks every order before it reaches Alpaca. The
strategy proposes trades; the firewall decides whether they are allowed.

- Live project: [publisherx02.github.io](https://publisherx02.github.io/)
- Verified paper account: `48a60c19-7e30-460a-8d96-1f84b44c40c5`
- Full control audit: [AUDIT.md](AUDIT.md)
- Fresh-account evidence: [submission milestone](reports/fresh_account_submission_milestone_2026-09-02.md)

## What Backstop delivers

Backstop combines a deliberately simple portfolio strategy with execution
controls designed for the failure modes that matter in an autonomous system:

- An 11-asset inverse-volatility basket with a 10% cash buffer.
- Drift-triggered rebalancing through the same governed execution path used by
  every other caller.
- Fresh broker position and open-order reconciliation before sizing and again
  at the serialized mutation boundary.
- Server-side position, notional, liquidity, account-identity, and market-data
  controls that callers cannot override.
- A cumulative session-loss killswitch at `-$1,000` that blocks risk-adding
  orders and stays latched until explicitly reset.
- A narrow deleveraging exception that permits only a plain-equity sell proven
  by a fresh authoritative snapshot not to exceed the held quantity.
- Tamper-evident, hash-chained audit records and a human-readable dashboard.
- Dry-run by default, explicit account pinning for execution, single-cycle
  locking, market-clock gating, and deterministic recovery controls.

## Verified paper-account result

Backstop was run against a pinned, previously empty **$100,000 Alpaca paper
account** with the options overlay disabled.

| Result | Verified outcome |
|---|---:|
| Filled orders | 10 |
| Resulting basket positions | 9 |
| Open orders after reconciliation | 0 |
| Fail-closed order blocks during the run | 13 |
| Audit records in the verified chain | 1,726 |
| Automated tests | 635 passing |

Alpaca's order and position records matched the execution artifact. The 13
blocked chunks were all denied when the broker exposure snapshot was
unavailable; Backstop did not guess and did not blindly retry. The resulting
positions were `AAPL`, `MSFT`, `SPY`, `GD`, `ACN`, `LDOS`, `NOC`, `BAH`, and
`J`.

This is real **paper-account** trading history, not live-money trading history.
Generated runtime artifacts and credentials are intentionally excluded from
Git; the evidence report records the outcome and provenance without publishing
secrets or mutable account state.

## How it works

```text
Inverse-volatility strategy
          |
          v
   order proposal
          |
          v
Serialized MCP mutation gateway
          |
          +--> refresh account, positions, open orders, and market data
          +--> resolve authoritative quantity and notional
          +--> evaluate versioned policy rules
          +--> append hash-chained audit decision
          |
     allow | block
          v
       Alpaca paper account
          |
          v
  lifecycle reconciliation + cycle artifact
```

The portfolio logic lives outside the firewall and has no special execution
privileges. Every proposed order crosses the same `firewall.proxy.build_proxy()`
boundary and is evaluated against `policies/default.yaml`.

### Strategy

The fixed basket contains `AAPL`, `MSFT`, `SPY`, `QQQ`, `GD`, `CACI`, `ACN`,
`LDOS`, `NOC`, `BAH`, and `J`. The last seven are federal IT and defense
contractors with manually verified high-dollar subaward links to Microsoft on
USASpending.gov. That relationship was validated for this specific basket; it
is not a general-purpose discovery engine.

Weights are inverse to trailing realized volatility:

```text
w_i = (1 / sigma_i) / sum(1 / sigma_j)
```

This allocates risk, not conviction, and makes no claim of predictive edge.
Rebalancing occurs only when absolute weight drift exceeds 5%.

Drift rebalancing is symmetric: a falling asset that becomes underweight can
generate a buy back toward target. It is therefore **not** crash de-risking.
The only automatic crash de-risking path is a strategy-generated sell passing
through the killswitch's deleveraging exception after the session-loss breach.
That exception permits a qualifying sell; it does not create or size one.

### Policy firewall

The default policy composes independent rules for:

- symbol allowlisting and unsupported order-shape rejection;
- per-order notional and percentage-of-volume limits;
- dynamic per-symbol position caps based on live equity;
- pending-order exposure and authoritative quantity resolution;
- CVaR, stale-market-data, and session drawdown gates;
- order-rate throttling and duplicate/concurrent mutation protection;
- option expiry, spread, delta, premium-cost, and sell-side constraints; and
- audit logging for allowed, blocked, proposed, and lifecycle outcomes.

Unknown order-like tools and unsupported bracket, OCO, OTO, multi-leg, or
structurally nested order payloads fail closed before downstream evaluation.

## Quick start

Requirements: Python 3.10+ and Alpaca paper-trading credentials.

```bash
pip install -e ".[dev]"
cp .env.example .env
```

Add the paper credentials and expected account ID to `.env`. Never commit it.

```bash
# Full test suite
pytest -q

# Read-only account verification
python -m run_agent --preflight-only --expected-account-id YOUR_ACCOUNT_ID

# Full policy evaluation with upstream mutations suppressed
python -m run_agent --dry-run --budget 1000 --no-overlay

# Explicit, account-pinned paper execution
python -m run_agent --execute \
  --expected-account-id YOUR_ACCOUNT_ID \
  --budget 1000 \
  --no-overlay

# Bounded autonomous dry-run loop
python run_loop.py --max-cycles 5 --interval-seconds 60 --budget 1000
```

## Dashboard and evidence

The dashboard renders policy outcomes, order lifecycle state, exposure
reconciliation, and the audit chain in a reviewer-friendly interface.

```bash
python dashboard/app.py
```

| Path | Purpose |
|---|---|
| `src/firewall/proxy.py` | Serialized MCP gateway and broker mutation path |
| `src/firewall/policy.py` | Policy engine and structural fail-closed checks |
| `src/firewall/rules/` | Individual risk controls |
| `src/core_strategy.py` | Inverse-volatility portfolio proposal logic |
| `src/run_agent.py` | Governed cycle orchestration and reconciliation |
| `src/autonomous_loop.py` | Market-clock-gated scheduler |
| `dashboard/app.py` | Human-readable operational dashboard |
| `corpus/` and `evals/` | Adversarial payloads and evaluation harness |
| `tests/` | Regression and adversarial test suite |
| `AUDIT.md` | Detailed control coverage and audit findings |

## Honest boundaries

- **Options do not execute.** Protective-put logic can produce an audited
  proposal, but option submission is hard-blocked pending a schema and
  risk-control rebuild. There is no approval-token infrastructure.
- **The hedge proposal is not an autonomous hedge.** It detects and specifies a
  candidate; it never submits it.
- **`cooldown_after_loss` is intentionally inert.** Alpaca does not expose the
  rolling realized-only P&L event stream the rule requires, and Backstop does
  not silently substitute mark-to-market P&L.
- **The autonomous scheduler defaults to unlimited cycles.** Operational runs
  must supply `--max-cycles` explicitly and be checked periodically.
- **Exposure snapshots can fail intermittently under load.** Each affected
  order fails closed, but a clean cycle-start snapshot does not guarantee every
  later per-order refresh will succeed.
- **The audit chain has no external terminal-hash anchor.** It detects internal
  corruption, but a correctly recomputed suffix is outside its trust boundary.
- **Historical simulation is not a forecast.** CVaR and inverse-volatility
  weights depend on available historical data and can be noisy on short
  windows.

These are design facts, not footnotes. Detailed rationale, the control matrix,
and remediation history remain in [AUDIT.md](AUDIT.md).

## Repository hygiene

- Runtime credentials, audit logs, heartbeats, lifecycle files, cached data,
  and generated cycle artifacts are gitignored.
- The committed submission state passed 635 tests and credential-value scans.
- The submission branch is `main`; changes should arrive through reviewed pull
  requests.

## Responsibility

Backstop is a competition project for governed paper trading. It is not
investment advice or production-ready live-money trading software.
