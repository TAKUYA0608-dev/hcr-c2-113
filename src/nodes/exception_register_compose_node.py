"""HCR-C2-113 — inner workflow step 4 (SoT Step 6, deliverable): exception_register_compose.

Composes the **EvidenceCompletenessRegister** deliverable: a completeness summary, and a per-field register
entry (schema field, present/missing status, document/page citation where grounded, deterministic
confidence, missing-data + needs-pharmacist-review flags, cited schema clause), each present finding cited to
its record source. Entries are ordered so critical gaps surface first. The register is candidate / advisory
only — it never asserts sterility, releases a preparation, changes the record, or makes a clinical decision.
Sets ``review_required`` once any finding needs pharmacist review. On the 0-located / rejected branch it
emits the out-of-scope safe answer.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import BatchRecordEvidenceService
from src.utils.audit import emit_trace_event

_OUT_OF_SCOPE = (
    "評価可能な無菌調製バッチ記録が入力に見つかりませんでした。"
    "records 配列に record_id・source（認可済み文書システムの参照）・evidence（field / doc_page / value）を"
    "含む JSON をご指定いただくか、対象範囲を明確にしてください。"
)

# Order register entries so critical evidence gaps surface first, then ungrounded present findings.
_STATUS_RANK = {"missing": 2, "present": 1}


class ExceptionRegisterComposeNode(FunctionNode):
    """Compose the EvidenceCompletenessRegister deliverable with citations (or safe answer on 0-located)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        checked = json.loads(state.get("provenance_checked") or "[]")
        if state.get("error_code") or not checked:
            emit_trace_event(
                "exception_register_compose.safe", {"reason": state.get("error_code") or "no_findings"}, state
            )
            report: dict[str, Any] = {
                "status_kind": "out_of_scope",
                "message": _OUT_OF_SCOPE,
                "register_summary": {},
                "register_entries": [],
                "citations": [],
            }
            return {
                "result": json.dumps(report, ensure_ascii=False),
                "review_required": False,
                "review_status": "not_required",
                "status": AgentStatus.SUCCESS.value,
            }

        entries: list[dict[str, Any]] = []
        citations: list[dict[str, str]] = []
        for finding in checked:
            entry = BatchRecordEvidenceService.compose_entry(finding)
            entries.append(entry)
            # top-level authoritative citation only for a grounded (present + cited) finding
            if entry["status"] == "present" and entry.get("citation"):
                citations.append({"item_id": entry["item_id"], "source": entry["citation"]})
        entries.sort(
            key=lambda e: (
                -_STATUS_RANK.get(e["status"], 0),
                -int(bool(e["critical"])),
                e["field_key"],
                e["record_ref"],
            )
        )

        review_required = any(e["needs_pharmacist_review"] for e in entries)
        summary = BatchRecordEvidenceService.register_summary(entries)
        report = {
            "status_kind": "evidence_completeness_register",
            "scope": self._scope(state),
            "register_summary": summary,
            "register_entries": entries,
            "citations": citations,
        }
        emit_trace_event(
            "exception_register_compose.complete",
            {"field_count": len(entries), "citation_count": len(citations), "review_required": review_required},
            state,
        )
        return {
            "result": json.dumps(report, ensure_ascii=False),
            "review_required": review_required,
            "review_status": "needs_pharmacist_review" if review_required else "not_required",
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _scope(state: dict[str, Any]) -> dict[str, Any]:
        slots = json.loads(state.get("validated_input") or "{}")
        return {"scope": slots.get("scope")}
