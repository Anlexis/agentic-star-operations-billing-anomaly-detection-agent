"""AgentCore Platform v1.0"""

# SVC-C2-010 — GenerateManagementAlertNode
# Inner domain node 4 (last in DomainWorkflowGraph): generate a structured
# management alert from the classified anomalies, including recommended
# management actions for each anomaly type.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import uuid
from typing import Any, ClassVar, Dict, List, Set

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Recommended management actions per anomaly type (v1: rule-based playbook).
_ACTION_PLAYBOOK: Dict[str, List[str]] = {
    "billing_overrun": [
        "Review timesheet records for the billing period with engagement manager.",
        "Confirm scope vs. SOW; initiate change-order process if scope was expanded.",
        "Set real-time billing alerts for the next billing cycle.",
    ],
    "utilization_spike": [
        "Assess team workload distribution; consider capacity reallocation.",
        "Check for shadow work or unpaid activities inflating utilisation.",
        "Review headcount planning for risk of burnout or attrition.",
    ],
    "sla_breach_cluster": [
        "Initiate SLA post-mortem with delivery lead within 48 hours.",
        "Review SLA definitions and identify root causes of repeated breaches.",
        "Implement early-warning SLA monitoring dashboard.",
    ],
    "margin_compression": [
        "Conduct a line-item expense review with finance.",
        "Identify and eliminate non-billable expense categories.",
        "Renegotiate vendor/subcontractor rates where applicable.",
    ],
    "revenue_underperform": [
        "Compare billable vs. billed hours to detect write-offs or discounts.",
        "Review pricing model and validate contract rates are applied correctly.",
        "Escalate to account manager if client disputes are present.",
    ],
    "metric_anomaly": [
        "Investigate data quality for the flagged metric.",
        "Consult operations team for domain-specific root-cause analysis.",
    ],
}

# Summary templates by severity.
_SUMMARY_TEMPLATES: Dict[str, str] = {
    "critical": (
        "CRITICAL alert for engagement {eid} — {count} anomaly/anomalies detected "
        "including critical-severity findings. Immediate management attention required."
    ),
    "high": (
        "HIGH-severity alert for engagement {eid} — {count} anomaly/anomalies detected. "
        "Management review recommended within 24 hours."
    ),
    "medium": (
        "MEDIUM-severity alert for engagement {eid} — {count} anomaly/anomalies detected. "
        "Scheduled review recommended at next operations meeting."
    ),
    "normal": (
        "No significant anomalies detected for engagement {eid}. " "Operations appear within normal parameters."
    ),
}


def _build_actions(classifications: List[Dict[str, Any]]) -> List[str]:
    """Build a deduplicated list of recommended management actions."""
    seen: Set[str] = set()
    actions: List[str] = []
    for cls in classifications:
        anomaly_type = cls.get("type", "metric_anomaly")
        for action in _ACTION_PLAYBOOK.get(anomaly_type, _ACTION_PLAYBOOK["metric_anomaly"]):
            if action not in seen:
                seen.add(action)
                actions.append(action)
    return actions


class GenerateManagementAlertNode(FunctionNode):
    """Generate structured management alert from classified anomaly findings.

    Inner node — declared ANONYMOUS so the outer VERIFIED_EXTERNAL InvocationContext
    passes through the GraphNode boundary without rejection (ADR trust model).

    Input state keys:
        anomaly_classifications: list[dict]  — from ClassifyAnomalyNode
        anomaly_count:           int
        alert_severity:          str
        operations_data:         dict        — for engagement context

    Output state keys (partial dict):
        management_alert: dict
        status:           str
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        classifications: List[Dict[str, Any]] = state.get("anomaly_classifications") or []
        anomaly_count: int = state.get("anomaly_count") or 0
        alert_severity: str = state.get("alert_severity") or "normal"
        operations_data: Dict[str, Any] = state.get("operations_data") or {}
        baseline_sources: Dict[str, str] = state.get("baseline_sources") or {}
        unbaselined: List[str] = list(state.get("unbaselined_metrics") or [])

        engagement_id = operations_data.get("engagement_id", "unknown")
        billing_period = operations_data.get("billing_period", "unknown")

        # ── Build summary ─────────────────────────────────────────────────────
        summary_tmpl = _SUMMARY_TEMPLATES.get(alert_severity, _SUMMARY_TEMPLATES["normal"])
        summary = summary_tmpl.format(eid=engagement_id, count=anomaly_count)

        # ── Build findings list (no personal data reaches the alert) ──────────
        findings: List[Dict[str, Any]] = []
        for cls in classifications:
            metric = cls.get("metric")
            findings.append(
                {
                    "anomaly_type": cls.get("type"),
                    "severity": cls.get("severity"),
                    "metric": metric,
                    "deviation_pct": cls.get("deviation_pct"),
                    "baseline_source": baseline_sources.get(str(metric), "benchmark"),
                    "description": cls.get("description"),
                }
            )

        # ── Build recommended actions ──────────────────────────────────────────
        recommended_actions = _build_actions(classifications)

        # ── Assemble alert ─────────────────────────────────────────────────────
        # Coverage is stated, not implied: a metric with no baseline was never
        # compared, and a reader who is not told that will read its absence from
        # the findings as a clean result.
        alert_id = f"ALERT-{engagement_id}-{uuid.uuid4().hex[:8].upper()}"
        management_alert: Dict[str, Any] = {
            "alert_id": alert_id,
            "engagement_id": engagement_id,
            "billing_period": billing_period,
            "severity": alert_severity,
            "anomaly_count": anomaly_count,
            "summary": summary,
            "findings": findings,
            "recommended_actions": recommended_actions,
            "unanalysed_metrics": unbaselined,
        }

        logger.info(
            "GenerateManagementAlertNode: alert_id=%s engagement_id=%s " "severity=%s anomalies=%d actions=%d",
            alert_id,
            engagement_id,
            alert_severity,
            anomaly_count,
            len(recommended_actions),
        )
        emit_trace_event(
            "management_alert_generated",
            {
                "alert_id": alert_id,
                "engagement_id": engagement_id,
                "severity": alert_severity,
                "anomaly_count": anomaly_count,
                "action_count": len(recommended_actions),
            },
            state,
        )

        return {
            "management_alert": management_alert,
            "status": AgentStatus.SUCCESS.value,
        }
