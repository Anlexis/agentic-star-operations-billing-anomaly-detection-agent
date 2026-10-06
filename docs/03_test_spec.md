# Test Specification — SVC-C2-010

## Strategy

Three layers, and the middle one is where most of the value sits:

- **Unit** — the caller contract, the domain arithmetic and the output boundary,
  each driven directly so a refusal can be attributed to the layer that made it.
- **Proof-of-boundary** — the structural invariants (state safety, import
  isolation, invoke order) plus the full request path through the real HTTP entry
  point. A pipeline that works node by node can still be unable to serve a single
  request; only the second kind of test sees that.
- **Behavioural assertions only.** Security tests assert what happened — refused,
  nothing carried forward — never the wording of any message, so a platform
  release that rephrases its own errors cannot turn a test green or red.
- **Two stop outcomes, asserted apart.** A value the caller can correct is
  *declined*: the run completes (`status=SUCCESS`) carrying a reason code in
  `error_code`, and both halves are asserted together, since the status alone
  would also pass on a run that quietly reported. Hostile content is *refused*
  and terminates (`status=ERROR`). Nothing is accepted and nothing is published
  on either path. Below, "declined" and "refused" are used in exactly that
  sense.

Run: `python -m pytest tests/ -v`

## Framework compliance tests

`tests/unit/test_framework_compliance_tc06_tc07.py`, `tests/unit/test_agent.py`,
`tests/proof_of_boundary/test_state_safety.py`

| TC-ID | Test | Expected result |
|-------|------|----------------|
| TC-01 | State contract: flat TypedDict | No models or dataclasses in `src/schemas/state.py` |
| TC-02 | Node execute signature | Every node takes `(self, state)` — the framework supplies nothing else |
| TC-03 | No credentials in state | Gate scan: 0 violations |
| TC-04 | Invocation context via `configurable` only | Not present in state |
| TC-05 | No duplicate lifecycle events inside `execute()` | Lifecycle events absent from node bodies |
| TC-06 | Default input gate not overridden | `TypeError` at class definition if overridden |
| TC-07 | Default output gate not overridden | `TypeError` at class definition if overridden |
| TC-08 | `required_trust_level` declared and enforced | Insufficient trust → refused |
| TC-11 | At least one domain audit event per node | ≥1 reachable emit per node |

## Proof-of-boundary tests

| PB-ID | Boundary | Test file | Expected result |
|-------|----------|-----------|----------------|
| PB-1 | Node → audit sink | `test_pb_invoke_order.py` | Events fire on every invocation path |
| PB-2 | State serialization | `test_state_safety.py` | Post-invoke state is primitives only |
| PB-4 | Import isolation | `test_import_isolation.py` | AST scan: 0 platform-internal imports |
| PB-5 | Checkpoint safety | `test_state_safety.py` | No credential-shaped fields in state |
| PB-6 | Invoke execution order | `test_pb_invoke_order.py` | Trust gate → start event → input gate → `execute()` → output gate → complete event |
| PB-7 | Cross-boundary interrupt propagation | `test_pb7_hitl_interrupt_propagation.py` | Not applicable — no graph class opts into propagation; ships as a skip stub |
| PB-8 | Deployed request path | `test_pb_e2e_invoke.py` | Real HTTP entry point serves a real report |

## Business logic tests

`tests/unit/test_agent.py`

| TC-ID | Test | Input | Expected result |
|-------|------|-------|----------------|
| BL-01 | A baseline is never a multiple of the value under test | `billed_hours` 100 and 90,000 | Same baseline both times |
| BL-02 | The declared benchmark is what is used | `standard_billable_hours_per_person: 100` | Hours baseline = 100 × team size |
| BL-03 | Caller history takes precedence over the benchmark | `baseline_metrics.billed_hours` supplied | Source recorded as `caller_history` |
| BL-04 | A metric with no baseline is named, not invented | An unknown metric key | Listed in `unanalysed_metrics`, absent from the findings |
| BL-05 | The deviation tracks the input | Two different `billed_hours` | Two different deviation percentages |
| BL-06 | At baseline, nothing is flagged | Metrics equal to their baselines | No finding exceeds the threshold |
| BL-07 | The declared threshold is what is applied | Threshold 90% vs 1% | Nothing flagged vs everything flagged |
| BL-08 | A non-finite deviation is dropped, not read as normal | A ratio that overflows | Metric excluded from the deviations |
| BL-09 | Severity bands, and the declared bands | 55% / 40% / 20%, then bands moved | critical / high / medium, then critical at 25% |
| BL-10 | Revenue underperformance is reachable | Revenue below baseline | `revenue_underperform` classified |
| BL-11 | Monetary figures render as aggregates | Revenue 52,123,456 | `52,123,000` rendered; the raw figure absent |
| BL-12 | Coverage is stated | Unanalysed metrics present | Named in the alert |

## Caller-contract tests

`tests/unit/test_caller_contract.py`

| TC-ID | Test | Expected result |
|-------|------|----------------|
| IN-01 | Non-finite matrix, per numeric field | `NaN`, `±Infinity`, strings, bools and out-of-range values declined — `status=SUCCESS` + `error_code` set, nothing published |
| IN-02 | `NaN` through the JSON body channel | Declined — the bare literal parses, so the check has to be ours |
| IN-03 | Identifiers bound to an inert alphabet | Whitespace, newlines, markup and over-length declined |
| IN-04 | Free-text engagement label declined | Output injection closed at the contract |
| IN-05 | A rejected value is never echoed | `error_log` names the field only; the caller-facing body is the fixed sentence for the reason code |
| IN-06 | Control markers and anchored directives caught | `<|im_start|>`, `[INST]`, `<<SYS>>` and directive phrases refused — terminating (`status=ERROR`), not declined |
| IN-07 | The marker class caught where the platform scores it clean | `<<SYS>>` refused although the platform's detector returns no finding |
| IN-08 | Hostile key names and escaped payloads caught | Post-parse, depth-first, keys included |
| IN-09 | Markup-spliced directives caught after the strip | `ig<b>nore …` refused |
| IN-10 | Ordinary professional-services language passes | Real sentences containing the same words are not refused |
| IN-11 | Credential shapes named by field | Every framework pattern refused; the value never echoed |
| IN-12 | Refusal set equals the platform block set | Property test over the framework's own detector |
| IN-13 | Structural bounds | Metric-count cap, baseline-count cap, body-size cap, missing required fields — each declined, carrying its reason code |

## Output-boundary tests

`tests/unit/test_output_gate.py`

| TC-ID | Test | Expected result |
|-------|------|----------------|
| OUT-01 | Every leak form snaps | Marker before/after, symbol or code, attached or separated, signed, tabs and single newlines |
| OUT-02 | No magnitude exemption | A small amount snaps too |
| OUT-03 | Grouped values snap whole | `JPY 1,234` → `JPY 1,000`, never `JPY 0,234` |
| OUT-04 | The delimiter stays inside one line | A section number after a three-letter code is byte-identical |
| OUT-05 | Decimals and percentages survive | Ratios, percentages and fractions untouched |
| OUT-06 | The fraction cannot be backtracked out of | A decimal followed by a letter is not re-matched as its integer part |
| OUT-07 | This report's identifiers survive | Engagement labels, alert ids, metric names, purely numeric suffixes |
| OUT-08 | The separated form still snaps | Only the attached `<letters>-<digits>` case is narrowed |
| OUT-09 | Layer order | A number group is still intact after the snap, so a pattern scan can see it |
| OUT-10 | A violation clears every output-bearing field | Presence AND emptiness asserted for each |
| OUT-11 | The replacement notice is truthy | A falsy one re-opens the framework fallback |
| OUT-12 | The notice carries nothing read back out of state | No identifier, no alert id, no figure |
| OUT-13 | The reason comes from a closed set | No traceback, no source path |
| OUT-14 | The cleared set covers every content-bearing state field | Inventory guard against the state schema; `error_code` is declared allowed-to-stay — a closed-set reason code, never caller content, and clearing it with the report would leave a notice with nothing behind it |
| OUT-15 | The framework's own detector is used | Every framework pattern withholds the report |
| OUT-16 | Clean-path control | The ordinary report is still produced |

## End-to-end tests

`tests/proof_of_boundary/test_pb_e2e_invoke.py` — the deployed HTTP entry point.

| TC-ID | Test | Expected result |
|-------|------|----------------|
| E2E-01 | Unauthenticated caller | 401, with a reason that does not say which check failed |
| E2E-02 | Authorised caller | A real report, gate node present in the node history |
| E2E-03 | Doubling a metric moves its deviation | 35.4% → 134.4% |
| E2E-04 | Data on the benchmark | Status normal |
| E2E-05 | Caller history wins over the benchmark | The benchmark finding disappears |
| E2E-06 | A metric with no baseline | Named as unanalysed in the report |
| E2E-07 | Lowering the declared threshold flags more | Normal at 900%, alerting at 1% |
| E2E-08 | The benchmark declaration reaches the inner graph | Raising the utilisation target removes the finding |
| E2E-09 | The contract bound reaches the validation node | A metric cap of 2 stops the request — declined, so the envelope is `status=success` with the fixed reason sentence as `output` and no report |
| E2E-10 | A non-finite declaration falls back to the floor | The built-in threshold is used |
| E2E-11 | The shipped configuration loads | `config/config.yaml` readable and validated |
| E2E-12 | Non-finite metrics declined through the body channel | `status=success` with the fixed reason sentence as `output`, no report, the rejected value never echoed |
| E2E-13 | Free text in an identifier declined | `status=success` with the fixed reason sentence as `output`, no report |
| E2E-14 | Control markers refused | Error status — the one input stop that still terminates |
| E2E-15 | A credential shape on the structured channel | 400 naming the field, value not echoed |
| E2E-16 | Ordinary domain text on the same channel | Served normally |
| E2E-17 | An oversized structured channel | 413 |
| E2E-18 | A credential on the data path reaches no caller | Error envelope carrying no released text |
| E2E-19 | No traceback or source path at the surface | Neither present in the envelope |
| E2E-20 | The precision layer fires end to end | An off-grid amount is delivered on the grid |
| E2E-21 | Monetary figures on the grid | The raw figure never reaches the caller |

## Test execution summary

- Total: 249 passed, 2 skipped (PB-7 — cross-boundary interrupt propagation not applicable)
- The suite runs against the framework wheel the pipeline installs.
