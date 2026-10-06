"""AgentCore Platform v1.0"""

# SVC-C2-010 — ClassifyAnomalyNode
# Inner domain node 3: turn each flagged deviation into a professional-services
# anomaly type with a severity.
#
# Anomaly types (rule-based taxonomy):
#   billing_overrun       — billed hours significantly above baseline
#   utilization_spike     — utilisation abnormally high
#   sla_breach_cluster    — SLA breach count significantly above expected
#   margin_compression    — expense ratio higher than benchmark
#   revenue_underperform  — revenue below expected for the billed effort
#   metric_anomaly        — catch-all for a caller-defined metric that deviates
#
# Severity bands are read from config/config.yaml (detection.severity_high_pct,
# detection.severity_critical_pct) and applied to the absolute deviation. The
# worst individual severity drives the alert severity.
#
# Monetary figures are rounded to the nearest 1,000 currency units where they are
# built, so the aggregate rule the report states holds by construction and the
# output gate has nothing to correct on the normal path.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import math
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Grid every monetary figure in the report sits on.
MONETARY_ROUND_UNIT = 1000

# Built-in floor for the severity bands, used only where config/config.yaml
# declares nothing usable.
DEFAULT_HIGH_PCT: float = 30.0
DEFAULT_CRITICAL_PCT: float = 50.0

# Anomaly type per metric, with the sentence used to describe a finding.
_ANOMALY_MAP: Dict[str, Tuple[str, str]] = {
    "billed_hours": (
        "billing_overrun",
        "Billed hours are {dev:.1f}% above the {base:.0f}-hour baseline "
        "(current: {cur:.0f} hours). Potential scope creep or unauthorised time.",
    ),
    "utilization_rate": (
        "utilization_spike",
        "Team utilisation is {dev:.1f}% above the {base:.0%} target "
        "(current: {cur:.0%}). Burnout risk or under-staffing signal.",
    ),
    "sla_breach_count": (
        "sla_breach_cluster",
        "SLA breach count is {dev:.1f}% above baseline (current: {cur:.0f}, "
        "expected ~{base:.1f}). Delivery quality degradation detected.",
    ),
    "expense_ratio": (
        "margin_compression",
        "Expense ratio is {dev:.1f}% above the {base:.0%} benchmark "
        "(current: {cur:.0%}). Margin is being compressed.",
    ),
    "revenue_jpy": (
        "revenue_underperform",
        "Revenue is {dev:.1f}% below baseline (current: JPY {cur}, "
        "expected JPY {base}). Investigate pricing or write-off.",
    ),
}

# Metrics whose values are amounts of money, and therefore reported as
# aggregates on the grid rather than to the yen.
_MONETARY_METRICS = frozenset({"revenue_jpy"})


def _severity_bands(state: AgentState) -> Tuple[float, float]:
    """Read the declared severity bands the graph seeded into state."""
    settings = state.get("runtime_settings") or {}
    high, critical = DEFAULT_HIGH_PCT, DEFAULT_CRITICAL_PCT
    if isinstance(settings, dict):
        for key, current in (("severity_high_pct", "high"), ("severity_critical_pct", "critical")):
            declared = settings.get(key)
            if isinstance(declared, (int, float)) and not isinstance(declared, bool):
                candidate = float(declared)
                if math.isfinite(candidate) and candidate >= 0:
                    if current == "high":
                        high = candidate
                    else:
                        critical = candidate
    return high, critical


def _assign_severity(deviation_pct: float, high: float, critical: float) -> str:
    abs_dev = abs(deviation_pct)
    if abs_dev >= critical:
        return "critical"
    if abs_dev >= high:
        return "high"
    return "medium"  # the threshold gate upstream already cleared this finding


def _severity_rank(severity: str) -> int:
    return {"critical": 3, "high": 2, "medium": 1, "normal": 0}.get(severity, 0)


def round_to_grid(amount: float) -> int:
    """Round a monetary amount onto the reported grid."""
    return int(round(amount / MONETARY_ROUND_UNIT) * MONETARY_ROUND_UNIT)


def _render_amount(amount: float) -> str:
    """Render a monetary amount as a grouped aggregate on the grid."""
    return f"{round_to_grid(amount):,d}"


def build_description(metric: str, current: float, baseline: float, dev_pct: float) -> str:
    """Describe one finding in the professional-services vocabulary."""
    entry: Optional[Tuple[str, str]] = _ANOMALY_MAP.get(metric)
    if entry is not None:
        cur: Any = _render_amount(current) if metric in _MONETARY_METRICS else current
        base: Any = _render_amount(baseline) if metric in _MONETARY_METRICS else baseline
        try:
            return entry[1].format(dev=abs(dev_pct), base=base, cur=cur)
        except (KeyError, ValueError, TypeError):
            pass
    direction = "above" if dev_pct > 0 else "below"
    return (
        f"Metric '{metric}' is {abs(dev_pct):.1f}% {direction} baseline " f"(current: {current}, baseline: {baseline})."
    )


class ClassifyAnomalyNode(FunctionNode):
    """Classify threshold-exceeded deviations into anomaly types with severities.

    Inner node — declared ANONYMOUS so the outer invocation context passes the
    subgraph boundary without rejection.

    Input state keys:
        deviations:       list[dict] — from DetectDeviationsNode
        operations_data:  dict       — for engagement context
        runtime_settings: dict       — declared severity bands

    Output state keys (partial dict):
        anomaly_classifications: list[dict]
        anomaly_count:           int
        alert_severity:          str
        status:                  str
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        deviations: List[Dict[str, Any]] = state.get("deviations") or []
        operations_data: Dict[str, Any] = state.get("operations_data") or {}
        engagement_id = operations_data.get("engagement_id", "unknown")
        high, critical = _severity_bands(state)

        flagged = [d for d in deviations if d.get("threshold_exceeded", False)]

        if not flagged:
            logger.info("ClassifyAnomalyNode: engagement=%s — no anomalies detected", engagement_id)
            emit_trace_event(
                "anomalies_classified",
                {"engagement_id": engagement_id, "anomaly_count": 0, "alert_severity": "normal"},
                state,
            )
            return {
                "anomaly_classifications": [],
                "anomaly_count": 0,
                "alert_severity": "normal",
                "status": AgentStatus.SUCCESS.value,
            }

        classifications: List[Dict[str, Any]] = []
        worst_severity = "normal"

        for dev in flagged:
            metric = str(dev["metric"])
            current = float(dev["current"])
            baseline = float(dev["baseline"])
            dev_pct = float(dev["deviation_pct"])

            severity = _assign_severity(dev_pct, high, critical)
            if _severity_rank(severity) > _severity_rank(worst_severity):
                worst_severity = severity

            entry = _ANOMALY_MAP.get(metric)
            anomaly_type = entry[0] if entry is not None else "metric_anomaly"

            classifications.append(
                {
                    "type": anomaly_type,
                    "severity": severity,
                    "metric": metric,
                    "current_value": current,
                    "baseline_value": baseline,
                    "deviation_pct": dev_pct,
                    "description": build_description(metric, current, baseline, dev_pct),
                }
            )

        classifications.sort(key=lambda c: -_severity_rank(str(c["severity"])))

        logger.info(
            "ClassifyAnomalyNode: engagement=%s anomalies=%d severity=%s",
            engagement_id,
            len(classifications),
            worst_severity,
        )
        emit_trace_event(
            "anomalies_classified",
            {
                "engagement_id": engagement_id,
                "anomaly_count": len(classifications),
                "alert_severity": worst_severity,
                "anomaly_types": [c["type"] for c in classifications],
            },
            state,
        )

        return {
            "anomaly_classifications": classifications,
            "anomaly_count": len(classifications),
            "alert_severity": worst_severity,
            "status": AgentStatus.SUCCESS.value,
        }
