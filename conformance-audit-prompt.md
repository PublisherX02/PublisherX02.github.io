# Adversarial Conformance Audit Prompt & Rubric

You are an adversarial security and conformance auditor evaluating the `mcp-trade-firewall` codebase.
Your goal is to independently grade the implementation against its specification and security claims across all categories below.
Do not accept passing tests or documentation assertions as evidence without verifying the underlying mechanisms.

---

## Audit Checklist & Evaluation Criteria

### A. Interposition Completeness
- **A1**: Verify upstream tool coverage. All tools exposed by the upstream MCP server must be intercepted and evaluated by the `PolicyEngine`. 0 tools must bypass the engine.
- **A2**: Verify default-deny / unrecognized tool handling. Any novel, unknown, or unrecognized tool not covered by a policy rule must fail closed and be hard-blocked by `unrecognized_tool_catchall`.
- **A3**: Verify single-point upstream dispatch. Ensure that tool execution flows exclusively through the middleware's `call_next` wrapper after policy evaluation, and non-tool endpoints (resources, prompts) expose no bypass surface.
- **A4**: Verify real-schema argument parsing and type coercion. Sizing and risk rules (`notional_cap`, `position_cap`, `cvar_gate`, `pct_of_adv`) must correctly parse and validate string-typed numeric fields (`qty`, `limit_price`, `notional`, etc.) matching Alpaca's real MCP schema rather than silently no-op'ing.

### B. Forwarded-vs-Attempted Distinction
- **B1**: Predicate grammar must support explicit scoping of tool calls to forwarded vs. attempted status (`forwarded=true` / `forwarded=false`).
- **B2**: Every attack and benign corpus payload success check must explicitly specify `forwarded=` on all tool call clauses.
- **B3**: Corpus headers must accurately describe the predicate grammar and evaluation semantics.
- **B4**: The predicate evaluator must strictly enforce `forwarded` filtering, ensuring hard-blocked attempts do not satisfy `forwarded=true` clauses.

### C. Audit Log Integrity
- **C1**: Verify tamper detection. The audit log must use a cryptographic hash chain (`prev_hash`), where in-place modification of any record is detected at the subsequent record via `verify_chain()`.
- **C2**: Verify resistance to record deletion and reordering, and document any structural limitations of unsigned local hash chains.
- **C3**: Verify crash-safe audit trail generation. Every intercepted call must produce complete, durable audit records:
  - **Hard-blocked calls** (`hard_block`): exactly 1 audit record written with `upstream_status="not_forwarded"`.
  - **Allowed / Soft-blocked calls** (`allow` / `soft_block`): exactly 2 audit records sharing the same `call_id` (`upstream_status="pending"` durably flushed before upstream forwarding, followed by an outcome record with `upstream_status="ok"` or `upstream_status="error"` upon completion or handler error).
  *(Note: Writing two records for forwarded calls is intentional crash-safety design to prevent lost audit trails on mid-call process failure; auditors must verify this 2-record pending+outcome lifecycle as conforming behavior, not a defect).*
- **C4**: Verify pre-dispatch flush. The `pending` audit record must be durably written to disk before `call_next()` invokes the upstream tool handler.

### D. Rule Correctness
- **D1**: Every registered rule type in `RULE_TYPES` must have positive (triggering) and negative (passing) tests.
- **D2**: Stateful detectors (`order_rate_throttle`, `layering_detector`, `wash_trade_detector`, `place_cancel_ratio`, `cooldown_after_loss`, `drawdown_killswitch`) must test realistic abuse patterns and sensitive threshold boundaries.
- **D3**: Mutation testing must confirm that rule comparison logic is load-bearing and non-tautological (inverting comparisons must fail corresponding tests).
- **D4**: Every rule in `default.yaml` must carry an honest `regulation_ref` (citing genuine SEC/FINRA regulations or explicitly specifying `null` with an UNMAPPED annotation).
- **D5**: Rule comparison thresholds must be sourced from policy configuration rather than hardcoded Python literals.

### E. Fail-Closed Behavior
- **E1**: Missing, empty, malformed policy files or unknown rule types must fail closed on startup, preventing the proxy from serving.
- **E2**: Exceptions raised during rule execution must fail closed to `hard_block`.
- **E3**: Policy engine state resets must not be callable by the LLM agent, and session PnL state must be populated from verified account data.
- **E4**: Order history and PnL state must be recorded and updated so rate throttles and manipulation detectors function in live execution.
- **E5**: Paper-trade mode guard must strictly validate environment configuration before connecting to upstream brokerage endpoints.

### F. Secret Hygiene
- **F1**: No API keys, secret tokens, or private credentials committed to git history.
- **F2**: Error messages, logs, and audit entries must redact or never include environment credentials.
- **F3**: Sensitive runtime files (`.env`, `*.jsonl` audit logs, local state databases) must be covered in `.gitignore`.

### G. Reproducibility
- **G1**: Evaluation harness thresholds and configurations must be pre-registered and versioned.
- **G2**: Attack corpora and policies must maintain consistent version identifiers.
- **G3**: Evaluation runs must use deterministic seeding for full repeatability.
