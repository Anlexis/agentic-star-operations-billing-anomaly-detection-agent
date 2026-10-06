# Template Design Specification — SVC-C2-010

## Position in the architecture

- **Agent class**: `BillingAnomalyDetectionAgent`
- **Category**: Cat 2 — multi-step domain workflow
- **Industry**: SVC (professional services)
- **Three-layer separation**:
  - State: flat TypedDict (`src/schemas/state.py`) — no models, checkpoint-safe
  - Node: `FunctionNode.execute()` / `GraphNode` subclass
  - Graph: composition via `register_nodes()` / `add_edges()`

| Layer | Class |
|-------|-------|
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |

## Architecture overview

### Two-layer nested pattern

```
Outer backbone (BillingAnomalyDetectionAgent — AgentBaseGraph):
  START → initialize → pre_process → main → {route} → post_process → finalize → END
                                         ↓ (retry)
                                       pre_process

Inner graph (DomainWorkflowGraph — BaseGraph; inside the `main` slot):
  START → load_baseline → detect_deviations → classify_anomaly → generate_alert → END
```

### Node configuration

| Slot | Node class | Trust level | Responsibility |
|------|-----------|-------------|---------------|
| initialize | InitializeNode (framework default) | — | Sets schema version, session id, caller trust |
| pre_process | InputValidateNode | VERIFIED_EXTERNAL | The caller-data contract: screens, bounds and accepts the payload. A correctable value completes carrying `error_code`; hostile content terminates |
| main | AnomalyDetectionGraphNode (GraphNode) | (inherits outer) | Delegates to DomainWorkflowGraph |
| post_process | OutputFormatNode | ANONYMOUS | Renders the alert and enforces the output boundary; on a declined run renders that reason's fixed sentence instead |
| finalize | FinalizeNode (framework default) | — | Builds response metadata and timings |

### Inner domain workflow nodes

| Node | Trust level | Responsibility |
|------|-------------|---------------|
| LoadOperationsBaselineNode | ANONYMOUS | Establish each metric's baseline and record where it came from |
| DetectDeviationsNode | ANONYMOUS | Compute the signed deviation per metric and flag those past the threshold |
| ClassifyAnomalyNode | ANONYMOUS | Assign an anomaly type and a severity to each flagged deviation |
| GenerateManagementAlertNode | ANONYMOUS | Assemble the structured alert with findings and recommended actions |

### Outer → inner hand-off

`GraphNode.execute()` invokes the inner graph with the single string
`extract_input()` returns and forwards no other outer state field. The validated
payload and the runtime settings therefore travel over the ContextVar bridge in
`src/graph/context_bridge.py`: the outer node stashes them immediately before the
inner invoke, and `DomainWorkflowGraph._extra_initial_state()` seeds the inner
initial state from the stash. A ContextVar keeps the hand-off correct per thread
and per task, so concurrent requests in one process cannot see each other's data.

The payload also travels on that channel because the platform's input gate masks
personal-name-shaped text on `user_input` and `validated_input` in place. Text
carried there arrives with parts of it replaced by a mask token; a domain state
field is delivered verbatim, and this template screens that field itself.

### State definition

| Field | Type | Written by | Notes |
|-------|------|-----------|-------|
| validated_input | Optional[str] | InputValidateNode | Inert summary of what was accepted — no free text |
| caller_payload | Optional[dict] | InputValidateNode | The accepted, bounded payload the pipeline reads |
| enriched_context | Optional[dict] | InputValidateNode | Channel metadata |
| runtime_settings | Optional[dict] | Graph initial state | Validated values from `config/config.yaml` |
| operations_data | Optional[dict] | LoadOperationsBaselineNode | The payload, republished into the inner layer |
| baseline_data | Optional[dict[str, float]] | LoadOperationsBaselineNode | Baseline per metric |
| baseline_sources | Optional[dict[str, str]] | LoadOperationsBaselineNode | `caller_history` or `benchmark`, per metric |
| unbaselined_metrics | Optional[list[str]] | LoadOperationsBaselineNode | Metrics excluded from the analysis |
| deviations | Optional[list] | DetectDeviationsNode | Per-metric deviation records |
| anomaly_classifications | Optional[list] | ClassifyAnomalyNode | Typed, severity-tagged findings |
| anomaly_count | Optional[int] | ClassifyAnomalyNode | Total anomalies |
| alert_severity | Optional[str] | ClassifyAnomalyNode | Worst severity: critical / high / medium / normal |
| management_alert | Optional[dict] | GenerateManagementAlertNode | The structured alert |
| formatted_output | Optional[str] | OutputFormatNode | The rendered report, or the withheld notice |

**State constraints (mandatory):**
- Flat TypedDict only (primitives and JSON-serialisable types)
- No tokens, keys or credentials in state — checkpoints persist it
- Invocation context travels through `config["configurable"]`, not state
- No models, dataclasses or arbitrary objects — the checkpoint encoding cannot carry them

### Request payload

```json
{
  "engagement_id": "ENG-2026-001",
  "billing_period": "2026-Q2",
  "metrics": {
    "billed_hours": 5200,
    "utilization_rate": 0.94,
    "revenue_jpy": 52000000,
    "sla_breach_count": 4,
    "expense_ratio": 0.38
  },
  "baseline_metrics": { "billed_hours": 5100 },
  "team_size": 8
}
```

Sent as `input_context.operations_payload`, or as a JSON body on `input`.
Required: `engagement_id`, `billing_period`, and `metrics` carrying
`billed_hours`, `utilization_rate` and `revenue_jpy`.

### Rendered alert

```
[HIGH ALERT] — Engagement: ENG-2026-001
Period: 2026-Q2  |  Alert ID: ALERT-ENG-2026-001-A1B2C3D4

HIGH-severity alert for engagement ENG-2026-001 — 3 anomaly/anomalies detected.
Management review recommended within 24 hours.

Findings:
  1. [HIGH] utilization_spike — Team utilisation is 25.3% above the 75% target
     (current: 94%). Burnout risk or under-staffing signal. (baseline: benchmark)
  ...

Not analysed (no baseline available): custom_throughput

Recommended Actions:
  1. Assess team workload distribution; consider capacity reallocation.
  ...

Monetary figures are reported as aggregates rounded to the nearest 1,000;
individual line items are not reported.
```

## Anomaly detection logic

### Baseline resolution

A baseline must be independent of the value under test. A baseline derived by
scaling the current figure makes the deviation a constant of the scaling factor —
identical for every engagement and every number ever submitted — so the report
says the same thing whatever the data says. Resolution order per metric:

| Order | Source | Applies to |
|-------|--------|-----------|
| 1 | `caller_history` — the caller's own `baseline_metrics` entry | any metric |
| 2 | `benchmark` — `baselines.target_utilization_rate` | `utilization_rate` |
| 2 | `benchmark` — `baselines.expected_sla_breach_count` | `sla_breach_count` |
| 2 | `benchmark` — `baselines.expense_ratio_benchmark` | `expense_ratio` |
| 2 | `benchmark` — `team_size` × `baselines.standard_billable_hours_per_person` | `billed_hours` |
| 2 | `benchmark` — the hours baseline × `baselines.standard_rate_jpy_per_hour` | `revenue_jpy` |
| 3 | none — the metric is reported as unanalysed | anything else |

### Severity bands

Applied to the absolute deviation of a flagged metric; the values come from
`config/config.yaml`.

| Severity | Absolute deviation |
|----------|--------------------|
| critical | ≥ `detection.severity_critical_pct` (50%) |
| high | ≥ `detection.severity_high_pct` (30%) |
| medium | past `detection.deviation_threshold_pct` (15%) |
| normal | below the threshold |

### Anomaly taxonomy

| Anomaly type | Trigger metric | Anomalous direction |
|-------------|---------------|---------------------|
| billing_overrun | billed_hours | current > baseline |
| utilization_spike | utilization_rate | current > baseline |
| sla_breach_cluster | sla_breach_count | current > baseline |
| margin_compression | expense_ratio | current > baseline |
| revenue_underperform | revenue_jpy | current < baseline |
| metric_anomaly | any caller-defined metric | either direction |

## Boundaries

### The caller-data contract

`src/schemas/caller_contract.py` is the single definition both the HTTP adapter
and the validation node screen through, so the two cannot drift apart.

- **Inert.** Every caller string that can reach the rendered alert is bound to
  `[A-Za-z0-9_-]{1,32}` (metric names to `[a-z0-9_]{1,32}`). Free text there
  would be caller-controlled output injection: the alert is read by a human
  deciding whether to act on an engagement, and a newline plus a plausible
  heading is enough to forge a finding.
- **Finite.** Every caller number passes a finite-and-bounded check. `NaN` and
  `Infinity` survive a float conversion, and `NaN` compares False against every
  threshold — an unchecked one is not a crash but a silent "no anomalies found".
- **Screened.** The whole parsed payload is walked depth-first, keys included,
  for chat-template control markers (`<|…|>`, `[INST]`, `<<SYS>>`) and anchored
  directive phrases. Each string is screened twice: raw, which is the only pass
  that can still see a marker, and again with markup stripped, which is the only
  pass that can see a directive spliced apart by inserted tags. Scanning after
  the parse is what makes escaped spellings irrelevant.
- **Bounded structurally.** Metric-count cap, identifier-length cap, request-body
  cap, and a serialized size cap at the adapter.
- **Fail-closed, and quiet.** An unrecognised shape is refused, never coerced,
  and the refusal names the field rather than echoing the value. The field name
  and the bound stay in `error_log`, the internal audit channel.

Two outcomes, chosen by whether the caller can act on the finding and selected by
an explicit argument at the call site rather than by the wording of any message,
so the distinction survives a later rewrite:

- **A value the caller can correct** — nothing sent, a request body over its cap,
  a payload that is not an object or not valid JSON, a missing required field, a
  non-inert identifier, a non-finite or out-of-range number, a metric count over
  its cap — **completes** the run (`status = AgentStatus.SUCCESS.value`) carrying
  a reason code in `error_code`. The payload is still not accepted and nothing is
  published; only the reporting changes. The calling surface shows the fixed
  sentence for that code, so a corrected request can be sent on the same
  conversation instead of the caller receiving only an exception type.
- **Hostile content** — a chat-template control marker or an anchored directive
  phrase found by the screen — **terminates** (`status = AgentStatus.ERROR.value`).
  Spliced instructions are not a value the caller can correct by rewording, and
  completing the run would make a refusal read like an ordinary declined request.

The reason code is internal: it marks State so the `main` slot skips the inner
graph entirely — otherwise the workflow would reach its first domain node, fail
that node's own precondition and terminate, replacing the specific reason already
settled with a vaguer one — and it is never surfaced in the response envelope.
The caller reads the sentence, not the code.

The screens live in the node that owns the contract rather than being left to
the platform. A template that relies on the platform's input gate alone is
fail-open wherever that gate is absent or configured off, and the platform's own
detector scores the `<<SYS>>` frame as clean.

### Credential shapes on the structured channel

The framework's first node returns the structured request verbatim in its own
result, and the platform's output gate scans every value of every result — so a
credential-shaped string anywhere on that channel kills the run at node one with
an error the caller cannot act on. The adapter screens for it first, using the
framework's own detector so the refusal set matches the platform's block set
exactly, and answers 400 naming the field (never the value).

### The output boundary

A run the caller-contract gate declined reaches this node with no alert to
render, so it is answered before either layer runs: the node emits its own audit
event and returns the reason's fixed sentence as `formatted_output`, completing
and carrying the marker. Without that branch the "no alert in state" path would
report *Operations are within normal parameters* — a clean-baseline result for a
request that was never analysed at all.

Two layers run in `OutputFormatNode`, each with its own audit event, in this
order:

1. **Credential scan**, delegated to the framework's own detector. A local
   pattern set narrower than the framework's is a bypass rather than a defence.
2. **Precision grid**, snapping every monetary form onto the nearest 1,000.

The order matters: the grid reads any standalone three-letter uppercase word as a
currency marker, so running it first would rewrite the digits of a pattern the
credential scan is looking for.

The grid's grammar is group-based, with the comma-grouped form first in each
alternation, a delimiter that stays inside one line, and a decimal fraction
absorbed into each value alternative as `(?:\.\d+|(?!\.\d))` so the engine cannot
backtrack out of it. Identifier guards over this report's own render alphabet
(`[A-Za-z0-9_-]`) keep engagement labels, alert ids and metric names intact, and
one ISO 4217 check decides the single ambiguous case — whether
`<three letters>-<digits>` is a negative amount or an identifier. Only the
attached form is narrowed; a separated marker still snaps, so the ambiguous case
stays fail-safe.

On a violation the node does not raise. The platform's envelope is
`formatted_output or result` with no status check, so an error that leaves the
answer fields populated ships the un-gated answer inside the error envelope. The
gate returns an error status, blanks every output-bearing field by name — a
delta that omits a key leaves the old value in state — and sets a **truthy**
refusal notice, since a falsy one re-opens the fallback. The notice is a
constant: it reads nothing back out of the state that was just cleared.

Reachability, measured rather than assumed: on the framework version this
template ships against, a credential introduced upstream is refused by the
platform's own per-node scan before the report is rendered, so the caller
receives an empty error envelope rather than this notice. The layer is kept
because a detector gap is a containment bypass, and because the precision layer
on the same path is reached on every request.

### Audit events

Every node emits at least one domain event on a reachable path:

- `input_validated` / `input_validation_failed` (InputValidateNode)
- `baseline_loaded` / `baseline_load_failed` (LoadOperationsBaselineNode)
- `deviations_detected` / `deviation_detection_failed` (DetectDeviationsNode)
- `anomalies_classified` (ClassifyAnomalyNode)
- `management_alert_generated` (GenerateManagementAlertNode)
- `output_formatted` / `output_precision_enforced` / `output_withheld` (OutputFormatNode)

## Composition pattern

- **Pattern**: nested GraphNode (subgraph)
- **Composition target**: `DomainWorkflowGraph(BaseGraph)` via `AnomalyDetectionGraphNode.get_subgraph()`
- **Error propagation**: `propagate` — inner failures are re-raised, not absorbed

## Import isolation

- [x] Template does not import the platform-internal SDK
- [x] Import targets: `framework/` and `shared/` only
- [x] `langgraph.graph.{END,START}` imported only in the inner graph

## Design decision record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Fixed pipeline; no autonomous loop needed |
| Inner graph parent | BaseGraph | AgentBaseGraph | **BaseGraph** | Custom four-node linear topology; no forced backbone |
| Baseline source | Caller history, then declared benchmark | Fraction of the current value | **Caller history, then benchmark** | A baseline scaled from the value under test yields a constant deviation, not a detection |
| Unbaselined metrics | Named as unanalysed | Silently dropped | **Named** | Absence from the findings would otherwise read as a clean result |
| Anomaly classification | Rule-based | Model-based | **Rule-based** | Deterministic and auditable; the same figures always give the same alert |
| Caller channel | `input_context` (structured) | JSON body only | **Structured, body retained** | The body channel is rewritten in place by the platform's input masking |
| Error strategy | propagate | handle | **propagate** | Fail fast on inner errors; a partial anomaly report is worse than none |
