"""HCR-C2-113 — inner domain workflow graph (Cat 2).

Instantiated by BatchRecordEvidenceValidateWorkflowGraphNode.get_subgraph() in graph.py. Linear topology
with per-node skip guards (the portable Cat 2 form; conditional edges don't propagate across the subgraph
boundary):

    START → schema_field_locate → evidence_completeness_check → citation_provenance_check → exception_register_compose → END

On rejected / 0-record input, schema_field_locate sets located_count=0 (+error_code);
evidence_completeness_check and citation_provenance_check no-op and exception_register_compose emits the
out-of-scope safe answer — no fabricated register.
"""

from __future__ import annotations
from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState

from src.nodes.citation_provenance_check_node import CitationProvenanceCheckNode
from src.nodes.evidence_completeness_check_node import EvidenceCompletenessCheckNode
from src.nodes.exception_register_compose_node import ExceptionRegisterComposeNode
from src.nodes.schema_field_locate_node import SchemaFieldLocateNode
from src.schemas.state import State


class BatchRecordEvidenceValidateWorkflow(BaseGraph):
    """Inner graph: schema_field_locate → evidence_completeness_check → citation_provenance_check → exception_register_compose."""

    @property
    def name(self) -> str:
        return "BatchRecordEvidenceValidateWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract.
        self._nodes["schema_field_locate"] = SchemaFieldLocateNode()
        self._nodes["evidence_completeness_check"] = EvidenceCompletenessCheckNode()
        self._nodes["citation_provenance_check"] = CitationProvenanceCheckNode()
        self._nodes["exception_register_compose"] = ExceptionRegisterComposeNode()

    def add_edges(self) -> None:
        # Static linear backbone; the 0-record / rejected skip is handled by per-node guards.
        self._sg.add_edge(START, "schema_field_locate")
        self._sg.add_edge("schema_field_locate", "evidence_completeness_check")
        self._sg.add_edge("evidence_completeness_check", "citation_provenance_check")
        self._sg.add_edge("citation_provenance_check", "exception_register_compose")
        self._sg.add_edge("exception_register_compose", END)

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph ABC. Linear topology → not wired to a conditional edge."""
        if state.get("error_code") or state.get("located_count", 0) == 0:
            return "exception_register_compose"
        return "evidence_completeness_check"

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            "output": state.get("result"),
            "status": state.get("status"),
            "located_count": state.get("located_count", 0),
            "review_required": state.get("review_required", False),
            "error_code": state.get("error_code"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
