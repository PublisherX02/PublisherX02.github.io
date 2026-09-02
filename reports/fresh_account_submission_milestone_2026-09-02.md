# Fresh Alpaca paper-account submission milestone — 2026-09-02

## Verified outcome

- Pinned paper account: `48a60c19-7e30-460a-8d96-1f84b44c40c5`.
- Starting state: zero orders and zero positions.
- Strategy budget: $90,000, or 90% of the initial $100,000 NAV.
- Options overlay: disabled.
- Broker result: 10 filled orders, 9 resulting basket positions, zero open
  orders, and no phantom position or pending exposure.
- Audit result: 1,726 records; current chain valid. First, middle, and
  penultimate edits, a middle deletion, and a middle swap were all detected.
- Known audit boundary: without an external terminal-hash anchor, a last-record
  edit or correctly recomputed suffix remains undetectable.

Every blocked chunk failed closed rather than guessing, which is why the
account holds nine correct positions instead of eleven uncertain ones. This is
real Alpaca paper-account trading history, not live-money trading history.

## Fail-closed records preserved verbatim

```text
AAPL  8   pending-order-exposure  broker position/open-order exposure snapshot unavailable
MSFT 10   pending-order-exposure  broker position/open-order exposure snapshot unavailable
SPY   6   pending-order-exposure  broker position/open-order exposure snapshot unavailable
SPY   6   pending-order-exposure  broker position/open-order exposure snapshot unavailable
QQQ   7   pending-order-exposure  broker position/open-order exposure snapshot unavailable
QQQ   6   pending-order-exposure  broker position/open-order exposure snapshot unavailable
GD   13   pending-order-exposure  broker position/open-order exposure snapshot unavailable
GD    3   pending-order-exposure  broker position/open-order exposure snapshot unavailable
CACI  7   pending-order-exposure  broker position/open-order exposure snapshot unavailable
LDOS  8   pending-order-exposure  broker position/open-order exposure snapshot unavailable
NOC   7   pending-order-exposure  broker position/open-order exposure snapshot unavailable
BAH  67   pending-order-exposure  broker position/open-order exposure snapshot unavailable
J    33   pending-order-exposure  broker position/open-order exposure snapshot unavailable
```

These are thirteen manifestations of one condition: the exposure snapshot was
intermittently unavailable during a multi-chunk cycle. They are not thirteen
independent defects, and no blind retry was attempted.

## Evidence paths

- Dry-run: `data/cycles/20260902T172309369269Z.json`
- Execute cycle: `data/cycles/20260902T172637036914Z.json`
- Audit log: `audit.jsonl`
- Broker evidence: Alpaca paper-account order history and positions, correlated
  by the execute artifact's broker and client order IDs.

The evidence files above are preserved as generated; this report does not
replace or normalize them.

## Recorded, non-blocking follow-ups

1. Determine whether exposure-snapshot failures arise upstream at Alpaca or
   from the reconciliation query's cost/timing under multi-chunk load.
2. Add a distinct audit record for throttle/pacing waits. The cycle correctly
   paused for approximately 38 seconds, but the pause is only inferable from
   timestamps rather than represented as its own governance event.

## Final hygiene verification

- Full suite: `635 passed in 24.28s`.
- Exact credential-value scan across tracked and unignored files: zero hits for
  both Alpaca credential pairs and the Featherless API key.
- `.env`, `audit.jsonl`, heartbeat/current-cycle/lifecycle state, cached market
  brief, and generated cycle artifacts are explicitly gitignored.
- `git diff --check`: clean apart from Git's existing LF-to-CRLF notices.
