"""HCR-C2-113 — post_process node: OutputSanitise (S-3 output gate + S-4 audit).

S-3 (fail-closed): **enforce** per-entry citation completeness — a grounded EvidenceCompletenessRegister
whose *present* findings are not all cited to a verifiable source is never presented; it degrades to a safe
``needs_review`` answer with the register body withheld (``error_code=CITATION_INCOMPLETE``, still SUCCESS so
post/S-4/disclaimer run). Missing findings are the evidence gaps the register surfaces and need no citation.
Re-redact any credential / My-Number / email / phone / person- or company-name leakage (defense-in-depth),
and append the mandatory DRAFT / needs-pharmacist-review disclaimer — the register is a decision aid, not a
sterility / release / clinical decision; the final judgment is an authorized human pharmacist's. S-4
(no-persist): emit an audit event (counts / status distribution / review flag / error_code only — never a
patient name, patient ID, diagnosis, attestation value, or staff name); the raw record is not retained. Runs
on the full register, the citation-blocked branch, and the out-of-scope safe branch.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, cast

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.utils.audit import emit_trace_event

_DISCLAIMER = (
    "本レジスタは提供された認可済みデータに基づく参考用の DRAFT（候補・要薬剤師レビュー）エビデンス"
    "完全性チェック結果であり、無菌性の保証、製剤の出荷可否判定、調製記録の変更、投薬・臨床判断を行うもの"
    "ではありません。各所見は candidate であり、欠落の是正要否・無菌性・出荷可否・患者への投与を含む最終判断"
    "は、必ず認可された薬剤師 / 医師 / 品質責任者（人間）が行ってください。引用（provenance）を伴わない present "
    "所見は提示されず、要薬剤師レビューに routing されます。本エージェントは記載の照合と欠落フラグ付与のみを"
    "行い、実行は行いません。"
)

_CITATION_INCOMPLETE_MSG = (
    "レジスタの一部の present 所見に検証可能な出典（doc/page 引用 + 認可済み source）が確認できなかったため、"
    "根拠不十分な完全性所見の提示を差し控えました。各 present フィールドに doc_page と認可済みシステムの"
    "参照ID（source）を付与のうえ再実行してください。"
)
_NEEDS_REVIEW_NOTE = (
    "Grounding could not be verified for every present finding; the draft evidence-completeness register is "
    "withheld pending valid provenance and authorized human pharmacist review."
)

# S-3 defense-in-depth: re-redact secrets / contact info / person- or company-names that could leak into any
# free-text field of the register (applied to the whole serialized report before it becomes the output envelope).
_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|\d{12})\b")
_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}")
_PHONE = re.compile(r"(?<![\d.])(?:(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4})(?![\d.])")
# Person / company names: an English name run ending in a corporate suffix, or a Japanese company form.
_NAME = re.compile(
    r"(?:[A-Z][A-Za-z0-9&.\-]*\s){1,4}(?:Inc|Corp|Corporation|Ltd|LLC|LLP|GmbH|PLC|K\.?K|KK)\b\.?"
    r"|[^\s\"',]{1,24}(?:株式会社|有限会社|合同会社)"
    r"|(?:株式会社|有限会社|合同会社)[^\s\"',]{1,24}"
)
_REDACTORS = (_SECRET, _EMAIL, _PHONE, _NAME)


def _redact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Serialize → redact secret / contact / name patterns → deserialize (whole-report defense)."""
    text = json.dumps(report, ensure_ascii=False)
    for pattern in _REDACTORS:
        text = pattern.sub("[REDACTED]", text)
    return cast(dict[str, Any], json.loads(text))


class PostProcessNode(FunctionNode):
    """Verify citations, redact leakage, append the DRAFT needs-pharmacist-review disclaimer, emit S-4 audit."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3 preservation check: the DRAFT / needs-pharmacist-review disclaimer must be present in output.

        SDK 1.0.0 contract: receives the **result dict from ``execute()``**; returns the (possibly filtered)
        result. MAY raise to block an output missing the mandatory disclaimer.
        """
        out = result.get("formatted_output", "")
        if out and "参考" not in out and "DRAFT" not in out:
            raise ValueError("S-3: DRAFT / needs-pharmacist-review disclaimer missing from output")
        return dict(result)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = _redact_report(json.loads(state.get("result", "{}") or "{}"))

        grounded = report.get("status_kind") == "evidence_completeness_register"
        citations = report.get("citations", [])
        entries = report.get("register_entries", [])
        # S-3 per-entry authoritative correspondence: every *present* register entry must carry BOTH its own
        # local citation AND an exact top-level {item_id, source} citation for the same item (not merely a
        # non-empty citation list — a partially ungrounded present finding, or a top-level citation belonging
        # to a different item, must fail closed). Missing entries are the evidence gaps and need no citation.
        cited_sources = {c.get("item_id"): c.get("source") for c in citations if c.get("source")}
        present_entries = [e for e in entries if e.get("status") == "present"]
        citation_complete = (not grounded) or all(
            e.get("citation") and cited_sources.get(e.get("item_id")) == e.get("citation") for e in present_entries
        )

        # S-3 fail-closed: an ungrounded present finding (any present entry missing a verifiable citation) is
        # never presented. Degrade to a safe needs-review answer (SUCCESS + error_code), withhold the register
        # body, and still run the disclaimer + terminal S-4 audit.
        if grounded and not citation_complete:
            error_code = state.get("error_code") or "CITATION_INCOMPLETE"
            blocked: dict[str, Any] = {
                "status_kind": "needs_review",
                "scope": report.get("scope"),
                "register_summary": {},
                "register_entries": [],  # incomplete register body withheld
                "human_review": {"required": True, "status": "needs_pharmacist_review", "note": _NEEDS_REVIEW_NOTE},
                "citations": [],
                "citation_complete": False,
                "message": _CITATION_INCOMPLETE_MSG,
                "disclaimer": _DISCLAIMER,
            }
            emit_trace_event(
                "output_sanitise.citation_blocked",
                {"present_finding_count": len(present_entries), "error_code": error_code},
                state,
            )
            return {
                "formatted_output": json.dumps(blocked, ensure_ascii=False),
                "disclaimer": _DISCLAIMER,
                "audit_logged": True,
                "error_code": error_code,
                "status": AgentStatus.SUCCESS.value,
            }

        review_required = bool(state.get("review_required")) or any(e.get("needs_pharmacist_review") for e in entries)
        human_review = {
            "required": review_required,
            "status": "needs_pharmacist_review" if review_required else "not_required",
            "note": "Gap remediation, sterility, release, and administration must be decided by an authorized "
            "human pharmacist / quality lead. This agent produces a candidate register only.",
        }

        formatted = {
            "status_kind": report.get("status_kind"),
            "scope": report.get("scope"),
            "register_summary": report.get("register_summary", {}),
            "register_entries": entries,
            "human_review": human_review,
            "citations": citations,
            "citation_complete": citation_complete,
            "message": report.get("message"),
            "disclaimer": _DISCLAIMER,
        }
        emit_trace_event(
            "output_sanitise.complete",
            {
                "status_kind": report.get("status_kind"),
                "field_count": len(entries),
                "review_required": review_required,
                "citation_complete": citation_complete,
                "error_code": state.get("error_code"),
            },
            state,
        )
        return {
            "formatted_output": json.dumps(formatted, ensure_ascii=False),
            "disclaimer": _DISCLAIMER,
            "audit_logged": True,
            "status": AgentStatus.SUCCESS.value,
        }
