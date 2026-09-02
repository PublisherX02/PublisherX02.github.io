# Adversarial Conformance Audit

Audit date: 2026-09-02. Scope: current working tree at `06b8a77` plus pre-existing uncommitted changes. No file under `src/` was changed. Findings apply to the dirty working tree, not solely to HEAD.

| Check | Verdict | Evidence | Defect description if not PASS |
|---|---|---|---|
| A1 | PASS | Real `fastmcp.Client.list_tools()` enumeration: **72 upstream tools** and **72 proxy tools**, identical name sets. All 72 names produced a policy decision (59 allow, 13 hard-block). `src/firewall/proxy.py:646,705`. | No tool bypassed evaluation. |
| A2 | PASS | Fake upstream added `totally_novel_admin_tool_xyz`; proxy listed it but returned `BLOCKED by rule 'unrecognized-tool-catchall'`; handler spy remained false. | Unknown tools are blocked, although catch-all coverage lists are manually duplicated and drift-prone. |
| A3 | PASS | Search for `call_next`, `create_proxy`, `Client(` found the sole upstream tool invocation at `src/firewall/proxy.py:705`, after evaluation (`:646`) and pending audit (`:663`). Resource/prompt hooks block at `:750-780`. | None. Direct Alpaca HTTP reads supply policy state and are not client MCP forwarding paths. |
| A4 | FAIL | Direct policy probes: bracket and OCO stock orders with harmless parent fields/extreme children allowed; price-less `mleg` option allowed without CVaR fetch. Replacement with incomplete sizing and bulk/all tools hard-blocked. See `notional_cap.py`, `option_spread_guard.py`, `cvar_gate.py`. | Bracket/OCO children and price-less multi-leg options escape meaningful notional, position, CVaR, and ADV checks. Rules infer semantics from names/top-level fields. |
| B1 | PASS | Grammar header (`evals/predicates.py:1-23`): `tool_call_expr := "tool_call(" tool_call_clause ...`; `tool_call_clause := "name~=" STRING | "forwarded=" ("true" | "false") | ...`. It states “`forwarded=` is mandatory”; omission raises at `:132-135`. | Attempted=`false`, forwarded=`true`; no implied default. |
| B2 | PASS | Strict `validate_corpus_file()` parsed 119 payloads across all seven YAML files. | Ambiguous payload IDs: **none**. |
| B3 | FAIL | Six scored files have a byte-identical four-line grammar block; `corpus/edge_cases.yaml` has a differently worded pointer and no identical block. | Requirement says all corpus files; `edge_cases.yaml` diverges even though excluded from scoring. |
| B4 | PASS | Constructed hard-blocked `SessionRecord(... forwarded=False)`: `forwarded=true` predicate false; `forwarded=false` true. B2 prevents omission. | No current payload can count a blocked attempt as forwarded success. |
| C1 | PASS | Fresh 100-record log: `(True,None)`; edit record 50 reason: `(False,51)`. In-memory verifier mutant disabling the link comparison returned `(True,None)` on the tampered log. `audit.py:163-196`. | None. |
| C2 | FAIL | Fresh probes: field edit `(False,51)`; deletion `(False,50)`; swap `(False,50)`; forged/re-chained suffix `(True,None)`. Last-record-only edit also `(True,None)`. | Detects (a)-(c), not (d) or final-record alteration. Expected limitation of an unsigned, unanchored chain. |
| C3 | FAIL | Proxy probe: hard block=1 record; allow=2 (`pending`,`ok`); handler error=2 (`pending`,`error`). `policy.py:337-428`, `proxy.py:663-724`. | Violates the requested “exactly one” invariant by design. Genuine transport timeout remained UNVERIFIABLE. |
| C4 | PASS | Handler read log inside its own body and saw a flushed pending record. Source: pending `proxy.py:663`, upstream `:705`. | Crash after decision leaves a pending record. |
| D1 | FAIL | 21 registered types, each with `tests/rules/test_<type>.py`; suite: `624 passed in 27.41s`. Types: notional_cap, position_cap, symbol_allowlist, blast_radius, drawdown_killswitch, order_rate_throttle, place_cancel_ratio, layering_detector, wash_trade_detector, cvar_gate, pct_of_adv, gtc_restriction, option_expiry_floor, option_spread_guard, net_delta_floor, hedge_cost_cap, option_sell_guard, hedge_regime_call_guard, cooldown_after_loss, hedge_proposal, unrecognized_tool_catchall. | `place_cancel_ratio` and `wash_trade_detector` lack meaningful negative boundary tests; green tests alone are not evidence. |
| D2 | FAIL | `test_sell_of_meaningfully_different_size_does_not_trigger` uses 10 vs 100, not near 1% tolerance. `test_single_cancelled_order_below_sample_size_does_not_trigger` tests only minimum sample size, not sufficient-sample/below-ratio behavior. | Negatives do not resemble the relevant abuse boundary. |
| D3 | UNVERIFIABLE | Prior report mutation-killed only a nine-rule sample via in-memory mutants. This pass did not mutate all 21. Current status retained the pre-audit dirty baseline; no source edit occurred. | Exhaustive deliberate-break requirement was not completed. Unmutated `624 passed` is not treated as proof. |
| D4 | PASS | Every configured rule has `regulation_ref`. References: notional/position/cooldown/drawdown—SEC 15c3-5(c)(1)(i); option-spread/net-delta/order-rate—(c)(1)(ii); symbol/catch-all—(c)(2)(ii)/(b); blast—15c3-5(b)+FINRA RN 15-09; layering—FINRA 5210, Exchange Act 9(a)(2), Rule 10b-5, FINRA 2020; wash—FINRA 5210.02. Nine others explicitly null. | No invented reference identified; unmapped rules are honestly null. |
| D5 | PASS | AST scan found only structural literals: cooldown_after_loss.py:93; cvar_gate.py:175; iv_hv_ratio.py:183,197; net_delta_floor.py:204; notional_cap.py:87; order_rate_throttle.py:62; pct_of_adv.py:91,101,109,110; place_cancel_ratio.py:70; position_cap.py:71. | No security threshold literal; gates use YAML-backed config. |
| E1 | PASS | Missing policy→`FileNotFoundError`; empty→Pydantic `ValidationError`; malformed→YAML `ParserError`; unknown type→`ValueError`. Missing-policy `main()` exited 1 before serve. | All fail closed. |
| E2 | PASS | Injected rule raising `ValueError("boom")` produced hard-block. In-memory mutant removing exception guard let it escape. `policy.py:191-319`. | Call does not proceed. |
| E3 | PASS | No MCP-exposed reset; drawdown latch clears only through direct Python `reset()`, not call arguments. | Out-of-band only. |
| E4 | PASS | Same for throttle pause. Live 25-call probe tripped call 21 at max 20; no client tool cleared it. | Out-of-band only. |
| E5 | PASS | `_require_paper_trade_mode()` rejected unset/empty/false/live/0/whitespace; accepted true/1/yes; guard failure exits 1. `proxy.py:826-838`. | None. |
| F1 | PASS | Full-history command: `git log --all -p -- . | Select-String -Pattern 'ALPACA_API_KEY...|ALPACA_SECRET_KEY...|AKIA...|sk-...|BEGIN.*PRIVATE KEY|PK...'` over 15 commits. Only a documented public example/test key ID matched. | Regex scanning cannot prove absence of arbitrary secrets, but found no owned credential/account ID. |
| F2 | PASS | Distinct fake keys plus mocked 401 and generic failures through account/bars fetchers; logged reasons contained neither value. | Third-party upstream subprocess logging is UNVERIFIABLE from this repo. |
| F3 | PASS | `git check-ignore -v .env audit.jsonl` cites `.gitignore:2` and `:15`; `git ls-files` found no `.env`, JSONL audit, or agent DB. | Local `.env`/`audit.jsonl` exist but are ignored and untracked. |
| G1 | FAIL | Threshold commit `ec2df93`: **2026-08-15T02:20:04+01:00**. First results `fa76044`: **2026-08-18T01:10:00+01:00**. Literal precedence holds, but reflog/history shows implementation/harness/results imported in ~4 minutes and later corpus/results in ~2.5 minutes. | Timestamp ordering does not substantiate methodological pre-registration; history is consistent with scripted retroactive import. |
| G2 | FAIL | Reachable tags include `corpus-v1.0`, `corpus-v1.1`; `corpus-v1.1` is an ancestor. `git diff corpus-v1.1 -- corpus/` is non-empty. | Current evaluated corpus is modified and untagged. |
| G3 | PASS | Two fresh `.venv312` runs with seed 1337, second under `PYTHONHASHSEED=42`: both `Gate: PASS`; parsed JSON identical after removing `generated_at` (`IDENTICAL_EXCEPT_GENERATED_AT=True`). Temp outputs removed. | Raw JSON differs only by timestamp. Use of process-salted `hash()` remains a latent reproducibility risk near future boundaries. |

## Totals

Counting each of A1–G3 once (28 checks total): **PASS 19 / FAIL 8 / UNVERIFIABLE 1**.

## Three defects most likely to invalidate published metrics

1. **A4: semantic order-shape bypasses.** Bracket/OCO children and price-less multi-leg options are absent from both meaningful risk sizing and the corpus, so published ASR/FPR cannot observe the bypass.
2. **G2: evaluated corpus is not tagged.** Fresh metrics come from uncommitted corpus content and cannot be reproduced from the advertised corpus tag alone.
3. **G1: weak pre-registration evidence.** Literal timestamp ordering exists, but the bulk-import chronology does not demonstrate thresholds fixed independently before system/corpus tuning.

## Unable to test

- Exhaustive D3 mutation testing for all 21 current rules.
- A genuine transport-level timeout/upstream process death for C3.
- The third-party upstream server's internal credential/error logging.
- Broker-side behavior with valid Alpaca paper credentials; none were used.

Overall, the repository is not conformant: it has a direct order-shape enforcement gap, violates the literal one-record audit invariant, lacks meaningful boundary negatives for two stateful detectors, and does not bind the currently evaluated corpus to a reachable tag.
