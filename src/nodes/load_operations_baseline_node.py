"""AgentCore Platform v1.0"""

# SVC-C2-010 — LoadOperationsBaselineNode
# Inner domain node 1: establish the baseline each billing metric is measured
# against, and publish the payload the rest of the pipeline reads.
#
# A baseline must be independent of the value under test. Deriving one by scaling
# the current figure — baseline = current x factor — produces a deviation that is
# a constant of the factor, identical for every engagement and every number the
# caller sends, so the pipeline reports the same finding whatever the data says.
# Baselines here therefore come from exactly two places:
#
#   caller_history  the caller's own historical figure for that metric
#   benchmark       a professional-services standard declared in config/config.yaml
#
# A metric with neither is reported as unbaselined and excluded from the analysis,
# rather than given a fabricated baseline.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Built-in floor for the benchmark values, used only where config/config.yaml
# declares nothing usable for a key.
_BUILTIN_BENCHMARKS: Dict[str, float] = {
    "target_utilization_rate": 0.75,
    "expected_sla_breach_count": 0.5,
    "expense_ratio_benchmark": 0.28,
    "standard_billable_hours_per_person": 480.0,
    "standard_rate_jpy_per_hour": 12000.0,
}

CALLER_HISTORY = "caller_history"
BENCHMARK = "benchmark"


def _benchmarks(state: AgentState) -> Dict[str, float]:
    """Read the declared benchmark values the graph seeded into state."""
    settings = state.get("runtime_settings") or {}
    if not isinstance(settings, dict):
        return dict(_BUILTIN_BENCHMARKS)
    resolved = dict(_BUILTIN_BENCHMARKS)
    for key in _BUILTIN_BENCHMARKS:
        value = settings.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            resolved[key] = float(value)
    return resolved


def derive_baselines(
    metrics: Dict[str, float],
    caller_baselines: Optional[Dict[str, float]],
    team_size: Optional[float],
    benchmarks: Dict[str, float],
) -> Tuple[Dict[str, float], Dict[str, str], List[str]]:
    """Return (baselines, per-metric source, metrics left unbaselined).

    Resolution order per metric: the caller's own history first, then the
    declared professional-services benchmark, then nothing.
    """
    history = caller_baselines or {}
    baselines: Dict[str, float] = {}
    sources: Dict[str, str] = {}

    hours_baseline: Optional[float] = None
    if team_size is not None and team_size > 0:
        hours_baseline = round(team_size * benchmarks["standard_billable_hours_per_person"], 4)

    for metric in metrics:
        supplied = history.get(metric)
        if isinstance(supplied, (int, float)) and not isinstance(supplied, bool):
            baselines[metric] = float(supplied)
            sources[metric] = CALLER_HISTORY
            continue

        derived: Optional[float] = None
        if metric == "utilization_rate":
            derived = benchmarks["target_utilization_rate"]
        elif metric == "sla_breach_count":
            derived = benchmarks["expected_sla_breach_count"]
        elif metric == "expense_ratio":
            derived = benchmarks["expense_ratio_benchmark"]
        elif metric == "billed_hours":
            derived = hours_baseline
        elif metric == "revenue_jpy" and hours_baseline is not None:
            derived = round(hours_baseline * benchmarks["standard_rate_jpy_per_hour"], 4)

        if derived is None:
            continue
        baselines[metric] = derived
        sources[metric] = BENCHMARK

    unbaselined = sorted(metric for metric in metrics if metric not in baselines)
    return baselines, sources, unbaselined


class LoadOperationsBaselineNode(FunctionNode):
    """Publish the operations payload and the baselines it is measured against.

    First node of DomainWorkflowGraph. Reads the validated payload the outer
    graph bridged into the inner initial state.

    Inner node — declared ANONYMOUS so the outer invocation context passes the
    subgraph boundary without rejection.

    Input state keys:
        caller_payload:   dict — validated payload from InputValidateNode
        runtime_settings: dict — declared benchmark values

    Output state keys (partial dict):
        operations_data:      dict
        baseline_data:        dict
        baseline_sources:     dict
        unbaselined_metrics:  list[str]
        status:               str
        error_log:            list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        payload: Any = state.get("caller_payload")
        if not isinstance(payload, dict) or not payload:
            emit_trace_event("baseline_load_failed", {"reason": "missing_payload"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["LoadOperationsBaselineNode: no validated payload reached the workflow"],
            }

        metrics = payload.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            emit_trace_event("baseline_load_failed", {"reason": "missing_metrics"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["LoadOperationsBaselineNode: the payload carries no metrics"],
            }

        team_size = payload.get("team_size")
        baselines, sources, unbaselined = derive_baselines(
            {str(k): float(v) for k, v in metrics.items()},
            payload.get("baseline_metrics"),
            float(team_size) if isinstance(team_size, (int, float)) and not isinstance(team_size, bool) else None,
            _benchmarks(state),
        )

        engagement_id = payload.get("engagement_id", "unknown")
        logger.info(
            "LoadOperationsBaselineNode: engagement=%s metrics=%d baselined=%d unbaselined=%d",
            engagement_id,
            len(metrics),
            len(baselines),
            len(unbaselined),
        )
        emit_trace_event(
            "baseline_loaded",
            {
                "engagement_id": engagement_id,
                "metric_count": len(metrics),
                "baseline_keys": sorted(baselines.keys()),
                "caller_history_count": sum(1 for s in sources.values() if s == CALLER_HISTORY),
                "unbaselined": unbaselined,
            },
            state,
        )

        return {
            "operations_data": payload,
            "baseline_data": baselines,
            "baseline_sources": sources,
            "unbaselined_metrics": unbaselined,
            "status": AgentStatus.SUCCESS.value,
        }
