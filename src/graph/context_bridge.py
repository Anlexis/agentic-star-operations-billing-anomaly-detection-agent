"""AgentCore Platform v1.0"""

# src/graph/context_bridge.py — carries the validated operations payload and the
# runtime settings across the outer -> inner graph boundary.
#
# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(<extract_input result>, session_id=..., ctx=...)` and forwards
# NO other field of the outer state. An inner-node read of state["caller_payload"]
# would therefore always see nothing on the full nested path, however carefully the
# outer node wrote it. The two sanctioned subclass hooks bridge the gap:
#
#   AnomalyDetectionGraphNode.extract_input(state)   [runs BEFORE subgraph.invoke]
#       -> set_caller_input_context({...validated payload + settings...})
#   DomainWorkflowGraph._extra_initial_state()       [runs INSIDE subgraph.invoke]
#       -> seeds the inner initial state from get_caller_input_context()
#
# Only values InputValidateNode has already validated are stashed here — the bridge
# is a transport, never a second contract.
#
# The payload travels on this channel for a second reason. The platform's input
# gate masks personal-name-shaped text on the user_input and validated_input
# fields, and it rewrites the string in place: an engagement label carried there
# arrives with parts of it replaced by a mask token, and the pipeline then reports
# on an identifier the caller never sent. A value carried on a domain state field
# is delivered verbatim, and this template screens that channel itself (see
# src/nodes/input_validate_node.py).
#
# A ContextVar keeps the hand-off correct per thread and per task, so concurrent
# invocations inside one process cannot see each other's payload.

from contextvars import ContextVar
from typing import Any, Dict, Optional

_CALLER_INPUT_CONTEXT: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "billing_anomaly_caller_input_context", default=None
)


def set_caller_input_context(payload: Optional[Dict[str, Any]]) -> None:
    """Stash the outer graph's validated request for the imminent inner-graph invoke."""
    _CALLER_INPUT_CONTEXT.set(dict(payload) if payload else {})


def get_caller_input_context() -> Dict[str, Any]:
    """Read (without consuming) the stashed request; {} when none was set."""
    return _CALLER_INPUT_CONTEXT.get() or {}
