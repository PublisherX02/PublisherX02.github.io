# Competition audit checkpoint — 2026-09-01

This checkpoint records the last clean audit pass before execute-mode CLI and
exit-status hardening.

## Verification results

- Full test suite: **587 passed in 12.84 seconds**.
- Permanent AI architecture boundary: **3 passed**.
- Removed AI decision-component identifier scan across `src/`: **0 hits**.
- Pending-order exposure path tests: **5 passed**.
  - Raw unsafe 100-share order: blocked.
  - `run_agent` unsafe 100-share proposal: blocked.
  - Direct core-strategy path: reduced to the reconciled 20-share residual.
- Snapshot race test (position changing from 0 to 1 during reconciliation):
  failed closed before submission.
- Current audit chain: **1,298 records, valid**.
  - First, middle, and penultimate edits detected.
  - Middle deletion and reorder detected.
  - Last-record and correctly forged-suffix tampering remain known,
    accurately disclosed limitations of the unanchored hash chain.
- Deliberately wrong Alpaca expected account ID: rejected with
  `account ID mismatch`.
- Secret hygiene focused suite: **39 passed**.
- `data/runtime_heartbeat.json`: gitignored; credential-key scan returned
  zero hits.

## Real-state dry cycle

- Account: previously configured/old Alpaca paper account (not the fresh
  competition account).
- Execution mode: `dry_run`.
- Broker-reported equity: **$97,640.71**.
- Open orders: **0**.
- Aggregate outstanding notional: **$0.00**.
- Stock submissions: **0**.
- Option submissions: **0**.
- Position outcome: verified.
- Cycle artifact:
  `data/cycles/20260901T153738405030Z.json`.

## Scope and exclusions

This checkpoint intentionally excludes `.env`, credentials, `audit.jsonl`,
`data/runtime_heartbeat.json`, and mutable runtime-state files. The single dry
cycle artifact named above is retained as evidence.
