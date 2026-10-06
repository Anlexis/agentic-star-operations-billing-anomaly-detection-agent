"""AgentCore Platform v1.0"""

# Reference node for the sample graphs under src/examples/ — the smallest correct
# shape of a pre-process node, kept so a new template starts from a working one.
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus values — never a bare string
#  - Read input_context via state.get("input_context", {}) — read-only
#  - Emit an audit event on every decision the node makes
#  - Never import from the mediator, the api layer, or another agent
#
# This template's own pre-process node is src/nodes/input_validate_node.py.

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class PreProcessNode(FunctionNode):
    """Validate and enrich incoming input before main processing."""

    # Trust is explicit by design, never inherited implicitly. Raise it to
    # VERIFIED_EXTERNAL or INTERNAL when the node performs a privileged operation.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context: Dict[str, Any] = state.get("input_context", {}) or {}  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            emit_trace_event("pre_process_rejected", {"reason": "empty_input"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        emit_trace_event(
            "pre_process_completed",
            {"input_length": len(user_input.strip())},
            state,
        )
        return {
            "validated_input": user_input.strip(),
            "enriched_context": {
                "source": "BillingAnomalyDetectionAgent",
                "channel": input_context.get("channel", "unknown"),
            },
            "status": AgentStatus.SUCCESS.value,
        }
