# System Walkthrough & Demo Guide

Run the full autonomous trading and risk-governance system end-to-end with a single command.

**Expect it to take several minutes.** Most of that is a deliberate ~90+ second pause between order chunks for rate-limit safety (`order_rate_throttle`) -- the run is not hung, it's pacing itself.

---

## 1. The Exact Command to Run It

Run from the project root:

```bash
python -m run_agent
```

*(Alternative shortcuts: `python run_agent.py`, `./run_demo.sh`, or `run_demo.bat`)*

This single command initializes the MCP proxy firewall, connects to the real Alpaca paper trading account, pulls live market data, calculates inverse-volatility risk-parity allocations, enforces pre-trade risk policies, submits orders, and verifies the tamper-evident audit trail.

---

## 2. What a Normal, Healthy Cycle Looks Like

During execution, the runner outputs a structured, human-readable 5-step report:

```text
┌──────────────── System Status: Online & Connected ────────────────┐
│ [*] MCP TRADE FIREWALL -- LIVE DEMONSTRATION RUN                  │
│ [+] Environment: Alpaca Paper Trading Account                     │
│ [+] Account Equity: $99,256.83 | Session P&L: -$720.71            │
│ [+] Target Allocation: 90% NAV in 11 Basket Equities, 10% Cash    │
│ [+] Active Limits: Position Cap 25% ($24,814) | Notional Cap 5%   │
└───────────────────────────────────────────────────────────────────┘

STEP 1: Market Data & Inverse-Volatility Sizing
  Fetches 90-day price history for 11 basket assets (AAPL, MSFT, SPY, QQQ, GD, CACI, ACN, LDOS, NOC, BAH, J).
  Sizes target weights inversely proportional to realized volatility (equal risk per asset).

STEP 2: Portfolio Drift & Rebalancing Decisions
  Compares current paper holdings against target allocations.
  Orders are only generated if an asset's position has drifted >5.0% from target.

STEP 3: Live Firewall Policy Enforcement & Order Execution
  Evaluates every proposed stock order against 18 of the firewall's 21 configured policy
  rules (the remaining 3 gate options orders or bulk-cancel calls only -- see policies/default.yaml):
  [Chunking Notice] SPY: Order of 90 shares ($69,018) exceeds 5% notional cap ($4,963).
                    Split into 15 chunks (6 shares each).
  -> Evaluating Order: SELL 6 SPY (chunk 1/15) @ ~$766.87 ($4,601.22)
    [OK] ALLOWED & EXECUTED -- Forwarded to Alpaca Paper Exchange
       Status: accepted (upstream returned no parseable order ID) |
       Audit correlation: 2026-08-31T20:49:53Z SELL 6 SPY

STEP 4: Scheduled Options Overlay (Standing Portfolio Insurance)
  Proposes a protective put option on the largest basket holding.

STEP 5: Resulting Basket State & Audit Verification
  Displays final holdings table, market value, and portfolio percentage.
  Verifies the SHA-256 tamper-evident cryptographic hash chain in audit.jsonl.

STEP 6: AI Market Commentary (Informational / Non-Trading)
  Generates a 3-5 sentence plain-English brief summarizing fundamental solvency ratios
  (SEC EDGAR Current Ratios) and contract exposures (USASpending prime contractor linkages)
  via Featherless AI (Qwen/Qwen2.5-7B-Instruct).
  Strictly informational -- zero tool access, zero feedback into trading logic.
```

---

## 3. What a Blocked Order Looks Like (and Why It's Working Correctly)

When a proposed order violates a risk limit, the firewall rejects it before it ever touches the market. The block you'll almost certainly see on any given run today is **`net-delta-floor`**, in STEP 4, rejecting the scheduled protective-put overlay:

```text
STEP 4: Scheduled Options Overlay (Standing Portfolio Insurance)
  [+] Proposed Overlay: BUY 1 PUT contract(s) on SPY (SPY260925P00731000)
      Strike: $731.00 | Expiry: 2026-09-25 | Rationale: standing portfolio insurance
    Option Order Filtered / Blocked: BLOCKED by rule 'net-delta-floor':
       proposed hedge exceeds neutral delta -- this is a directional short,
       not a hedge (net delta -35.32 < floor -10.00)
    (Standing insurance proposal recorded in audit log; options trading restrictions applied).
```

This is the **common case** -- it fires on effectively every run today (13 occurrences in this project's own audit history at time of writing), because the firewall can't currently see the equity shares underlying the option position (a disclosed gap -- see AUDIT.md), so it can't confirm the put is actually delta-neutral and blocks it rather than risk waving through a naked directional bet.

A rarer example, seen twice historically, is a stock order tripping the position cap:

```text
  -> Evaluating Order: BUY 10 AAPL @ ~$317.14 ($3,171.40)
    [X] BLOCKED BY FIREWALL RULE: 'position-cap-per-symbol'
       Reason: prospective exposure $26,956.90 exceeds cap $24,814.21 (25.0% of equity)
       Protection Note: This block is the policy engine working as designed to prevent risk violation.
```

### Why this is a feature, not a bug:
The firewall is a **security and governance layer**. A blocked order is proof that the system successfully stopped an autonomous agent from exceeding portfolio risk thresholds (e.g. over-concentrating in one stock, or -- as with `net-delta-floor` above -- placing an options order that isn't actually the hedge it claims to be). The block protects capital and enforces institutional risk boundaries.

---

## 4. How to Open the Live Dashboard Alongside It

To monitor orders, risk limits, and P&L in real time, open a second terminal window and run:

```bash
python dashboard/app.py
```

*(Or to serve as a web dashboard in your browser: `textual serve dashboard/app.py`)*

The terminal UI displays:
- Live audit stream of incoming tool calls and policy verdicts.
- Active risk controls (Killswitch, Rate Throttle, Cooldown, Hash Chain integrity).
- **AI Commentary Panel** ("*AI COMMENTARY (informational, generated by Featherless, not a trading input)*") displaying real-time fundamental & contract exposure briefs.

---

## 5. How to Independently Verify It's Real

You do not have to trust terminal text — verify the activity independently:

1. **Check the Local Cryptographic Audit Log**:
   ```bash
   tail -n 5 audit.jsonl
   ```
   Every evaluated order appends an immutable JSON record with timestamp, rule verdict, and SHA-256 hash `record_hash`.

2. **Check the Live Alpaca Paper Trading Portal**:
   - Log into [Alpaca Paper Dashboard](https://app.alpaca.markets/paper/dashboard/overview).
   - Go to **Orders** and confirm by matching **timestamp + symbol + side + qty** against the terminal
     output and `audit.jsonl` -- this system does not surface a broker order ID (Alpaca's real MCP
     response isn't guaranteed to include one), so there is no ID to match against.
   - Go to **Positions** to see the updated portfolio quantities matching the final basket table.
