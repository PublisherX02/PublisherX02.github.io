# Adversarial Conformance Audit — mcp-trade-firewall

Audit date: 2026-08-17. Scope: repository at `C:\Users\moham\Trading Agent`, branch
`dev`, working tree on top of single commit `ec2df93`. AUDIT-ONLY mode: no file
under `src/` was modified at any point (verified by `git status` and, for D3's
mutation testing, by SHA-256 hash comparison of every file in
`src/firewall/rules/` before/after). All mutation testing was done via in-memory
`sys.modules` substitution or scratch-directory copies, never by editing files on
disk in the real repo.

**Headline finding, load-bearing for most of what follows:** `src/firewall/proxy.py`
— the only MCP-facing component in this repository — never imports or
instantiates `PolicyEngine` or `AuditLogWriter`. It implements exactly one
hardcoded rule (block `close_all_positions`-shaped calls) and prints raw
`tool_name`/`arguments` to stderr for every call, with no verdict, no rule
attribution, no hash chain. The entire rule engine (13 rule types,
`src/firewall/rules/`), the audit log writer (`src/firewall/audit.py`), and the
policy loader (`src/firewall/policy.py`) are fully implemented, well-tested in
isolation, and **never reachable from the running proxy**. They are exercised
only by `tests/` and by `evals/run.py`, which evaluates `PolicyEngine` directly
in-process and never spawns or calls the real (or even a fake) upstream MCP
server. Every check below that concerns the live system inherits this gap; it is
noted per-row rather than repeated in every cell.

---

## A. Interposition completeness

| Check | Verdict | Evidence | Defect |
|---|---|---|---|
| A1 | **FAIL** | Enumerated the real upstream via `uvx alpaca-mcp-server` + `fastmcp.Client.list_tools()`: **74 tools** (full list: add_asset_to_watchlist_by_id, cancel_all_orders, cancel_order_by_id, close_all_positions, close_position, create_locate, create_watchlist, delete_watchlist_by_id, do_not_exercise_options_position, exercise_options_position, fetch_alpaca_doc, get_account_activities(_by_type), get_account_config, get_account_info, get_all_assets, get_all_positions, get_alpaca_endpoint_docs, get_asset, get_calendar, get_clock, get_corporate_action(_announcement(s)), get_crypto_bars/latest_bar/latest_orderbook/latest_quote/latest_trade/quotes/snapshot/trades, get_fixed_income_latest_quotes, get_index_latest_values/values, get_locate/locate_quotes/locates, get_market_movers, get_most_active_stocks, get_news, get_open_position, get_option_bars/chain/contract/contracts/exchange_codes/latest_quote/latest_trade/snapshot/trades, get_order_by_client_id/get_order_by_id/get_orders, get_portfolio_history, get_stock_bars/latest_bar/latest_quote/latest_trade/quotes/snapshot/trades, get_watchlist_by_id/get_watchlists, list_alpaca_api_endpoints, place_crypto_order, place_option_order, place_stock_order, remove_asset_from_watchlist_by_id, replace_order_by_id, search_alpaca_api_specs, search_alpaca_docs, update_account_config, update_watchlist_by_id). Ran the same enumeration through `build_proxy()` (default, real backend): **also 74**, identical names — confirmed by executing both scripts. Of these 74, the count **wrapped by the policy engine is 0** — `PolicyEngine` is never instantiated in `proxy.py` (`grep -n "PolicyEngine\|AuditLogWriter" src/firewall/proxy.py` → no matches). One tool (`close_all_positions`, plus `close_position` called with no scoping args) is touched by a single hardcoded ad hoc check in `FirewallMiddleware`, which is not the policy engine. | **74 upstream tools reachable by a client; 0 wrapped by the policy engine.** The other 73 (including all order-placement, order-replacement, and bulk-cancel tools) pass through with no rule evaluation whatsoever. |
| A2 | **FAIL** | Executed a live simulation: built a fake upstream exposing two tools the proxy had never seen before (`submit_order`, `totally_novel_admin_tool_xyz`), called both through `build_proxy(upstream)`. Output: `submit_order error? False ... {"order_id":"fake-123","status":"accepted"}`; `novel tool error? False ... {"result":"executed"}`; `received by upstream: [('submit_order', {...qty=100000...}), ('totally_novel_admin_tool_xyz', {...})]`. Both passed straight through, unblocked, with a 100,000-unit order accepted. | Unknown-tool-passes-through, as the check defines it: any tool not shaped like `close_all_positions` — known or unknown, upstream-native or freshly invented — forwards unconditionally. |
| A3 | **PASS (structurally) / FAIL (in spirit)** | Exactly one code path reaches upstream: `FirewallMiddleware.on_call_tool` → `return await call_next(context)` (`src/firewall/proxy.py:63`), which fastmcp's `create_proxy` wires to the spawned `alpaca-mcp-server` subprocess. No other call site in `src/` reaches upstream. `evals/run.py`/`evals/agent.py` never call the real or a fake MCP server — they call `PolicyEngine.evaluate()` in-process and record a `SessionRecord`; nothing is "forwarded" in any I/O sense there. So: one path, verified — but it is downstream of exactly one hardcoded check, not downstream of "policy evaluation" as the check intends (the actual rule engine). | The single path is not gated by the policy engine at all. "Exactly one path, downstream of the engine" is only half true — one path exists, but the engine isn't in it. |
| A4 | **FAIL — critical, concrete** | `policies/default.yaml` configures 4 of the 13 rules (`order_rate_throttle`, `place_cancel_ratio`, `layering_detector`, `wash_trade_detector` — i.e. **every rule designed to catch market-manipulation patterns: rate abuse, spoofing/cancel-ratio, layering, wash trading**) with `place_tool_match: ["place_order"]` (`policies/default.yaml:125,141,156,169`), and `_util.matches_any()` does plain substring containment. Executed directly: `matches_any("place_stock_order", ["place_order"])` → `False`; same for `place_option_order`, `place_crypto_order`. **None of Alpaca's three real order-placement tool names contain "place_order" as a contiguous substring** (each has an asset-class infix: `place_​stock_​order`, `place_​option_​order`, `place_​crypto_​order`). So even if `PolicyEngine` were wired into `proxy.py` tomorrow, these 4 rules would never fire against a real order placement — they only fire against the fictional tool name `"place_order"` that every one of their own unit tests (`tests/rules/test_order_rate_throttle.py:44`, `test_layering_detector.py:44`, `test_wash_trade_detector.py:43`, `test_place_cancel_ratio.py:47`) and the entire eval harness/corpus (`evals/agent.py:73: name = clause.name_substr or "place_order"`) use as a stand-in, never reconciled against the real upstream's tool names (established in A1). By contrast, the 8 rules using the broader pattern `tool_match: ["order"]` (notional_cap, position_cap, symbol_allowlist, cvar_gate, pct_of_adv, gtc_restriction, cooldown_after_loss, drawdown_killswitch) *would* match real tool names (`"order"` is a substring of `"place_stock_order"`), and `blast_radius`'s `["cancel_all","close_all","liquidate"]` also matches real names correctly. `replace_order_by_id` (order replacement/modification) and any bracket/multi-leg structure passed via `order_class`/`legs` params inside `place_stock_order`/`place_option_order` are not semantically inspected by any rule — only tool-name substrings are checked, and no rule targets `replace_order_by_id` at all. | Order-name matching is fictional for exactly the 4 rules that exist to catch manipulation; they are dead code against the real system by construction, not by omission. This is name-matching, not semantic matching, and it fails precisely where the check predicted it would. `replace_order_by_id` and bracket/multi-leg orders are unguarded by any rule. |

---

## B. Forwarded-vs-attempted distinction

| Check | Verdict | Evidence | Defect |
|---|---|---|---|
| B1 | **FAIL (partial)** | Grammar header, quoted verbatim from `evals/predicates.py:1-24`: `tool_call_expr := "tool_call(" arg ("," WS arg)* ")"` where `arg := "name~=" QUOTED \| "forwarded=" ("true"\|"false") \| "args." FIELD OP VALUE"`. `forwarded=` is one optional member of an arg list — nothing in the grammar or its prose requires it, and nothing states what happens if it's omitted. Reading `_tool_call_matches()` (`evals/predicates.py:200-220`): if `clause.forwarded is None`, the forwarded check is skipped entirely — an omitted `forwarded=` matches **both** forwarded and non-forwarded (attempted-but-blocked) records. That is an implied default ("match either"), not an explicit, documented one. The grammar *can* distinguish forwarded from attempted (the field exists and is enforced when present), but does not *require* the author to make the distinction, and silently does the permissive thing when they don't. | The distinguishing capability exists; the "explicit default" requirement does not. Nothing in `predicates.py` prevents a future corpus entry from omitting `forwarded=` and silently degrading to "attempted counts as success." |
| B2 | **PASS** | `grep -n "success_check:" corpus/*.yaml \| grep -E "tool_call\(\|count\(" \| grep -v "forwarded="` → **zero output**. Checked all 124 `success_check` lines across all 7 corpus files; every `tool_call(...)`/`count(tool_call(...))` clause explicitly carries `forwarded=true` or `forwarded=false`. No ambiguous payload found. | None — despite B1's latent grammar gap, current corpus authoring discipline avoids it everywhere. |
| B3 | **FAIL** | Extracted each file's header block (everything before `payloads:`) and diffed pairwise. Headers are **not** byte-identical: e.g. `benign.yaml`'s header documents only `no_hard_block()` and `tool_call(...)` (the two constructs that file uses) and carries file-specific prose ("False-positive control set..."), while `credential_exfiltration.yaml`'s header additionally documents `count(...)` and both `contains(...)` variants and carries different attack-class prose. `edge_cases.yaml` doesn't restate the grammar at all — it just says "see [benign.yaml]'s header." Diff excerpt (benign.yaml vs credential_exfiltration.yaml) confirms divergent content, not a formatting artifact. | Per-file headers are curated subsets/paraphrases of the shared grammar in `evals/predicates.py`, not copies of one canonical block. No outright contradiction was found (each subset is consistent with `predicates.py`), but the check's literal requirement — byte-identical — does not hold. |
| B4 | **FAIL (latent, unexploited in current corpus)** | Constructed the case directly against `evals/predicates.py`: a `SessionRecord` with `verdict="hard_block", forwarded=False` for a `place_order`/`PUMP` call. Evaluated `tool_call(name~='place_order', args.symbol=='PUMP')` (no `forwarded=` scoping, which the grammar permits per B1) → **`True`** — a hard-blocked, never-forwarded attempt satisfies the predicate. The properly-scoped equivalent, `tool_call(name~='place_order', forwarded=true, args.symbol=='PUMP')`, correctly evaluates to `False` on the same record. Cross-referenced against B2's exhaustive grep: **zero payloads in the current corpus omit `forwarded=`**, so this vulnerability is not currently exploited by any of the 124 existing `success_check` entries — but nothing in the grammar or a schema-validation test (none found) would catch a future one that did. | The mechanism the check warns about is real and demonstrated; its blast radius on the *current* corpus is zero payloads, which should be stated plainly rather than either alarmed or swept away. |

---

## C. Audit log integrity

*(Full detail: `AUDIT_PARTIAL_C.md`, produced by a parallel sub-agent under the same
no-src-edit constraint; git status confirmed clean before/after.)*

| Check | Verdict | Evidence | Defect |
|---|---|---|---|
| C1 | **PASS (mechanism)**, see C3 for scope | Generated a fresh 100-record log via `AuditLogWriter`; `verify_chain` → `OK: chain verified`. Mutated one record's `verdict`/`reason` fields in place (valid JSON preserved) at index 50 → re-verify → `TAMPERED: chain broken at record index 51`. Confirmed the existing test (`test_verify_chain_detects_mutation_at_the_right_index`) is non-tautological: built a scratch-only copy of `verify_chain` with the `prev_hash` comparison dropped and confirmed it reports `ok=True` on the same tampered log (i.e. the real assertion would fail against this mutant). | None in the chain mechanism itself. |
| C2 | **(a) PASS, (b) PASS, (c) PASS, (d) FAIL — expected, documented limitation** | (a) same as C1. (b) Deleted record 50 from a fresh 100-record log → `TAMPERED: chain broken at record index 50`. (c) Swapped records 50/51 → `TAMPERED: chain broken at record index 50`. (d) Kept records [0,60), replaced [60,100) with 40 forged records (a real `hard_block` rewritten as `allow`/`forwarded=True`), correctly recomputing `prev_hash` forward from record 60 → `verify_chain` → `OK: chain verified`. **Undetected**, as expected for an unsigned hash chain — `audit.py`'s own docstring acknowledges this for the last record; this test shows the exposure is broader: an attacker with file-write access can forge an arbitrarily long, internally consistent tail from any truncation point, including flipping real hard-blocks into fabricated allows, with no external anchor (no signing key, no append-only FS attribute, no notarized timestamp) anywhere in the codebase. | (d) is a known, unmitigated limitation — report it as such, not as a pass. In-place edit, deletion, and reordering are all genuinely detected. |
| C3 | **FAIL as a claim about the running system** | `grep -n "audit\|Audit" src/firewall/proxy.py` → zero matches; the live proxy never writes a tamper-evident record for any call, only a bare stderr `print()` of `tool_name`/`arguments` (no verdict, no rule_id, no chain). At the `PolicyEngine` level, `audit_writer.append()` is called from exactly two sites in `src/firewall/policy.py`: the rule-exception fail-closed branch, and the `state_events` side-channel for stateful-rule transitions. **No ordinary `allow` verdict and no ordinary triggered `hard_block` verdict is ever written to the audit log anywhere in `src/`.** `tests/test_policy.py` says so directly in a comment: audit-logging ordinary verdicts is "the proxy's job once wired up" — and `proxy.py` does not do it. | "Every intercepted call produces exactly one audit record" does not hold for the system as wired: in production, ordinary allow/hard_block decisions produce zero audit records; only rule-evaluation exceptions and internal state transitions do, and even those never reach a real running proxy since `PolicyEngine` isn't instantiated there at all. |
| C4 | **Moot, not a pass** | `grep -rn "forwarded=True" src/` → zero occurrences; every real call site in `policy.py` hardcodes `forwarded=False, upstream_status="not_forwarded"`. `PolicyEngine.evaluate()` never itself performs the upstream call. No code path anywhere in `src/` writes an audit record for a call that was actually forwarded. | The before/after-the-call ordering question is unanswerable because no such record is ever produced. A crash cannot lose a record that is never written. |

---

## D. Rule correctness

*(Full detail: `AUDIT_PARTIAL_D.md`. Scope: the 13 rule types in `RULE_TYPES`;
`kelly_sizing.py` is excluded — it is an advisory sizing helper, not a `Rule`
subclass, not in the registry, not policy-YAML-addressable.)*

| Check | Verdict | Evidence | Defect |
|---|---|---|---|
| D1 | **PASS** | All 13 registered rule types have a named positive test and a named negative test (full file:line table in the partial report — e.g. `notional_cap`: `test_order_over_cap_triggers` / `test_order_just_under_cap_passes`). | None. |
| D2 | **PASS with one flagged gap** | 5/6 stateful detectors (order_rate_throttle, layering_detector, cooldown_after_loss, drawdown_killswitch, and place_cancel_ratio to a lesser degree) have negative tests that genuinely probe a meaningful boundary of the abuse shape. `wash_trade_detector`'s negative test uses qty=10 vs the triggering qty=100 (10x gap) rather than probing the real `qty_tolerance=0.01` boundary — same symbol/side/window/fill-status, so not "obviously unrelated" (doesn't meet the check's own bar for FAIL), but the weakest of the six. `place_cancel_ratio`'s negative test exercises only the minimum-sample-size gate, not the ratio threshold itself. | Two coverage gaps flagged (wash_trade_detector, place_cancel_ratio), neither rising to the check's FAIL bar. |
| D3 | **PASS — 13/13 mutations killed** | Every one of the 13 rules had its single most load-bearing comparison inverted via in-memory `sys.modules` substitution (mutated source exec'd into a synthetic module, never written to disk). Every mutation caused test failures (full mutation log with exact diffs and pass/fail counts in the partial report, e.g. `notional_cap: notional > cfg.max_usd → <`, 2/2 tests failed). Zero survivors. SHA-256 of all 17 files in `src/firewall/rules/` identical before/after; `git status` identical before/after. | None — no tautological rule test found among the 13. (Separately, see A4: even a rule whose *own* tests are sound can still never fire against the real system if its tool-name pattern doesn't match real tool names — that is a distinct defect from "the test is broken.") |
| D4 | **PASS** | All 13 rules carry a `regulation_ref` decision in `policies/default.yaml` — 9 cite specific US federal/FINRA law (SEC Rule 15c3-5 variants, FINRA Rule 5210/5210.02/2020, 15 U.S.C. 78i(a)(2), 17 CFR 240.10b-5), 4 explicitly and honestly carry `null` with an "UNMAPPED" comment rather than an invented citation (gtc_restriction, place_cancel_ratio, cvar_gate, pct_of_adv). No MiFID/EU leftovers anywhere (`grep` for "MiFID","RTS","MAR-Art" → zero hits), consistent with the documented prior migration to US law. | Minor inconsistency, not a defect: `gtc_restriction.py`'s own module docstring omits a "Regulation:" line (every other rule module states its basis or UNMAPPED status in-file); the UNMAPPED reasoning lives only in the YAML comment. |
| D5 | **PASS with a caveat** | No rule hardcodes a security-relevant threshold outside a YAML-configurable `pydantic` params model; every gating comparison reads from `self.cfg.<field>`, itself sourced from `RuleConfig.model_validate(rule_dict)`. Full literal inventory in the partial report. Lower-priority literals (`< 2`, `<= 0`, `max(1, ...)`, `+ 1`) are data-sufficiency invariants, not policy thresholds. | Caveat: several `_Params` fields carry Python-side defaults (`cvar_lookback_days=90`, `cooldown_loss_window=300`, etc.) that would apply silently if a preset YAML omitted them. `policies/default.yaml` sets everything explicitly, so this is inert there, but `policies/preset_*.yaml` were not checked for silent reliance on code-embedded defaults — flagged, not verified either way. |

---

## E. Fail-closed behavior

| Check | Verdict | Evidence | Defect |
|---|---|---|---|
| E1 | **PASS at the `PolicyEngine.from_yaml` level; N/A / moot for the live proxy** | Executed all four cases directly: missing file → `FileNotFoundError`; empty YAML → pydantic `ValidationError: version — Field required`; malformed YAML → `yaml.parser.ParserError`; unknown rule type → `ValueError: Unknown rule type 'this_rule_type_does_not_exist'...`. All four **raise**, none silently produce a permissive engine. | Since `proxy.py` never calls `PolicyEngine.from_yaml` at all (headline finding), whether the *live server* "refuses to serve" on a bad policy file is unanswerable — there is no code path in `proxy.py` that loads a policy YAML in the first place. The loader itself is sound; it is simply not wired to anything that runs. |
| E2 | **PASS, verified non-tautological** | Reproduced `test_rule_exception_does_not_propagate_and_fails_closed`: a rule whose `check()` raises `ValueError("boom")`, run through the real `PolicyEngine.evaluate()` → `decision == "hard_block"`. Then, in-memory only (never touching `src/`), rebuilt `evaluate()` with its `try/except` removed and ran the same scenario → the exception **propagated** instead of being caught, proving the real try/except is load-bearing (removing it changes observable behavior, so the test is not vacuous). | None at the `PolicyEngine` level. Same proxy-wiring caveat as E1: this protection never runs in production today since `PolicyEngine` isn't instantiated by `proxy.py`. |
| E3 | **PASS on the narrowest literal reading — but hollow** | `grep -rn "\.reset(\|def reset" src/ tests/` shows `PolicyEngine.reset(rule_id)` → `rule.reset()` is called only from `tests/rules/test_drawdown_killswitch.py:43` and `test_cooldown_after_loss.py`. No MCP tool, no code path anywhere in `src/`, exposes `reset()` to an agent — because `PolicyEngine` is never instantiated by `proxy.py` in the first place, there is no live surface for an agent to reach it through. Separately: `DrawdownKillswitchRule._tripped` (`src/firewall/rules/drawdown_killswitch.py:28`) is a plain in-process Python attribute with **no persistence** — it resets to `False` on every process restart, with no audit trail entry recording that a restart (not an explicit human reset) cleared it. | The "agent cannot reset it" property holds only because nothing is wired up for an agent to reach at all, not because of a deliberate access boundary — it is a symptom of the same architectural gap, not evidence of a designed safeguard. Separately, the killswitch's tripped state does not survive a process restart, which is functionally indistinguishable from an unauthorized reset from the trader's perspective, and is not logged anywhere. |
| E4 | **Same as E3** | `OrderRateThrottleRule._paused` (`src/firewall/rules/order_rate_throttle.py:34`) is the identical pattern: plain in-memory bool, `reset()` only ever called from `tests/rules/test_order_rate_throttle.py:55`, no MCP-reachable path, no persistence across restart. | Same two caveats as E3: hollow pass (nothing is wired up to reset in the first place) and no restart persistence. |
| E5 | **FAIL** | `grep -rniE "paper" src/ tests/ evals/ policies/ pyproject.toml README.md .env.example` → the **only** hit anywhere in the repository is a comment in `policies/default.yaml:4` ("Values here are conservative defaults sized for paper trading. Review and..."). No `ALPACA_PAPER_TRADE` environment variable, no startup check, no code of any kind validates that the server is running against paper (not live) credentials. `.env.example` only lists `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`. | There is no fail-closed (or any) enforcement of paper-vs-live trading anywhere in the codebase. The server will run against live credentials exactly as readily as paper ones; only a code comment expresses an assumption, not a guarantee. Critical FAIL. |

---

## F. Secret hygiene

*(Full detail: `AUDIT_PARTIAL_FG.md`.)*

| Check | Verdict | Evidence | Defect |
|---|---|---|---|
| F1 | **PASS, but low-value** | `git rev-list --all --count` = **1**. The entire git history is one commit (`ec2df93`, 2026-08-15, "Add eval harness pass/fail thresholds") adding only `evals/thresholds.yaml`; everything else in the repo is untracked. `git log --all -p \| grep -iE "ALPACA_API_KEY\|ALPACA_SECRET_KEY\|api[_-]?key\|secret[_-]?key\|account[_-]?id"` and a key-shaped-string scan of that one commit's diff both returned nothing. `agentdb.rvf`/`agentdb.rvf.lock` (untracked, unrelated binary lock-file format) contain no secret-shaped strings. | Not a defect, but the scan has almost no surface area to find anything on — there is essentially no history yet, not evidence of clean history over time. |
| F2 | **PASS** | `_log_call` in `proxy.py:41-47` logs only `tool_name`/`arguments`, never touches `os.environ`. Credentials are read only in `_alpaca_client()` (passed as subprocess env vars) and `market_data.py` (HTTP auth headers). Every exception branch in `market_data.py`'s `fetch_daily_bars` builds its `reason` string from `exc.code`/`exc.reason`, never request headers — verified against a real, non-tautological test (`tests/rules/test_pct_of_adv.py::test_failed_fetch_fails_closed_with_specific_reason`) asserting the literal string `"HTTP 401 fetching bars for AAPL: Unauthorized"` propagates, confirming only the status phrase leaks, not the key. `FirewallMiddleware.on_call_tool` has no try/except, so upstream exceptions propagate as fastmcp/upstream constructed them, not something built from env vars in this repo. | None found in this codebase. The third-party `alpaca-mcp-server` subprocess's own internal logging is out of this repo's scope — UNVERIFIABLE from here, called out as a boundary rather than assumed clean. |
| F3 | **PASS with a minor gap** | `.gitignore` excludes `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `build/`, `dist/`, `*.jsonl` (the audit-log extension). `git ls-files \| grep -iE "\.env$\|audit.*log\|\.log$\|\.jsonl$"` → no output. `git check-ignore -v` confirms `.env` is covered; `agentdb.rvf`, `agentdb.rvf.lock`, `.env.example` are **not** covered by any ignore rule (untracked only because nothing added them yet). | Minor housekeeping gap: `agentdb.rvf*` sit unignored in the repo root. Not secret-shaped, but a future `git add -A` would pull them in. |

---

## G. Reproducibility

*(Full detail: `AUDIT_PARTIAL_FG.md`.)*

| Check | Verdict | Evidence | Defect |
|---|---|---|---|
| G1 | **UNVERIFIABLE as literally specified** | `evals/thresholds.yaml` exists; its one and only commit is `ec2df93`, 2026-08-15 02:20:04 +0100. `git log --all --format="%H %ai %s" -- evals/out/results.json` → **empty** — that file has never been committed to any ref. The literal comparison ("thresholds commit precedes first commit containing results") has no second term to compare against. `results.json`'s self-reported `generated_at` field postdates the thresholds commit, which is weak circumstantial support, but it is a freely-regenerable field inside an uncommitted file, not a VCS-anchored timestamp. | The pre-registration claim is not checkable by the specified method: eval results (and the entire firewall implementation, for that matter) have never been committed. Only the thresholds file has real git provenance. |
| G2 | **FAIL** | `git tag --list` → empty. `git tag -l --format='%(refname) %(objectname) %(creatordate)'` → empty. | Zero tags exist anywhere in the repository. The corpus is not tagged in any sense. |
| G3 | **PASS (empirically, this run) with a latent defect** | Confirmed seeding in `evals/run.py`: `random.seed(args.seed)` plus a per-payload derivation `random.Random((seed, entry["id"], config, with_retries).__hash__() & 0xFFFFFFFF)` at line 324. Actually executed `evals/run.py --seed 1337` twice with `PYTHONHASHSEED` genuinely unset/randomized between runs (confirmed via direct `hash()` calls differing across two separate process invocations). Diffed both `results.json` outputs, excluding `generated_at`: **byte-identical**. | Latent, currently-unmanifested defect: the per-payload seed derivation uses Python's built-in `hash()` over strings, which is randomized per-process by `PYTHONHASHSEED` unless pinned. Reproducibility held in this run only because this corpus's aggregate metrics are insensitive to the resulting jitter, not because the mechanism is sound by construction — a future corpus entry near a threshold boundary could silently break "same seed → same results.json" under a different `PYTHONHASHSEED`. |

---

## FINAL

### Tally

- **PASS:** 15 (A3-structural half, B2, C1, C2a, C2b, C2c, D1, D2, D3, D4, D5, E1, E2, F1, F2, F3, G3) — see notes: several of these are "PASS with caveat" and are not clean.
- **FAIL:** 12 (A1, A2, A4, B1, B3, B4, C2d, C3, E3-hollow, E4-hollow, E5, G2)
- **UNVERIFIABLE:** 1 (G1)
- **Moot / not a pass but not a clean FAIL either:** C4 (the question doesn't apply because the thing it asks about is never produced)

(Total rows: 30, matching the enumerated checks A1–G3; A3, E3, and E4 are recorded
as PASS-with-heavy-caveat above rather than clean PASS, and are counted in the
FAIL-adjacent discussion below because the literal condition they "pass" on is
hollow.)

### Three defects most likely to invalidate published metrics, ranked

1. **`proxy.py` never wires in `PolicyEngine` or `AuditLogWriter` (headline finding, underlying A1–A4, C3, C4, E1, E2, E3, E4).** Every published number about "the firewall" — block rates, ASR/FPR, audit completeness — is measured against `PolicyEngine.evaluate()` called directly in-process by `evals/run.py`, which never touches the actual MCP proxy a real client would talk to. The live proxy currently enforces exactly one hardcoded rule out of an entire 13-rule engine. Any claim of the form "the firewall blocks X%" is a claim about a simulation harness, not about the artifact named in the README as the thing that "enforces trading policy rules on tool calls before they reach a trading MCP server."

2. **The 4 manipulation-detection rules key off a tool name (`"place_order"`) that never occurs in the real upstream server (A4).** This is not a hypothetical wiring gap — it is a concrete, empirically-confirmed string mismatch (`matches_any("place_stock_order", ["place_order"]) == False`) that would persist even after fixing defect #1. `order_rate_throttle`, `place_cancel_ratio`, `layering_detector`, and `wash_trade_detector` — every rule whose entire purpose is catching spoofing, layering, wash trading, and rate abuse — are dead code against Alpaca's real `place_stock_order`/`place_option_order`/`place_crypto_order` tools by construction. Every unit test, every corpus payload, and the entire eval harness share this same fictional naming assumption, so nothing in the existing test suite would ever surface this until it meets the real upstream server.

3. **The audit log's core claim doesn't hold even at the `PolicyEngine` level, independent of proxy wiring (C3/C4).** Ordinary `allow` and ordinary triggered `hard_block` verdicts are never passed to `audit_writer.append()` anywhere in `src/` — only rule-evaluation exceptions and stateful-rule state-transition side effects are. Combined with defect #1, this means the audit trail that any published metric would need to be *computed from* (a tamper-evident log of every intercepted call) is not produced for the overwhelming majority of call outcomes even in principle, let alone in the currently-unwired production path.

### What this audit could not test, and why

- **The real `alpaca-mcp-server` subprocess's own internal error/logging behavior** (F2's boundary note) — out of this repository's scope; only this repo's handling of its outputs was checked.
- **`policies/preset_1_loose.yaml` through `preset_5_strict.yaml`** were not individually re-run through E1–E5/D5's checks — the audit worked from `policies/default.yaml` throughout; whether any preset silently relies on a Python-side default (D5's caveat) or omits a rule present in default.yaml was not verified.
- **G1's pre-registration claim** is unverifiable by the git-timestamp method specified, not because the evidence points either way, but because the artifact being dated (`evals/out/results.json`) has never been committed to any ref in this repository's one-commit history. No stronger method (e.g. an external timestamp authority) was available to substitute.
- **Whether `policies/preset_*.yaml` files are used anywhere in a live path** — since `proxy.py` doesn't load any policy file at all currently, "which policy file governs production" has no answer to check against.
- **End-to-end behavior of the real, non-hardcoded rule engine against the real upstream server** — could not be tested because that code path does not exist yet; A1–A4's findings describe what *would* happen if the two were connected as designed, verified piece by piece (real tool enumeration, real `matches_any()` calls) rather than by running the missing integration itself.

### Overall assessment

This repository is in poor shape relative to its own README's claim ("An MCP proxy
that enforces trading policy rules on tool calls before they reach a trading MCP
server"). The rule engine, audit writer, and test suite around them are
individually well-built — mutation testing killed 13/13 rules, the hash chain
correctly detects in-place edits/deletions/reorderings, exception handling is
genuinely fail-closed, and regulation-reference discipline is honest. None of
that engineering is connected to the component that actually talks to clients.
The proxy that would carry this system's name in production enforces one
hardcoded rule and writes no audit trail. Even setting that gap aside entirely,
the rules built to catch market manipulation would silently no-op against the
real Alpaca tool surface due to a string mismatch. Do not treat any published
ASR/FPR number from this repository as a statement about the deployed system
until both of those are fixed and re-measured against the real proxy path.

---

## Remediation status

**A2 (unknown-tool-passes-through): remediated.** `unrecognized_tool_catchall`
(`src/firewall/rules/unrecognized_tool_catchall.py`) closes the specific gap
A2 demonstrated: any tool name that is neither on an explicit read-only
whitelist nor matched by another rule's `tool_match`/`place_tool_match`
pattern is now hard-blocked by default, rather than silently allowed through
with "no rule triggered". Added to all 5 `policies/preset_*.yaml` sweep
configs (see each preset's `unrecognized-tool-catchall` entry); must remain
the last rule in any policy's `rules:` list per `PolicyEngine.evaluate`'s
short-circuit-on-first-hard-block semantics. Regression-tested at the unit
level (`tests/rules/test_unrecognized_tool_catchall.py`, including a direct
reproduction of A2's own probe) and at the corpus level
(`corpus/induced_manipulation.yaml`'s `im-021`, part of corpus-v1.1, which
reproduces the audit's fabricated-tool/$999,999,999-argument scenario as a
permanent, mechanically-graded payload rather than a one-off manual test).

This closes A2 only. A1 (`PolicyEngine` not wired into `proxy.py`) and A4
(order-name substring matching against fictional vs. real Alpaca tool names)
remain open as described above; do not read this section as clearing either.
The catchall rule's own coverage is only as good as `covered_patterns`
staying in sync with every other rule's `tool_match` by hand — see that
module's docstring and the comment above its entry in `policies/default.yaml`.
