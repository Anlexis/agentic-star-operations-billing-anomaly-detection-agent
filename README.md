# Operations & Billing Anomaly Detection Agent

AI agent for detecting operations and billing anomalies in professional services, built with Agentic Star.

> **Category**: Cat 2 (a domain pipeline: an anomaly-detection workflow behind the fixed agent backbone)
> **Industry**: Services
> **Template ID**: SVC-C2-010

## Overview

Reviews one professional-services engagement's billing and operations figures for a period and
returns a management alert: which metrics deviated, by how much, how serious that is, and what a
delivery lead should do about it. It covers billing overruns, utilisation spikes, SLA-breach
clusters, margin compression and revenue underperformance, and it detects and reports only — no
node in the pipeline has a side effect.

The baselines are the point. Each metric is measured either against the engagement's own historical
figure, when the caller supplies one, or against a professional-services benchmark declared in
`config/config.yaml` — never against a fraction of the value being tested. A baseline scaled from
the figure under review makes the deviation a constant of the scaling factor: the same percentage
for every engagement and every number ever submitted, which reports a finding without detecting
anything. Every finding therefore names the source its baseline came from, and a metric with no
baseline available is listed as unanalysed rather than quietly omitted, so a reader can tell "no
anomaly" from "not checked".

The operations payload travels on a structured request channel rather than the plain input field.
That field is rewritten in place by the platform's input screen, which masks personal-name-shaped
text — so an engagement label sent there can arrive altered, and the report would then describe an
identifier the caller never sent. The structured channel is delivered verbatim and this template
screens it itself: every caller string that renders into the alert is bound to an inert identifier
alphabet, every caller number passes a finite-and-bounded check, and the whole payload is walked —
keys included — for chat-template control markers and directive phrases.

Detection is deterministic and offline. There is no model call and no external service, so the same
figures always produce the same alert and every finding traces back to a metric, a baseline and the
source of that baseline.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Request contract

`POST /invoke` carries the operations payload on `input_context.operations_payload`:

```json
{
  "input": "billing anomaly review",
  "session_id": "ops-2026-q2-001",
  "input_context": {
    "operations_payload": {
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
  }
}
```

- `engagement_id` and `billing_period` are required, and bound to 1–32 characters of
  `A-Z a-z 0-9 _ -` because they render into the alert.
- `metrics` is required and must carry `billed_hours`, `utilization_rate` and `revenue_jpy`.
  Every value must be a finite number inside its range.
- `baseline_metrics` is optional and takes precedence over the benchmark.
- `team_size` is optional, and is what makes an hours baseline — and hence a revenue baseline —
  available.

The same payload may be sent as a JSON body on `input`, subject to the platform masking described
above.

The caller must run at verified-external trust. Behind a gateway that establishes it, nothing
further is needed; standalone, set `INVOKE_AUTH_TOKEN` in the server environment and send it as
`Authorization: Bearer <token>`.

Monetary figures in the alert are aggregates rounded to the nearest 1,000 currency units;
individual line items are never reported. The rule is enforced at the output boundary rather than
assumed of the renderer.

## Configuration

| File | Role |
|---|---|
| `config/agent.yaml` | Static registration manifest — identity only, read at root level by the registry. |
| `config/config.yaml` | Every runtime parameter: `max_retry`, the detection thresholds and severity bands (`detection.*`), the benchmark baselines (`baselines.*`) and the caller-contract bounds (`caller_contract.*`). Loaded by the registry and by `src/api/server.py`, so both deployments run on the same declaration. |

Each declared value is validated once when the graph is built and then travels to the node that
consumes it, so changing a number changes the running agent. A key that is absent, of the wrong
type, non-finite or out of range falls back to the pipeline's built-in floor.

## Project Structure

```
src/          agent implementation (graphs, nodes, schemas, HTTP entry point)
tests/        unit, caller-contract, output-boundary and end-to-end tests
config/       agent manifest and runtime parameters
docs/         design and test documentation
```

See `docs/02_design.md` for the architecture, the caller-data contract and the output boundary,
and `docs/03_test_spec.md` for what the suite covers.

## Customising

1. Adjust `config/config.yaml` for your own thresholds, severity bands and benchmark baselines.
2. Extend the anomaly taxonomy and the finding sentences in `src/nodes/classify_anomaly_node.py`
   with metrics your organisation tracks.
3. Extend the recommended-action playbook in `src/nodes/generate_management_alert_node.py`.
4. Replace the benchmark fallbacks in `src/nodes/load_operations_baseline_node.py` with a call to
   your own historical store, keeping the rule that a baseline never derives from the value under
   test.
5. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
