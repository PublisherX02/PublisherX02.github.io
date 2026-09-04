# Backstop — judging brief

**Alpaca paper-trading account ID:** `48a60c19-7e30-460a-8d96-1f84b44c40c5`

Backstop is an autonomous paper-trading system built around a deliberate
separation: strategy may propose an order, but an independent MCP firewall is
the only path that can submit it to Alpaca. The verified submission run used a
previously empty $100,000 paper account; its evidence report records 10 filled
orders, nine resulting positions, zero open orders after reconciliation, and
1,726 audit events.

## AI logic

The execution strategy is deterministic rather than language-model-directed.
It targets an eleven-symbol basket using inverse trailing realized volatility,
keeps a 10% cash buffer, and rebalances only after absolute weight drift exceeds
5%. This makes the strategy auditable and makes the model boundary explicit.

The project also uses Featherless-hosted `Qwen/Qwen2.5-7B-Instruct` for an
informational dashboard market brief based on portfolio fundamentals and
contract-exposure context. That process has no MCP tool access, cannot submit
orders, and never feeds a signal back into sizing, execution, or risk policy.

## Risk gates

Every proposed mutation is serialized through `firewall.proxy.build_proxy()`
and evaluated against the versioned `policies/default.yaml` policy. Core
controls include symbol allowlisting; per-order notional, position, liquidity,
and pending-order exposure caps; fresh broker position/open-order
reconciliation; market-data and CVaR gates; order-rate and duplicate-mutation
protection; and a session-loss killswitch at -$1,000. The killswitch blocks
risk-adding orders until reset, while its narrow deleveraging exception permits
only a plain-equity sell proven by a fresh authoritative broker snapshot not to
exceed the held quantity. Missing or untrustworthy risk state fails closed.

Allowed calls receive a pre-forward pending audit record and a linked outcome
record; hard blocks are recorded directly. The append-only JSONL audit log is
SHA-256 hash-chained and independently verified by the dashboard. The system
also distinguishes a firewall allowance from the eventual Alpaca outcome, so a
broker error is never misreported as a successful trade.

## Alpaca infrastructure

Backstop uses Alpaca's paper-trading environment through an MCP proxy, with
explicit paper-mode/account-identity checks before execution. Broker-sourced
account equity, session P&L, positions, open orders, market data, and lifecycle
status are used as policy evidence. A durable lifecycle journal and
post-submission reconciliation preserve the final broker result. Dry-run is the
default; live mutation requires explicit execution mode and the pinned paper
account. API credentials are intentionally supplied only through local
environment configuration and are never committed to the repository.
