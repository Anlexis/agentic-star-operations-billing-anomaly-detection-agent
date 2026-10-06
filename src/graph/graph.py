"""AgentCore Platform v1.0"""

# SVC-C2-010 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (retry)
#                                          pre_process
#
#   The `main` slot is a GraphNode subclass (AnomalyDetectionGraphNode) that
#   delegates the whole domain workflow to DomainWorkflowGraph (inner BaseGraph).
#   Domain complexity lives entirely inside the inner graph; the outer backbone is
#   never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#   src/graph/context_bridge.py        ← outer → inner hand-off
#
# Runtime configuration:
#   config/agent.yaml is the static registration manifest — identity only, no
#   tuning. config/config.yaml holds every runtime parameter. The registry loads
#   that file and passes it as Graph(config=...); the standalone server does the
#   same through _runtime_config(), so a registry-loaded agent and a deployed one
#   see identical settings. Every declared value is validated once in
#   declared_settings() and then travels to its consumer — there is no second copy
#   of the defaults on disk.

import math
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional, Tuple, cast

from framework.schemas.agent_status import AgentStatus
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.graph.context_bridge import set_caller_input_context
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.schemas.state import State

# Runtime parameters: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Built-in settings, used only for keys config/config.yaml does not declare or
# declares out of contract. This is the pipeline's floor, not a mirror of the
# file: a value present in the file always wins, which is what makes the
# declaration observable end to end.
_BUILTIN_SETTINGS: Dict[str, Any] = {
    "deviation_threshold_pct": 15.0,
    "severity_high_pct": 30.0,
    "severity_critical_pct": 50.0,
    "target_utilization_rate": 0.75,
    "expected_sla_breach_count": 0.5,
    "expense_ratio_benchmark": 0.28,
    "standard_billable_hours_per_person": 480.0,
    "standard_rate_jpy_per_hour": 12000.0,
    "max_metrics": 32,
    "max_identifier_length": 32,
}

# Bounds every declared setting is checked against. A declared value outside its
# bound is not a preference the pipeline can honour, so the floor is kept instead.
_SETTING_BOUNDS: Dict[str, Tuple[float, float]] = {
    "deviation_threshold_pct": (0.0, 1000.0),
    "severity_high_pct": (0.0, 100000.0),
    "severity_critical_pct": (0.0, 100000.0),
    "target_utilization_rate": (0.0, 10.0),
    "expected_sla_breach_count": (0.0, 100000.0),
    "expense_ratio_benchmark": (0.0, 10.0),
    "standard_billable_hours_per_person": (1.0, 100000.0),
    "standard_rate_jpy_per_hour": (1.0, 1000000.0),
}

_INT_SETTING_BOUNDS: Dict[str, Tuple[int, int]] = {
    "max_metrics": (1, 512),
    "max_identifier_length": (1, 128),
}

# Where each declared key lives in config/config.yaml.
_SETTING_SECTIONS: Dict[str, str] = {
    "deviation_threshold_pct": "detection",
    "severity_high_pct": "detection",
    "severity_critical_pct": "detection",
    "target_utilization_rate": "baselines",
    "expected_sla_breach_count": "baselines",
    "expense_ratio_benchmark": "baselines",
    "standard_billable_hours_per_person": "baselines",
    "standard_rate_jpy_per_hour": "baselines",
    "max_metrics": "caller_contract",
    "max_identifier_length": "caller_contract",
}


def _runtime_config() -> Dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the file the registry loads and passes as Graph(config=...); the
    standalone server (src/api/server.py) reads it here so both deployments run on
    the same declaration. Returns an empty mapping — never raises — when the file
    is absent, unreadable, not valid YAML, or not a mapping; the graph then runs
    on its built-in floor. PyYAML is imported lazily because it is a framework
    runtime dependency rather than a module-load coupling of this template.
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return cast(Dict[str, Any], loaded)


def _config_number(value: Any, lo: float, hi: float) -> Optional[float]:
    """Validate one declared numeric setting: a real number, finite, within bounds.

    Bools, strings, other non-numerics, NaN/Infinity and out-of-range values all
    return None, and the consumer keeps its built-in floor. Rejecting non-finite
    values matters even for a declared setting: NaN compares False against every
    bound, so a NaN threshold would silently disable the detection it configures
    rather than fail.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def declared_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and flatten the declared runtime settings for the pipeline.

    `config` is what the graph was constructed with — the contents of
    config/config.yaml. Each value is checked for type, finiteness and range; an
    absent or out-of-contract key falls back to the built-in floor rather than
    propagating a value no consumer could use.
    """
    settings: Dict[str, Any] = dict(_BUILTIN_SETTINGS)

    def _section(name: str) -> Dict[str, Any]:
        block = config.get(name)
        return block if isinstance(block, dict) else {}

    for key, (lo, hi) in _SETTING_BOUNDS.items():
        declared = _config_number(_section(_SETTING_SECTIONS[key]).get(key), lo, hi)
        if declared is not None:
            settings[key] = declared

    for key, (lo_i, hi_i) in _INT_SETTING_BOUNDS.items():
        declared = _config_number(_section(_SETTING_SECTIONS[key]).get(key), float(lo_i), float(hi_i))
        if declared is not None and declared == int(declared):
            settings[key] = int(declared)

    return settings


class AnomalyDetectionGraphNode(GraphNode):
    """GraphNode assigned to the `main` slot of BillingAnomalyDetectionAgent.

    Wraps DomainWorkflowGraph (the inner Cat 2 BaseGraph) and forwards the
    validated runtime settings to it.

    Contracts:
      get_subgraph()    — instantiate and return DomainWorkflowGraph
      extract_input()   — hand the inner graph its input and bridge the payload
      merge_output()    — map sub_result fields into the outer state delta
      error_strategy    — "propagate": re-raise inner errors (fail-fast)
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        """Bind the validated runtime settings forwarded by the outer graph."""
        super().__init__()
        self._settings: Dict[str, Any] = dict(settings or _BUILTIN_SETTINGS)

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the validated runtime settings to the inner graph.

        The values come from the outer graph's own config — config/config.yaml as
        loaded by the registry, or by the standalone server through
        _runtime_config() — validated once in declared_settings(). The inner graph
        republishes them into inner state (DomainWorkflowGraph._extra_initial_state)
        so the domain nodes read a live threshold rather than a dead declaration.
        """
        return {"settings": dict(self._settings)}

    def execute(self, state: AgentState) -> Dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request the caller-contract gate declined has no validated payload to
        act on, so running the workflow would only reach the first domain node,
        fail its own precondition, and terminate the run - replacing the
        specific, actionable reason already settled with a vaguer one.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        return cast(Dict[str, Any], super().execute(state))

    def get_subgraph(self) -> Any:
        """Instantiate the inner domain workflow graph.

        Imported inside the method to keep module load free of a circular import
        between the two graph modules. The inner graph receives the settings
        through the BaseGraph constructor; its domain NODES still take no
        constructor arguments and read what they need from state.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the inner graph's input string, and bridge the caller payload.

        GraphNode.extract_input() returns a STRING the framework writes into the
        inner state as `user_input`; no other outer field is forwarded. That
        string is also one the platform's input gate rewrites in place, so the
        validated payload travels on the bridge instead and only the inert summary
        goes through here (see src/graph/context_bridge.py).
        """
        set_caller_input_context(
            {
                "caller_payload": state.get("caller_payload") or {},
                "runtime_settings": dict(self._settings),
            }
        )
        return cast(str, state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner graph's output back into the outer state delta.

        sub_result is the mapping DomainWorkflowGraph.get_output() returns.
        Returns ONLY changed keys — never the full state. Every key read here is
        one the inner get_output() emits; the two are written together.
        """
        return {
            "management_alert": sub_result.get("management_alert"),
            "anomaly_count": sub_result.get("anomaly_count", 0),
            "alert_severity": sub_result.get("alert_severity", "normal"),
            # Forwarded so OutputFormatNode has the engagement context and the
            # baseline provenance it renders.
            "operations_data": sub_result.get("operations_data"),
            "baseline_sources": sub_result.get("baseline_sources"),
            "unbaselined_metrics": sub_result.get("unbaselined_metrics"),
            "status": sub_result.get("status"),
        }


class BillingAnomalyDetectionAgent(AgentBaseGraph):
    """Outer graph for SVC-C2-010 (Cat 2).

    Inherits AgentBaseGraph directly. Domain logic is fully encapsulated in
    AnomalyDetectionGraphNode (main slot), which delegates to DomainWorkflowGraph.

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills initialize and finalize
      - pre_process : InputValidateNode  (VERIFIED_EXTERNAL — caller contract)
      - main        : AnomalyDetectionGraphNode
      - post_process: OutputFormatNode   (ANONYMOUS — output boundary)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.

    Runtime configuration: the registry loads config/config.yaml and passes it as
    Graph(config=...); the standalone server does the same via _runtime_config().
    AgentBaseGraph consumes max_retry from that config for retry routing, the
    validated detection and benchmark settings reach the inner graph through
    AnomalyDetectionGraphNode, and the caller-contract bounds reach the
    pre_process node through the initial state — so every declared value is live
    in both deployments.

    The class name matches config/agent.yaml's `class:` entry point exactly.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the platform registry."""
        return "BillingAnomalyDetectionAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all five backbone slots.

        super().register_nodes() MUST be called first — it injects the framework's
        default initialize node (schema version, session id, trust level) and
        finalize node (response metadata, total time).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = InputValidateNode()
        self._nodes["main"] = AnomalyDetectionGraphNode(settings=declared_settings(self.config))
        self._nodes["post_process"] = OutputFormatNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated runtime settings into the outer initial state.

        The node contract takes no config argument and every node is constructed
        without one, so what the bound InputValidateNode needs — the caller
        contract bounds — travels through State.
        """
        return {"runtime_settings": declared_settings(self.config)}


# Module-level alias for the registry module path and for src/api/server.py.
Graph = BillingAnomalyDetectionAgent
