"""AgentCore Platform v1.0"""

# SVC-C2-010 — DetectDeviationsNode
# Inner domain node 2: compare each current operations metric against its
# baseline and flag the deviations that exceed the declared threshold.
#
# The threshold comes from config/config.yaml (detection.deviation_threshold_pct)
# by way of the initial state, so lowering it in the file lowers it in the running
# agent. Only metrics that actually carry a baseline are compared; a metric the
# previous node left unbaselined is skipped rather than measured against a
# fabricated number.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import math
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Metrics where a POSITIVE deviation is anomalous (current > baseline).
_HIGH_IS_ANOMALOUS = frozenset(
    {
        "billed_hours",
        "utilization_rate",
        "sla_breach_count",
        "expense_ratio",
    }
)

# Metrics where a NEGATIVE deviation is anomalous (current < baseline).
_LOW_IS_ANOMALOUS = frozenset(
    {
        "revenue_jpy",
    }
)

# Built-in floor, used only when config/config.yaml declares nothing usable.
DEFAULT_THRESHOLD_PCT: float = 15.0


def _threshold(state: AgentState) -> float:
    """Read the declared deviation threshold the graph seeded into state."""
    settings = state.get("runtime_settings") or {}
    if isinstance(settings, dict):
        declared = settings.get("deviation_threshold_pct")
        if isinstance(declared, (int, float)) and not isinstance(declared, bool):
            candidate = float(declared)
            if math.isfinite(candidate) and candidate >= 0:
                return candidate
    return DEFAULT_THRESHOLD_PCT


def _compute_deviation_pct(current: float, baseline: float) -> Optional[float]:
    """Return the signed deviation percentage, or None when it is undefined.

    None covers a zero baseline (no ratio exists) and a deviation that is not a
    finite number. A non-finite deviation compares False against every threshold,
    so returning one would silently mark the metric as within parameters.

    One check, deliberately, on the RESULT. Guarding the operands as well would
    add nothing a caller can reach — a non-finite operand always produces a
    non-finite ratio — while making each of the two checks unfalsifiable by
    masking the other.
    """
    if baseline == 0.0:
        return None
    result = (current - baseline) / baseline * 100.0
    if not math.isfinite(result):
        return None
    return round(result, 2)


def _threshold_exceeded(metric: str, deviation_pct: float, threshold: float) -> bool:
    """Whether a deviation exceeds the anomaly threshold in the anomalous direction."""
    if metric in _HIGH_IS_ANOMALOUS:
        return deviation_pct > threshold
    if metric in _LOW_IS_ANOMALOUS:
        return deviation_pct < -threshold
    return abs(deviation_pct) > threshold


class DetectDeviationsNode(FunctionNode):
    """Compare current billing metrics against their baselines.

    Inner node — declared ANONYMOUS so the outer invocation context passes the
    subgraph boundary without rejection.

    Input state keys:
        operations_data:  dict          — the accepted payload
        baseline_data:    dict          — {metric: baseline}
        runtime_settings: dict          — declared deviation threshold

    Output state keys (partial dict):
        deviations: list[dict]  — one entry per compared metric
        status:     str
        error_log:  list[str]   (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        operations_data: Dict[str, Any] = state.get("operations_data") or {}
        baseline_data: Dict[str, float] = state.get("baseline_data") or {}

        if not operations_data:
            emit_trace_event("deviation_detection_failed", {"reason": "missing_operations_data"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DetectDeviationsNode: operations_data missing from state"],
            }

        metrics: Dict[str, Any] = operations_data.get("metrics", {})
        threshold = _threshold(state)
        deviations: List[Dict[str, Any]] = []

        for metric, baseline_val in baseline_data.items():
            current_raw = metrics.get(metric)
            if not isinstance(current_raw, (int, float)) or isinstance(current_raw, bool):
                continue
            dev_pct = _compute_deviation_pct(float(current_raw), float(baseline_val))
            if dev_pct is None:
                continue
            deviations.append(
                {
                    "metric": metric,
                    "current": float(current_raw),
                    "baseline": float(baseline_val),
                    "deviation_pct": dev_pct,
                    "threshold_exceeded": _threshold_exceeded(metric, dev_pct, threshold),
                }
            )

        # Threshold-exceeded first, then by absolute deviation descending.
        deviations.sort(key=lambda d: (not d["threshold_exceeded"], -abs(d["deviation_pct"])))

        flagged_count = sum(1 for d in deviations if d["threshold_exceeded"])
        engagement_id = operations_data.get("engagement_id", "unknown")
        max_dev = max((abs(d["deviation_pct"]) for d in deviations), default=0.0)

        logger.info(
            "DetectDeviationsNode: engagement=%s compared=%d flagged=%d threshold=%.1f%%",
            engagement_id,
            len(deviations),
            flagged_count,
            threshold,
        )
        emit_trace_event(
            "deviations_detected",
            {
                "engagement_id": engagement_id,
                "total_metrics": len(deviations),
                "flagged_count": flagged_count,
                "max_deviation_pct": max_dev,
                "threshold_pct": threshold,
            },
            state,
        )

        return {
            "deviations": deviations,
            "status": AgentStatus.SUCCESS.value,
        }
