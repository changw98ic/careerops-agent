from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from careerops.api.errors import (
    DependencyNotReadyError,
    IdempotencyKeyReusedError,
    InvalidStateError,
    SmartIntakeDisabledError,
    SmartPreviewExpiredError,
    SmartPreviewInProgressError,
)
from careerops.api.routes.smart_intake import (
    SmartIntakeApplyRequest,
    SmartIntakeContextRefs,
    SmartIntakeDecisionRequest,
    SmartIntakeInterviewRefs,
    SmartIntakePreviewRequest,
    SmartIntakePreviewResponse,
    SmartIntakeTextInput,
    _authorize,
    _service,
    _to_response,
)
from careerops.application import smart_intake as smart_intake_module
from careerops.application.smart_intake import (
    ApplyDecisionInput,
    ModelField,
    ModelOutput,
    SmartIntakeRequest,
    SmartIntakeService,
    SourceRef,
    _bounded,
    _build_patch,
    _canonical_decisions,
    _canonical_json,
    _decision_metadata,
    _digest,
    _is_expired,
    _model_schema,
    _normalize_text,
    _preview_result,
    _request_fingerprint,
    _request_from_row,
    _system_prompt,
    _trusted_fields,
    _user_prompt,
    _value_allowed,
)
from careerops.infrastructure import smart_intake_retention as retention_module
from careerops.infrastructure.rate_limit import RateLimitAction

CANDIDATE = UUID("00000000-0000-0000-0000-000000000001")
INPUT = "上海 Backend Engineer Python"
DIGEST = _digest(INPUT)


def ref(start: int = 0, end: int = len(INPUT)) -> SourceRef:
    return SourceRef(input_digest=DIGEST, start_offset=start, end_offset=end)


def field(
    path: str,
    value: object,
    *,
    value_type: str = "string",
    status: str = "proposed",
    source_refs: list[SourceRef] | None = None,
) -> ModelField:
    return ModelField(
        path=path,
        value=value,
        value_type=value_type,  # type: ignore[arg-type]
        confidence=0.9,
        status=status,  # type: ignore[arg-type]
        reason="from input",
        source_refs=[ref()] if source_refs is None else source_refs,
    )


def test_normalization_is_nfc_and_normalizes_newlines() -> None:
    assert _normalize_text("e\u0301\r\n上海\r", "profile") == "é\n上海\n"
    with pytest.raises(InvalidStateError):
        _normalize_text("x" * 2_001, "interview_context")
    assert _normalize_text("", "profile") == ""


def test_model_contract_is_closed_and_target_specific() -> None:
    profile_schema = _model_schema("profile")
    interview_schema = _model_schema("interview_context")
    assert profile_schema["additionalProperties"] is False
    assert interview_schema["additionalProperties"] is False
    profile_properties = cast(dict[str, object], profile_schema["properties"])
    profile_fields = cast(dict[str, object], profile_properties["fields"])
    profile_items = cast(dict[str, object], profile_fields["items"])
    profile_item_properties = cast(dict[str, object], profile_items["properties"])
    profile_path = cast(dict[str, object], profile_item_properties["path"])
    interview_properties = cast(dict[str, object], interview_schema["properties"])
    interview_fields = cast(dict[str, object], interview_properties["fields"])
    interview_items = cast(dict[str, object], interview_fields["items"])
    interview_item_properties = cast(dict[str, object], interview_items["properties"])
    interview_path = cast(dict[str, object], interview_item_properties["path"])
    assert "locations[i].radius_km" in cast(str, profile_path["description"])
    assert "user_context" in cast(str, interview_path["description"])
    with pytest.raises(ValidationError):
        ModelOutput.model_validate({"fields": [], "unexpected": True})


def test_trusted_fields_require_allowlisted_paths_and_source_spans() -> None:
    trusted = _trusted_fields(
        "profile",
        [
            field("target_roles[0].title", "Backend Engineer"),
            field(
                "locations[0].name",
                "上海",
                source_refs=[ref(INPUT.index("上海"), INPUT.index("上海") + 2)],
            ),
            field("remote_rules.remote_allowed", True, value_type="boolean"),
            field("target_roles[0].title", "duplicate"),
            field("include_keywords[0]", "Go", source_refs=[]),
            field("target_roles[1].title", "Not present", source_refs=[ref(0, 2)]),
        ],
        INPUT,
        DIGEST,
    )
    assert [item["path"] for item in trusted] == [
        "target_roles[0].title",
        "locations[0].name",
        "remote_rules.remote_allowed",
        "include_keywords[0]",
        "target_roles[1].title",
    ]
    assert trusted[0]["status"] == "proposed"
    assert trusted[1]["status"] == "proposed"
    assert trusted[2]["status"] == "blocked"
    assert trusted[3]["status"] == "blocked"
    assert trusted[4]["status"] == "blocked"

    interview = _trusted_fields(
        "interview_context",
        [field("user_context", "prepare", source_refs=[])],
        "prepare",
        _digest("prepare"),
    )
    assert interview[0]["status"] == "proposed"
    assert interview[0]["source_refs"] == [
        {"input_digest": _digest("prepare"), "start_offset": 0, "end_offset": 7}
    ]


def test_value_allowlist_is_typed() -> None:
    assert _value_allowed("target_roles[0].title", "Backend", "string")
    assert _value_allowed("locations[0].radius_km", 50, "integer")
    assert _value_allowed("locations[0].radius_km", None, "integer")
    assert not _value_allowed("locations[0].radius_km", "50", "string")
    assert not _value_allowed("target_roles[0].title", ["Backend"], "string_list")
    assert not _value_allowed("remote_rules.remote_allowed", True, "boolean")


def test_profile_limits_and_interview_output_limits_are_enforced() -> None:
    assert not _value_allowed("target_roles[5].title", "Backend", "string")
    assert not _value_allowed("locations[8].name", "Shanghai", "string")
    assert not _value_allowed("include_keywords[30]", "Python", "string")
    assert not _value_allowed("target_roles[0].title", "x" * 121, "string")
    assert not _value_allowed("target_roles[0].seniority", "x" * 81, "string")
    assert not _value_allowed("locations[0].name", "x" * 121, "string")
    assert not _value_allowed("include_keywords[0]", "x" * 81, "string")

    blocked = _trusted_fields(
        "interview_context",
        [field("user_context", "x" * 2_001, source_refs=[])],
        "x" * 2_001,
        _digest("x" * 2_001),
    )
    assert blocked[0]["status"] == "blocked"
    assert blocked[0]["value"] is None

    sparse = _trusted_fields(
        "profile",
        [field("target_roles[2].title", "Backend")],
        INPUT,
        DIGEST,
    )
    assert sparse[0]["path"] == "target_roles[0].title"
    assert sparse[0]["status"] == "proposed"


def test_normalization_rejects_urls_before_context_resolution() -> None:
    for text in (
        "请读取 https://example.test/resume.pdf",
        "请读取 file:///tmp/resume.txt",
        "请读取 data:text/plain,secret",
        "请发送 mailto:person@example.test",
        "请打开 www.example.test/jobs",
        "请打开 example.test/jobs",
    ):
        with pytest.raises(InvalidStateError, match="URLs"):
            _normalize_text(text, "profile")


def test_decisions_are_canonical_and_patch_is_non_persistent() -> None:
    decisions = (
        ApplyDecisionInput("target_roles[0].title", "accept"),
        ApplyDecisionInput("include_keywords[0]", "edit", value="PostgreSQL"),
        ApplyDecisionInput("locations[0].radius_km", "reject"),
    )
    canonical = _canonical_decisions(decisions)
    assert [item["path"] for item in canonical] == [
        "include_keywords[0]",
        "locations[0].radius_km",
        "target_roles[0].title",
    ]
    assert _canonical_json(canonical).startswith("[")
    rejected = _canonical_decisions(
        (ApplyDecisionInput("target_roles[0].title", "reject", value="secret"),)
    )
    assert rejected[0]["value"] is None
    patch = _build_patch(
        "profile",
        [
            field("target_roles[0].title", "Backend").model_dump(mode="json"),
            field("include_keywords[0]", "Python").model_dump(mode="json"),
            field("locations[0].radius_km", 40, value_type="integer").model_dump(mode="json"),
        ],
        decisions,
    )
    assert patch == {
        "fields": {"include_keywords[0]": "PostgreSQL", "target_roles[0].title": "Backend"}
    }
    with pytest.raises(InvalidStateError):
        _canonical_decisions((decisions[0], decisions[0]))
    with pytest.raises(InvalidStateError):
        _build_patch("profile", [], (ApplyDecisionInput("unknown", "accept"),))
    with pytest.raises(InvalidStateError):
        _build_patch(
            "profile",
            [field("target_roles[0].title", "Backend").model_dump(mode="json")],
            (ApplyDecisionInput("target_roles[0].title", "accept", value="tampered"),),
        )
    assert _build_patch(
        "profile",
        [field("target_roles[0].title", "Backend", status="unknown").model_dump(mode="json")],
        (ApplyDecisionInput("target_roles[0].title", "unknown"),),
    ) == {"fields": {}}
    metadata = _decision_metadata(
        canonical,
        [
            field("target_roles[0].title", "Backend").model_dump(mode="json"),
            field("include_keywords[0]", "Python").model_dump(mode="json"),
            field("locations[0].radius_km", 40, value_type="integer").model_dump(mode="json"),
        ],
    )
    assert all("value" not in item for item in metadata)
    assert "PostgreSQL" not in _canonical_json(metadata)
    assert all(len(item["value_digest"]) == 64 for item in metadata)
    assert all(len(item["proposal_value_digest"]) == 64 for item in metadata)
    edited_metadata = next(item for item in metadata if item["path"] == "include_keywords[0]")
    assert edited_metadata["value_digest"] == _digest(_canonical_json("PostgreSQL"))
    assert edited_metadata["proposal_value_digest"] == _digest(_canonical_json("Python"))


def test_request_fingerprint_and_row_projection_never_need_raw_candidate_id() -> None:
    request = SmartIntakeRequest(
        target="profile",
        text=INPUT,
        idempotency_key="key-1",
        profile_version_id=uuid4(),
    )
    refs = {"profile_version_id": str(request.profile_version_id), "profile_rules_version": "v1"}
    fingerprint = _request_fingerprint(request, INPUT, refs)
    assert len(fingerprint) == 64
    assert _bounded("secret", 3) == "sec"
    row = {
        "id": uuid4(),
        "candidate_id": CANDIDATE,
        "target": "profile",
        "idempotency_key": "key-1",
        "state": "unavailable",
        "input_digest": DIGEST,
        "context_digest": _digest("context"),
        "fields": [],
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "model_id": "disabled",
        "prompt_version": "none",
        "context_refs": refs,
        "input_text": INPUT,
    }
    result = _preview_result(row)
    assert result.candidate_id == CANDIDATE
    assert _request_from_row(row).profile_version_id == request.profile_version_id
    assert not _is_expired(row)
    row["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    assert _is_expired(row)


def test_disabled_service_claims_finalizes_and_reuses_without_recharging(monkeypatch) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData(schema="careerops")
    preview_table = sa.Table(
        "smart_intake_previews",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("target", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("context_digest", sa.String(64), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("context_refs", sa.JSON(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("claim_state", sa.String(16), nullable=False),
        sa.Column("claim_token", sa.String(64), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("capability_state", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            default=lambda: datetime.now(UTC),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    decision_table = sa.Table(
        "smart_intake_decisions",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
    )
    with engine.begin() as conn:
        conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS careerops")
        preview_table.create(conn)
        decision_table.create(conn)
    monkeypatch.setattr(smart_intake_module, "smart_intake_previews", preview_table)
    monkeypatch.setattr(smart_intake_module, "smart_intake_decisions", decision_table)

    class ProfileRepository:
        def get_active_for(self, _candidate_id):
            return None

        def get_by_version_id(self, _candidate_id, _version_id):
            return None

    class Limiter:
        calls = 0

        def check(self, action: RateLimitAction, subject_hash: str, *, now: datetime) -> bool:
            del action, subject_hash, now
            self.calls += 1
            return True

    class Audit:
        def __init__(self, _connection):
            pass

        def append(self, _event):
            return None

    monkeypatch.setattr(smart_intake_module, "PostgresAuditWriter", Audit)
    limiter = Limiter()
    service = SmartIntakeService(
        engine,
        profile_repository=ProfileRepository(),
        job_repository=None,
        resume_repository=None,
        evidence_repository=None,
        model_client=None,
        rate_limiter=limiter,
    )
    monkeypatch.setattr(
        service,
        "_resolve_context_in_connection",
        lambda _conn, _candidate_id, _request: {
            "profile_version_id": None,
            "profile_rules_version": "none",
        },
    )
    request = SmartIntakeRequest(target="profile", text=INPUT, idempotency_key="sqlite-key")
    first = service.create_preview(CANDIDATE, request, actor_id="candidate")
    second = service.create_preview(CANDIDATE, request, actor_id="candidate")
    assert first.id == second.id
    assert first.state == "unavailable"
    assert limiter.calls == 1

    with pytest.raises(IdempotencyKeyReusedError):
        service.create_preview(
            CANDIDATE,
            SmartIntakeRequest(target="profile", text="different", idempotency_key="sqlite-key"),
            actor_id="candidate",
        )

    with engine.begin() as conn:
        conn.execute(
            sa.update(preview_table)
            .where(preview_table.c.id == first.id)
            .values(
                claim_state="pending",
                claim_expires_at=datetime.now(UTC) + timedelta(seconds=10),
            )
        )
    with pytest.raises(SmartPreviewInProgressError):
        service.create_preview(CANDIDATE, request, actor_id="candidate")

    with engine.begin() as conn:
        conn.execute(
            sa.update(preview_table)
            .where(preview_table.c.id == first.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(SmartPreviewExpiredError):
        service.create_preview(CANDIDATE, request, actor_id="candidate")


def test_prompts_make_model_boundary_and_fallback_copy_explicit() -> None:
    assert "untrusted" in _system_prompt("profile").lower()
    assert "untrusted" in _system_prompt("interview_context").lower()
    assert "allowed" in _user_prompt("profile")
    assert "user_context" in _user_prompt("interview_context")


def test_api_contract_rejects_cross_target_context_and_projects_response() -> None:
    profile = SmartIntakePreviewRequest(
        target="profile",
        input=SmartIntakeTextInput(kind="text", text="Backend"),
        idempotency_key="profile-1",
    )
    assert profile.input.text == "Backend"
    with pytest.raises(ValidationError):
        SmartIntakePreviewRequest(
            target="profile",
            input=SmartIntakeTextInput(kind="text", text="Backend"),
            idempotency_key="profile-2",
            interview_refs=SmartIntakeInterviewRefs(
                canonical_job_id=uuid4(),
                job_version_id=uuid4(),
                resume_version_id=uuid4(),
            ),
        )
    with pytest.raises(ValidationError):
        SmartIntakePreviewRequest(
            target="interview_context",
            input=SmartIntakeTextInput(kind="text", text="practice"),
            idempotency_key="interview-1",
        )
    profile_version_id = uuid4()
    interview = SmartIntakePreviewRequest(
        target="interview_context",
        input=SmartIntakeTextInput(kind="text", text="practice"),
        idempotency_key="interview-2",
        context_refs=SmartIntakeContextRefs(profile_version_id=profile_version_id),
        interview_refs=SmartIntakeInterviewRefs(
            canonical_job_id=uuid4(),
            job_version_id=uuid4(),
            resume_version_id=uuid4(),
            profile_version_id=profile_version_id,
        ),
    )
    assert interview.interview_refs is not None
    with pytest.raises(ValidationError):
        SmartIntakePreviewRequest(
            target="interview_context",
            input=SmartIntakeTextInput(kind="text", text="practice"),
            idempotency_key="interview-3",
            context_refs=SmartIntakeContextRefs(),
            interview_refs=SmartIntakeInterviewRefs(
                canonical_job_id=uuid4(),
                job_version_id=uuid4(),
                resume_version_id=uuid4(),
                profile_version_id=uuid4(),
            ),
        )
    apply = SmartIntakeApplyRequest(
        apply_idempotency_key="apply-1",
        context_digest="1" * 64,
        decision_set_hash="0" * 64,
        decisions=[],
    )
    assert apply.decisions == []
    with pytest.raises(ValidationError):
        SmartIntakeDecisionRequest(  # type: ignore[arg-type]
            path="user_context",
            decision="edit",
            value={"raw": "json"},  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        SmartIntakeDecisionRequest(  # type: ignore[arg-type]
            path="user_context",
            decision="edit",
            value=["nested"],  # type: ignore[arg-type]
        )

    from careerops.application.smart_intake import SmartPreviewResult

    projected = _to_response(
        SmartPreviewResult(
            id=uuid4(),
            candidate_id=CANDIDATE,
            target="profile",
            state="unavailable",
            input_digest=DIGEST,
            context_digest=_digest("ctx"),
            fields=[],
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
            model_id="disabled",
            prompt_version="none",
        )
    )
    assert projected.state == "unavailable"
    schema = SmartIntakePreviewResponse.model_json_schema()
    assert schema["additionalProperties"] is False
    assert "SmartIntakeDraftPatch" in schema["$defs"]
    assert schema["$defs"]["SmartIntakeDraftPatch"]["additionalProperties"] is False


def test_api_gate_fails_closed_when_wiring_or_capability_is_missing() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(DependencyNotReadyError):
        _service(request)  # type: ignore[arg-type]
    with pytest.raises(DependencyNotReadyError):
        _authorize(request, CANDIDATE, consume_rate_limit=False)  # type: ignore[arg-type]

    class Resolver:
        def __init__(self, released: bool) -> None:
            self.released = released

        def decide(self, _kind: object) -> SimpleNamespace:
            return SimpleNamespace(released=self.released)

    released_request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                capability_resolver=Resolver(True),
                smart_intake_rate_limiter=None,
            )
        )
    )
    with pytest.raises(DependencyNotReadyError):
        _authorize(released_request, CANDIDATE, consume_rate_limit=True)  # type: ignore[arg-type]

    denied_request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                capability_resolver=Resolver(False),
                smart_intake_rate_limiter=object(),
            )
        )
    )
    with pytest.raises(SmartIntakeDisabledError):
        _authorize(denied_request, CANDIDATE, consume_rate_limit=False)  # type: ignore[arg-type]


def test_retention_purge_uses_only_the_security_definer_function() -> None:
    class Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: object, params: object = None) -> None:
            del params
            self.statements.append(str(statement))

        def scalar(self, statement: object, params: object = None) -> int:
            del statement, params
            return 3

    class Transaction:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def __enter__(self) -> Connection:
            return self.connection

        def __exit__(self, *_args: object) -> None:
            return None

    class Engine:
        def __init__(self) -> None:
            self.connection = Connection()

        def begin(self) -> Transaction:
            return Transaction(self.connection)

    engine = Engine()
    assert retention_module.purge_once(engine, now=datetime.now(UTC)) == 3  # type: ignore[arg-type]
    assert engine.connection.statements == ["SET LOCAL ROLE careerops_retention"]


def test_retention_worker_retries_on_a_bounded_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    class Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(retention_module, "get_settings", lambda: object())
    monkeypatch.setattr(
        retention_module,
        "create_database_engine",
        lambda _settings, *, enforce_role: engine,
    )
    monkeypatch.setattr(retention_module, "purge_once", lambda _engine: 0)

    def stop_after_one_sweep(_seconds: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(retention_module.time, "sleep", stop_after_one_sweep)
    assert retention_module.main() == 0
    assert engine.disposed is True


def test_smart_intake_metrics_are_advisory_and_do_not_leak_values() -> None:
    calls: list[tuple[str, object]] = []

    class Metrics:
        def record_llm_tokens(self, **kwargs: object) -> None:
            calls.append(("tokens", kwargs))

        def record_smart_intake_preview(self, **kwargs: object) -> None:
            calls.append(("preview", kwargs))

        def observe_smart_intake_latency(self, **kwargs: object) -> None:
            calls.append(("latency", kwargs))

        def record_smart_intake_decision(self, **kwargs: object) -> None:
            calls.append(("decision", kwargs))

    service = SmartIntakeService(
        None,  # type: ignore[arg-type]
        profile_repository=None,
        job_repository=None,
        resume_repository=None,
        evidence_repository=None,
        model_client=None,
        rate_limiter=None,  # type: ignore[arg-type]
        metrics=Metrics(),
    )
    service._record_model_usage(SimpleNamespace(input_tokens=3, output_tokens=2))
    service._record_preview_metrics("profile", "ready", 0.25)
    service._record_decision_metric("profile", "edit")
    service._record_apply_latency("profile", 0.1)
    assert [name for name, _ in calls] == ["tokens", "preview", "latency", "decision", "latency"]

    class BrokenMetrics:
        def record_smart_intake_preview(self, **_kwargs: object) -> None:
            raise RuntimeError("metrics unavailable")

        def observe_smart_intake_latency(self, **_kwargs: object) -> None:
            raise RuntimeError("metrics unavailable")

    service._metrics = BrokenMetrics()
    service._record_preview_metrics("profile", "ready", 0.25)
