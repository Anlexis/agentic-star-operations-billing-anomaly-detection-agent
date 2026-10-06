"""AgentCore Platform v1.0"""

# Reference node for the sample graphs under src/examples/ — the smallest correct
# shape of a post-process node, kept so a new template starts from a working one.
#
# This template's own post-process node is src/nodes/output_format_node.py, which
# also carries the output boundary.

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class PostProcessNode(FunctionNode):
    """Format and finalize the output."""

    # Trust is explicit by design, never inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        result = state.get("result", "")
        formatted = result if isinstance(result, str) else str(result)

        emit_trace_event(
            "post_process_completed",
            {"output_length": len(formatted)},
            state,
        )
        return {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }
