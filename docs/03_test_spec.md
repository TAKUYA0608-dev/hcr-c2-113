# Test Specification — HCR-C2-113

## Strategy

Deterministic domain logic (**no LLM**) → exhaustive unit tests over the service + each node, plus real
`Graph().invoke()` end-to-end tests that exercise the actual outer-GraphNode → inner-workflow path (not
`execute()`-chaining). Security behaviour (S-1 … S-4) is asserted on the real invoke path so short-circuit
bugs are caught.

## Coverage (local, the local SDK stub framework)

- **Domain suites**: `tests/unit/test_nodes.py` (59) + `tests/unit/test_graph.py` (40) +
  `tests/integration/test_end_to_end.py` (5) = **104 collected → 103 passed, 1 skipped**
  (`TestServerModule::test_server_imports` skips when the platform `server.py` deps are unavailable in the
  local stub env).
- **Statement coverage**: **94% on `src/`** (`--cov=src`).
- **Env-diff (pass under the real SDK in CI, expected fail/skip locally on the local SDK stub)**:
  `tests/proof_of_boundary/test_pb_invoke_order.py`, `tests/unit/test_framework_compliance_tc06_tc07.py`
  (TC-06 / TC-07) — the local stub framework does not enforce the `@final` security-gate contract; the real
  `agenticstar-agentcore==1.0.0` SDK does. `test_pb7_hitl_interrupt_propagation.py` = 2 SKIPPED (hitl unused).

## Test Cases

### TC — deterministic service (`test_nodes.py::TestServiceIdentity` / `TestServiceEvidence`)
- Privacy tokenize `opaque_id` deterministic + prefixed; a forged surrogate (`rec:deadbeef`) is **re-hashed**.
- `resolve_provenance`: authorized clinical systems (`ebr:`/`emr_pharmacy:`/`qms:`/`dms:`) → `src:<sha8>`;
  a name / `unknown` / empty / forged surrogate (`src:1a2b3c4d`, `rec:deadbeef`) → `None` (fail-closed).
- `_safe_doc_page` constrains the anchor charset + length caps.
- locate produces one item per required field; alias maps to canonical key; unknown field ignored; non-dict
  evidence skipped; missing field has no anchor.
- assess: present+anchor → high; present no-anchor → medium; absent → missing/none.
- citation: present grounded → cited; present ungrounded → uncited + needs-review; missing critical →
  needs-review; missing non-critical → no review.
- compose entry is whitelist-by-construction (no `value` key); register summary rolls up present/missing +
  critical gaps.

### TC — pre_process S-1 / S-2 (`test_nodes.py::TestPreProcess`)
- JSON object / bare-list / NL text / JSON scalar parsing; record without `record_id` dropped.
- **Empty → `INPUT_REJECTED`; injection → `INJECTION_REJECTED` (body cleared); oversize → `INPUT_TOO_LONG`
  — all `status=SUCCESS` (degraded, never ERROR)**; `_extra_security_gate_input` sets error_code and does
  **not raise**.
- Record projected to `{record_id, source, evidence}` allowlist (patient_name / patient_id / diagnosis
  dropped); attestation **value reduced to a presence flag** (staff name not retained); `scope` credential /
  My-Number hygiened; unauthorized `source` dropped to `None`.

### TC — inner nodes (`test_nodes.py::TestInnerNodes`)
- locate complete / zero-records skip / error_code skip / all-malformed skip / non-dict slots.
- completeness complete / skip emits S-4; citation-check complete / skip emits S-4.
- compose safe-answer (out-of-scope, `citations=[]`) / grounded register (N citations).
- **Every skip path emits a count-only S-4 domain event** (asserted via `monkeypatch` on `_platform_emit`).

### TC — post_process S-3 fail-closed (`test_nodes.py::TestPostProcess`)
- grounded output has `citation_complete=True` + disclaimer; **all-missing register is a valid grounded gap
  report** (missing findings need no citation).
- **Per-entry fail-closed**: present finding with missing local citation / missing top-level citation /
  top-level citation for a *different* item → `needs_review` + body withheld + `CITATION_INCOMPLETE`.
- out-of-scope passthrough; `_extra_security_gate_output` raises when the disclaimer is missing; leaked phone
  redacted.

### TC — real `Graph().invoke()` (`test_graph.py::TestRealInvoke` + integration)
- **grounded register** (all present + cited) / **grounded register with gaps** (criticals missing →
  `human_review.required=True`) / **out-of-scope safe** (NL text).
- **injection & oversize degrade + audit**: `status=SUCCESS`, `PostProcessNode in node_history`,
  `status_kind=="out_of_scope"`, disclaimer present, rejected body absent, and the terminal S-4 emit carries
  `error_code` (`INJECTION_REJECTED` / `INPUT_TOO_LONG`) captured via `monkeypatch`.
- **missing provenance degrades** to `needs_review` (`CITATION_INCOMPLETE`), register body withheld.
- **PII / tokenization**: unsafe `source` (name/phone) not leaked; `scope` PII redacted; `record_id` PII
  tokenized; patient fields + arbitrary caller notes dropped; **attestation value never in output**;
  **no-space names** (`Alice` / `Taro.Yamada` / `TaroYamada`) still tokenized in `record_id` and not grounded
  in `source`; unverifiable + **forged-surrogate** sources → `needs_review`; authorized source → grounded
  (`src:<sha8>` citation, raw provenance not in output).
- integration: multi-record portfolio (grounded + gaps), mixed cited/uncited present → whole-register
  fail-closed, forged source not grounded, all-missing valid gap register, empty object → out-of-scope.

## Run

```bash
python -m pytest tests/ -v
python -m pytest tests/unit tests/integration --cov=src --cov-report=term-missing
python3 scripts/check_trust_level.py src/
```
