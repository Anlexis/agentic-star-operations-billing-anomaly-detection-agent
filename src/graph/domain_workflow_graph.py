"""AgentCore Platform v1.0"""

# SVC-C2-010 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the professional-services billing anomaly detection workflow:
#
#   START
#     → load_baseline       (LoadOperationsBaselineNode)
#     → detect_deviations   (DetectDeviationsNode)
#     → classify_anomaly    (ClassifyAnomalyNode)
#     → generate_alert      (GenerateManagementAlertNode)
#     → END
#
# Called by AnomalyDetectionGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ All inner nodes declare required_trust_level = TrustLevel.ANONYMOUS
#   ✅ get_output() designed together with AnomalyDetectionGraphNode.merge_output()
#   ❌ No agenticstar imports
#   ❌ Not placed under src/subagents/

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.classify_anomaly_node import ClassifyAnomalyNode
from src.nodes.detect_deviations_node import DetectDeviationsNode
from src.nodes.generate_management_alert_node import GenerateManagementAlertNode
from src.nodes.load_operations_baseline_node import LoadOperationsBaselineNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for SVC-C2-010.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by AnomalyDetectionGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → load_baseline      (LoadOperationsBaselineNode)
          → detect_deviations  (DetectDeviationsNode)
          → classify_anomaly   (ClassifyAnomalyNode)
          → generate_alert     (GenerateManagementAlertNode)
          → END

    All nodes are FunctionNode subclasses with ANONYMOUS trust_level.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "svc_c2_010_billing_anomaly_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No key is mandatory: every runtime setting has a validated floor.

        The outer graph validates the declaration before forwarding it, so an
        absent or out-of-contract value has already been replaced by the floor and
        there is nothing left for this graph to refuse at compile time.
        """
        return None

    # ── Outer → inner hand-off ────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the inner initial state from the bridge.

        The framework forwards only the string extract_input() returned; the
        validated payload and the runtime settings arrive here instead. This runs
        inside subgraph.invoke(), immediately after the outer node stashed them.
        """
        bridged = get_caller_input_context()
        return {
            "caller_payload": bridged.get("caller_payload") or {},
            "runtime_settings": bridged.get("runtime_settings") or {},
        }

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 4 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        """
        self._nodes["load_baseline"] = LoadOperationsBaselineNode()
        self._nodes["detect_deviations"] = DetectDeviationsNode()
        self._nodes["classify_anomaly"] = ClassifyAnomalyNode()
        self._nodes["generate_alert"] = GenerateManagementAlertNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear anomaly-detection domain topology.

        Linear flow: load_baseline → detect_deviations → classify_anomaly
                     → generate_alert → END.

        No conditional branching — all anomaly paths are linear in v1.
        route() is implemented to satisfy the ABC but add_conditional_edges()
        is not used.
        """
        self._sg.add_edge(START, "load_baseline")
        self._sg.add_edge("load_baseline", "detect_deviations")
        self._sg.add_edge("detect_deviations", "classify_anomaly")
        self._sg.add_edge("classify_anomaly", "generate_alert")
        self._sg.add_edge("generate_alert", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        Linear topology; add_conditional_edges() is not used, so this
        method is never called at runtime.  Returns END on error so an
        unexpected invocation does not re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "generate_alert"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by AnomalyDetectionGraphNode.merge_output()
        in graph.py as the `sub_result` argument.  Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output()   emits: "management_alert", "anomaly_count",
                                        "alert_severity", "status", ...
            Outer merge_output() reads: sub_result.get("management_alert"),
                                        sub_result.get("anomaly_count"),
                                        sub_result.get("alert_severity"),
                                        sub_result.get("status")
        """
        return {
            "management_alert": state.get("management_alert"),
            "anomaly_count": state.get("anomaly_count", 0),
            "alert_severity": state.get("alert_severity", "normal"),
            "operations_data": state.get("operations_data"),
            "baseline_sources": state.get("baseline_sources"),
            "unbaselined_metrics": state.get("unbaselined_metrics"),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
            "correlation_id": state.get("correlation_id"),
        }
