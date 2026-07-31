from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

from fastapi import Depends, FastAPI

from careerops import __version__
from careerops.api.auth_dependency import path_candidate_id
from careerops.api.errors import install_error_handlers
from careerops.api.metrics_middleware import MetricsMiddleware
from careerops.api.middleware import RequestIdMiddleware
from careerops.api.routes.agent_console import router as agent_console_router
from careerops.api.routes.agent_runs import router as agent_runs_router
from careerops.api.routes.application_workspace import router as application_workspace_router
from careerops.api.routes.applications import contacts_router
from careerops.api.routes.applications import router as applications_router
from careerops.api.routes.candidates import router as candidates_router
from careerops.api.routes.crawl_permissions import router as crawl_permissions_router
from careerops.api.routes.crawl_plans import router as crawl_plans_router
from careerops.api.routes.crawl_runs import router as crawl_runs_router
from careerops.api.routes.crawl_sources import router as crawl_sources_router
from careerops.api.routes.email_payloads import router as email_payloads_router
from careerops.api.routes.evidence import router as evidence_router
from careerops.api.routes.health import router as health_router
from careerops.api.routes.inbox import router as inbox_router
from careerops.api.routes.jobs import router as jobs_router
from careerops.api.routes.mail_intelligence import router as mail_intelligence_router
from careerops.api.routes.mail_sync import router as mail_sync_router
from careerops.api.routes.matching import router as matching_router
from careerops.api.routes.metrics import router as metrics_router
from careerops.api.routes.notifications import router as notifications_router
from careerops.api.routes.profile import router as profile_router
from careerops.api.routes.reply_drafts import router as reply_drafts_router
from careerops.api.routes.resumes import router as resumes_router
from careerops.api.routes.review import install_review_endpoint
from careerops.api.routes.smart_intake import router as smart_intake_router
from careerops.api.routes.system_send import router as system_send_router
from careerops.application.ports.readiness import ReadinessProbe
from careerops.config import RuntimeEnvironment, Settings, get_settings
from careerops.domain.applications import (
    ApplicationPackage,
    FollowUpReminder,
    ResumeVersion,
)
from careerops.domain.email_payloads import EmailAccountSummary
from careerops.infrastructure.database.postgres_application_repo import (
    PostgresApplicationRepository,
)
from careerops.infrastructure.memory_repos import InMemoryApplicationRepository
from careerops.infrastructure.redis import RedisAuthRateLimiter
from careerops.infrastructure.runtime import RuntimeResources
from careerops.observability import Metrics

# ---------------------------------------------------------------------------
# Protocol adapters for InMemoryApplicationRepository
# ---------------------------------------------------------------------------
# ApplicationService.__init__ expects four separate Protocol-typed repos,
# each with a ``save`` method.  InMemoryApplicationRepository stores
# everything in one class but renames the write methods to avoid
# signature collisions (save_resume, save_package, save_follow_up).
# These thin wrappers bridge the gap.


class _ResumeRepoAdapter:
    def __init__(self, repo: InMemoryApplicationRepository | PostgresApplicationRepository) -> None:
        self._repo = repo

    def find_latest_version(self, candidate_id: UUID) -> ResumeVersion | None:
        return self._repo.find_latest_version(candidate_id)

    def save(self, version: ResumeVersion) -> None:
        self._repo.save_resume(version)


class _PackageRepoAdapter:
    def __init__(self, repo: InMemoryApplicationRepository | PostgresApplicationRepository) -> None:
        self._repo = repo

    def find_by_application(self, application_id: UUID) -> ApplicationPackage | None:
        return self._repo.find_by_application(application_id)

    def save(self, package: ApplicationPackage) -> None:
        self._repo.save_package(package)


class _FollowUpRepoAdapter:
    def __init__(self, repo: InMemoryApplicationRepository | PostgresApplicationRepository) -> None:
        self._repo = repo

    def find_by_id(self, reminder_id: UUID) -> FollowUpReminder | None:
        return self._repo.find_follow_up_by_id(reminder_id)

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None:
        return self._repo.find_active_by_application_and_rule(application_id, rule_version)

    def save(self, reminder: FollowUpReminder) -> None:
        self._repo.save_follow_up(reminder)


async def _run_trigger_loop_bootstrap(probe: "RuntimeResources", settings: Settings):
    """Build the Phase 2 collaborators and run the schedule bootstrap once.

    Constructs a ``ScheduleManager`` from the runtime's Temporal client and a
    ``CrawlActivationService`` from the runtime's existing Tier 2 budget +
    source queue (both already satisfy the activation Protocols), then calls
    ``bootstrap_trigger_loop``. May raise (e.g. Temporal not up); the caller
    in ``lifespan`` treats it as best-effort with retry.
    """
    from careerops.application.crawl_activation import (
        CrawlActivationService,
        TemporalScheduleActivator,
    )
    from careerops.application.loop_bootstrap import bootstrap_trigger_loop
    from careerops.infrastructure.temporal.schedule_manager import ScheduleManager

    client = await probe.get_temporal_client()
    schedule_manager = ScheduleManager(client)
    activation_service = CrawlActivationService(
        schedule_activator=TemporalScheduleActivator(
            schedule_manager, task_queue="careerops-m0"
        ),
        budget_checker=probe.tier2_budget,
        eligibility_checker=probe.source_queue_service,
        inbox_projector=None,
    )
    return await bootstrap_trigger_loop(
        settings,
        probe,
        schedule_manager=schedule_manager,
        crawl_activation_service=activation_service,
    )


async def _bootstrap_trigger_loop_safe(
    probe: "RuntimeResources", settings: Settings, app: FastAPI
) -> None:
    """Run the Phase 2 schedule bootstrap; best-effort with retry.

    Retries the Temporal connect a few times (the compose startup race where
    Temporal is not yet healthy when the API boots), then logs and gives up
    without ever crashing the API. The next restart reconciles again.
    """
    import asyncio
    import logging

    log = logging.getLogger(__name__)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            report = await _run_trigger_loop_bootstrap(probe, settings)
            app.state.trigger_loop_report = report
            return
        except Exception as exc:  # noqa: BLE101 - temporal may not be up yet
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    log.warning("trigger-loop bootstrap skipped after retries: %s", last_exc)


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    # Create metrics first so it can be wired into the runtime's graph
    # (LLM token recording + apply_submitted counter).
    metrics = Metrics(version=__version__, settings=resolved)
    if readiness_probe is not None:
        probe = readiness_probe
    else:
        probe = RuntimeResources(resolved, metrics=metrics)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # Capability rollback is also a data-lifecycle boundary: once the
        # trusted flag is off, remove any unapplied preview values before the
        # process serves requests. Decision metadata remains append-only.
        if isinstance(probe, RuntimeResources) and not resolved.smart_intake_enabled:
            smart_intake_service = getattr(_app.state, "smart_intake_service", None)
            if smart_intake_service is not None:
                smart_intake_service.revoke_unapplied(actor_id="smart-intake-capability-disabled")
        # Phase 2: reconcile the self-driving trigger-loop schedules on startup.
        # Best-effort (never crashes the API; retried inside the helper).
        if isinstance(probe, RuntimeResources):
            await _bootstrap_trigger_loop_safe(probe, resolved, _app)
        try:
            yield
        finally:
            await probe.close()

    docs_url = None if resolved.environment is RuntimeEnvironment.PRODUCTION else "/docs"
    app = FastAPI(
        title="CareerOps API",
        version=__version__,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.readiness_probe = probe
    app.state.metrics = metrics
    app.state.career_loop_trace = metrics.trace

    # Wire API route repositories and services when using RuntimeResources.
    # Routes gracefully degrade to empty results when these are absent, but
    # wiring them here lets the REST endpoints return real data.
    if isinstance(probe, RuntimeResources):
        from careerops.application.applications import ApplicationService
        from careerops.application.candidate_service import CandidateService
        from careerops.application.contacts import ContactService
        from careerops.application.crawl_plan_service import (
            CrawlPlanService,
            CrawlRunService,
            CrawlSourceService,
        )
        from careerops.application.evidence_service import (
            EvidenceService,
            LoggingEvidenceAuditSink,
        )
        from careerops.application.matching import (
            EvidenceImportService,
        )
        from careerops.application.profile_service import ProfileService
        from careerops.application.resume_service import ResumeService
        from careerops.application.smart_intake import SmartIntakeService
        from careerops.infrastructure.database.postgres_candidate_repo import (
            PostgresCandidateRepository,
        )

        # auth-rm Task 2: global candidate CRUD surface (no candidate path
        # prefix, no per-candidate auth scoping — the login + multi-candidate
        # path params are being removed). The route reads this off
        # ``app.state.candidate_service`` and returns 503 if it is absent.
        app.state.candidate_service = CandidateService(
            PostgresCandidateRepository(probe.database)
        )

        app.state.matching_repository = probe.matching_read_repo
        app.state.evidence_import_service = EvidenceImportService(probe.matching_read_repo)
        app.state.match_orchestrator = probe.match_orchestrator
        app.state.job_read_repository = probe.job_read_repo
        app.state.contact_repository = probe.contact_repo
        app.state.contact_service = ContactService(probe.contact_repo)
        app.state.application_repository = probe.application_repo
        app.state.application_service = ApplicationService(
            application_repo=probe.application_repo,
            resume_repo=_ResumeRepoAdapter(probe.application_repo),
            package_repo=_PackageRepoAdapter(probe.application_repo),
            follow_up_repo=_FollowUpRepoAdapter(probe.application_repo),
        )
        # Section-2 server-side-candidate-owned repos + shared capability
        # resolver (end-to-end-career-application-loop tasks 2.2/2.5/2.6/2.11).
        # These are exposed on app.state so Section-3+ routes can pull them via
        # ``require_repository``; the helper raises DependencyNotReadyError
        # (503) if a repo is ever absent rather than silently degrading. The
        # capability resolver backs both the LangGraph review stack and the
        # ``require_capability`` external-effect gate.
        app.state.profile_repository = probe.profile_repo
        app.state.evidence_repository = probe.evidence_repo
        app.state.application_cycle_repository = probe.application_cycle_repo
        app.state.capability_resolver = probe.capability_resolver
        app.state.smart_intake_rate_limiter = RedisAuthRateLimiter(probe.redis_sync)
        app.state.smart_intake_service = SmartIntakeService(
            probe.database,
            profile_repository=probe.profile_repo,
            job_repository=probe.job_read_repo,
            resume_repository=probe.application_repo,
            evidence_repository=probe.evidence_repo,
            model_client=probe.model_client,  # type: ignore[arg-type]
            rate_limiter=app.state.smart_intake_rate_limiter,
            metrics=metrics,
        )
        # Section-3 application services (tasks 3.1 / 3.3 / 3.6). Each wraps a
        # Section-2 repository; routes pull them through ``require_repository``
        # so a missing service surfaces as 503 rather than a silent empty
        # response. The resume service reuses the content-addressed store on
        # the runtime (``probe.storage``) and the per-candidate byte cap from
        # settings. The evidence service gets a structured-log audit sink by
        # default; a durable Postgres sink can be wired in a later stage
        # without changing the service contract.
        app.state.profile_service = ProfileService(probe.profile_repo)
        app.state.resume_service = ResumeService(
            probe.application_repo,
            probe.evidence_repo,
            probe.storage,
            max_bytes=resolved.storage_max_object_bytes,
        )
        app.state.evidence_service = EvidenceService(
            probe.evidence_repo, audit_sink=LoggingEvidenceAuditSink()
        )
        # Section-4 crawl services (tasks 4.4-4.7). Each wraps a Section-4
        # repository; routes pull them through ``require_repository`` so a
        # missing service surfaces as 503. The run service composes all three
        # repos so it can resolve the eligible source set + active plan version
        # when recording a manual run. Capability gating for
        # CRAWL_PLAN_MANAGEMENT is applied at each router; the resolver is
        # already on app.state.
        crawl_source_service = CrawlSourceService(probe.crawl_source_repo)
        crawl_plan_service = CrawlPlanService(probe.crawl_plan_repo)
        crawl_run_service = CrawlRunService(
            probe.crawl_run_repo,
            plan_repository=probe.crawl_plan_repo,
            source_repository=probe.crawl_source_repo,
        )
        app.state.crawl_source_service = crawl_source_service
        app.state.crawl_plan_service = crawl_plan_service
        app.state.crawl_run_service = crawl_run_service
        # Phase 6.2: source-specific crawl-permission service.  Composes the
        # permission repo (Phase 5.2) + source repo (for pausing) + audit sink.
        app.state.crawl_permission_service = probe.crawl_permission_service
        # Section 5 crawl execution service (tasks 5.1, 5.5, 5.6). Wired
        # through RuntimeResources; reachable from API routes and Temporal
        # activities via app.state.crawl_execution_service.
        app.state.crawl_execution_service = probe.crawl_execution_service
        # Section-6 inbox projection service + repository (tasks 6.1-6.6).
        # Connects the job projection to the active profile + crawl-plan
        # provenance. Same DI pattern as Section-2/4/5 services.
        app.state.inbox_repository = probe.inbox_repo
        app.state.inbox_service = probe.inbox_service
        app.state.agent_run_repository = probe.agent_run_repo
        app.state.agent_runtime = probe.agent_runtime

        # Phase 9: notification service (SSE + outbox).  The action repo is
        # wired from the probe so ActionProjectionBuilder can read persisted
        # actions instead of returning an empty stub queue.
        from careerops.agent_console.action_projection import (
            ActionProjectionBuilder,
        )
        from careerops.application.notification_service import (
            NotificationService,
            SSEChannel,
        )

        sse_channel = SSEChannel()
        app.state.notification_service = NotificationService(
            sse_channel,
            repository=getattr(probe, "notification_repo", None),
        )
        app.state.sse_channel = sse_channel

        # Wire action repo from probe for real action queue projection.
        action_repo = getattr(probe, "agent_action_repo", None)
        if action_repo is not None:
            app.state.agent_console_action_repo = action_repo
            app.state.action_projection = ActionProjectionBuilder(
                agent_action_repo=action_repo,
            )
        app.state.resume_review_service = probe.resume_review_service
        app.state.interview_preparation_service = probe.interview_preparation_service
        # Section-7 application workspace service (tasks 7.2-7.7). Composes the
        # existing application repo (doubles as resume/package/follow-up repo)
        # with the cycle repo and the default job-evidence channel resolver.
        from careerops.application.application_workspace import (
            ApplicationWorkspaceService,
            PackageServiceBindingStore,
        )
        from careerops.application.email_payload_service import (
            AccountLookupError,
            EmailPayloadService,
            RepositoryTrustedContactResolver,
        )

        # Section-8 package service (tasks 8.2-8.7). The application repo
        # doubles as both the package-version repo (find_latest_package_version
        # / save_package_version / ...) and the resume-read repo
        # (find_resume_by_id), so a single instance satisfies both ports.
        # Tailoring stays default-disabled via the shared capability resolver
        # until MODEL_TAILORING is released.
        from careerops.application.package_service import PackageService

        package_service = PackageService(
            probe.application_repo,  # type: ignore[arg-type]
            probe.application_repo,  # type: ignore[arg-type]
            capability_resolver=probe.capability_resolver,
            evidence_repo=probe.evidence_repo,
            job_version_repo=probe.job_read_repo,
            trace=metrics.trace,
        )
        app.state.package_service = package_service

        # Section 9 trusted-contact/account adapters.  Both are candidate/job
        # scoped and fail closed: repository misses produce no eligible
        # recipient/account rather than a guessed fallback.
        trusted_contact_resolver = RepositoryTrustedContactResolver(
            probe.contact_repo,
            probe.job_read_repo,
        )

        def _has_trusted_contact(candidate_id: UUID, canonical_job_id: UUID) -> bool:
            application = probe.application_repo.find_by_candidate_and_job(
                candidate_id, canonical_job_id
            )
            if application is None:
                return False
            return bool(
                trusted_contact_resolver.resolve(
                    candidate_id=candidate_id,
                    application_id=application.id,
                    canonical_job_id=canonical_job_id,
                )
            )

        app.state.application_workspace_service = ApplicationWorkspaceService(
            probe.application_repo,
            cycle_repo=probe.application_cycle_repo,
            package_binding_store=PackageServiceBindingStore(package_service),
            contact_lookup=_has_trusted_contact,
        )

        class _CandidateAccountLookup:
            def __init__(self, repository: Any) -> None:
                self._repository = repository

            def lookup_for_candidate(self, candidate_id: UUID, account_id: UUID) -> Any:
                account = self._repository.get_account(candidate_id, account_id)
                if account is None:
                    raise AccountLookupError(f"account {account_id} not found")
                return EmailAccountSummary(
                    account_id=account.id,
                    email_address=account.email_address,
                    status=account.status.value,
                )

            def lookup(self, account_id: UUID) -> Any:
                raise AccountLookupError(
                    f"candidate-scoped account lookup required for {account_id}"
                )

        workspace_service = app.state.application_workspace_service
        app.state.email_payload_service = EmailPayloadService(
            probe.application_repo,  # type: ignore[arg-type]
            workspace_service,  # type: ignore[arg-type]
            contact_resolver=trusted_contact_resolver,
            account_lookup=_CandidateAccountLookup(probe.mail_account_repo),  # type: ignore[arg-type]
        )
        # Section-10 system-managed send service (tasks 10.1-10.7). Composes
        # the shared side-effect kernel (built in RuntimeResources for the
        # non-production review stack) with the application repo, package
        # reader and shared capability resolver. The kernel stays absent in
        # PRODUCTION until the external-write qualification gate flips in a
        # separate change, so this service is only wired when the kernel is
        # present; the route's ``require_repository`` turns its absence into a
        # 503 (dependency-not-ready) rather than a silent degradation. The
        # provider is the FakeSideEffectProvider only — real Gmail activation
        # is a separate future qualification change (task 17.6).
        if probe.side_effect_kernel is not None:
            from careerops.application.system_managed_send import (
                SystemManagedSendService,
            )
            from careerops.infrastructure.database.outbox import PostgresOutboxStore

            email_payload_service = app.state.email_payload_service

            def _recipient_eligible(request: Any) -> bool:
                application = probe.application_repo.find_by_id(request.application_id)
                if (
                    application is None
                    or application.candidate_id != request.candidate_id
                    or (
                        request.canonical_job_id is not None
                        and request.canonical_job_id != application.canonical_job_id
                    )
                ):
                    return False
                verdict = email_payload_service.resolve_trusted_contact(
                    candidate_id=request.candidate_id,
                    application_id=request.application_id,
                    canonical_job_id=application.canonical_job_id,
                    recipient_email=request.recipient,
                )
                return verdict.eligible and (
                    verdict.email.strip().lower() == request.recipient.strip().lower()
                )

            def _account_active(request: Any) -> bool:
                if request.account_id is None:
                    return False
                account = probe.mail_account_repo.get_account(
                    request.candidate_id, request.account_id
                )
                return bool(
                    account is not None
                    and account.status.value == "active"
                    and account.email_address.strip().lower()
                    == request.account_email.strip().lower()
                )

            app.state.system_managed_send_service = SystemManagedSendService(
                probe.side_effect_kernel,  # type: ignore[arg-type]
                probe.application_repo,
                package_reader=package_service,
                capability_resolver=probe.capability_resolver,
                outbox_store=PostgresOutboxStore(probe.database),  # type: ignore[arg-type]
                account_status_lookup=_account_active,
                recipient_eligible=_recipient_eligible,
                trace=metrics.trace,
            )
        # Section-12 mail intelligence service (tasks 12.5-12.7). Composes the
        # durable proposal repo + minimized message reader with the workspace
        # service (ownership + USER-sourced transition delegation) and the
        # shared capability resolver. The proposal NEVER writes
        # ApplicationState directly — acceptance delegates to the workspace's
        # ``apply_user_transition`` via a thin sink (Iron Rule 2). No live OAuth
        # / external-write / auto-send flag is enabled here (task 17.6); the
        # message reader consumes already-ingested rows (fixtures in the slice).
        from careerops.application.mail_intelligence_service import (
            MailIntelligenceService,
        )

        mail_service = MailIntelligenceService(
            message_repo=probe.mail_message_repo,  # type: ignore[arg-type]
            proposal_repo=probe.mail_proposal_repo,  # type: ignore[arg-type]
            ownership_reader=workspace_service,  # type: ignore[arg-type]
            timeline_sink=workspace_service,  # type: ignore[arg-type]
            follow_up_scheduler=None,  # Section 13 wires concrete follow-up rules
            capability_resolver=probe.capability_resolver,
            trace=metrics.trace,
        )
        # Delegate the USER-sourced transition to the workspace service so the
        # proposal never owns the ApplicationRepository. The bound method
        # records a USER-sourced ApplicationEvent for every accepted proposal.
        mail_service.set_transition_sink(workspace_service.apply_user_transition)  # type: ignore[arg-type]
        app.state.mail_intelligence_service = mail_service
        # Section-11 Gmail read-sync service (tasks 11.1-11.7). Composes the
        # dedicated-account connection repo + durable sync-run/cursor repo +
        # thread-link repo. The GMAIL_READ capability stays DENIED at the
        # contract layer (Iron Rule 7); the router gates on it so the path is
        # default-deny until a separate qualification change releases it. No
        # live OAuth / external-write flag is enabled here (task 17.6); the
        # service builds the read path and the pending-action invalidator stays
        # None until the outbox wires a concrete invalidator.
        from careerops.application.mail_sync_service import MailSyncService

        app.state.mail_sync_service = MailSyncService(
            probe.mail_account_repo,  # type: ignore[arg-type]
            probe.mail_sync_run_repo,  # type: ignore[arg-type]
            probe.mail_thread_link_repo,  # type: ignore[arg-type]
        )
        app.state.mail_account_repository = probe.mail_account_repo
        app.state.mail_sync_run_repository = probe.mail_sync_run_repo
        app.state.mail_thread_link_repository = probe.mail_thread_link_repo
        # Section-13 reply-draft + follow-up services (tasks 13.1-13.6). The
        # follow-up service composes the M3 follow-up repo (the application
        # repo doubles as it) + application repo for ownership/timeline. The
        # reply-draft service composes a durable draft repo + the workspace
        # ownership reader + the shared capability resolver. The send port
        # stays None (send chain not wired) until the external-write
        # qualification gate flips in a separate change (task 17.6); the route
        # surfaces a 503/403 rather than silently no-op'ing. Auto-send is
        # permanently denied via the AUTO_SEND capability; high-risk categories
        # are permanently denied system send at the service layer. No live
        # OAuth / external-write flag is enabled here.
        from careerops.application.reply_draft_service import (
            FollowUpService,
            ReplyDraftService,
        )
        from careerops.infrastructure.database.postgres_reply_draft_repo import (
            PostgresReplyDraftRepository,
        )

        reply_draft_repo = PostgresReplyDraftRepository(probe.database)
        app.state.reply_draft_repository = reply_draft_repo
        follow_up_service = FollowUpService(
            _FollowUpRepoAdapter(probe.application_repo),  # type: ignore[arg-type]
            probe.application_repo,
            timeline_sink=workspace_service,  # type: ignore[arg-type]
            trace=metrics.trace,
        )
        app.state.follow_up_service = follow_up_service
        app.state.reply_draft_service = ReplyDraftService(
            reply_draft_repo,  # type: ignore[arg-type]
            workspace_service,  # type: ignore[arg-type]
            timeline_sink=workspace_service,  # type: ignore[arg-type]
            capability_resolver=probe.capability_resolver,
            send_port=None,  # Section 10 chain reuse wired at qualification
        )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(metrics_router)
    # auth-rm: global candidate CRUD routes (list / create / detail). The
    # console login is removed, so no router carries a session/CSRF auth
    # dependency; the candidate surface fails closed (503) via ``_service``
    # when the service is not wired.
    app.include_router(candidates_router)

    # All routers are mounted WITHOUT the former ``require_api_auth`` guard —
    # the console login / session / CSRF layer was removed (auth-rm Task 9).
    # Per-candidate identity comes from the ``{candidate_id}`` path parameter;
    # global routes (jobs, matches, contacts, candidates) are public API.
    app.include_router(jobs_router)
    app.include_router(matching_router)
    # Per-candidate routers carry ``path_candidate_id`` (auth-rm I1): an
    # unknown ``candidate_id`` in the URL yields 404 instead of silently
    # returning empty lists (or hitting FK IntegrityError -> 500 on writes).
    app.include_router(
        applications_router, dependencies=[Depends(path_candidate_id)]
    )
    # Global recruiting-contact catalog (company-scoped, not per-candidate).
    app.include_router(contacts_router)
    # Section-3 additive routers (profile / resumes / evidence). Candidate
    # identity comes from the URL path parameter.
    app.include_router(profile_router, dependencies=[Depends(path_candidate_id)])
    app.include_router(resumes_router, dependencies=[Depends(path_candidate_id)])
    app.include_router(smart_intake_router, dependencies=[Depends(path_candidate_id)])
    app.include_router(evidence_router, dependencies=[Depends(path_candidate_id)])
    # Section-4 additive routers (crawl sources / plans / runs). Candidate
    # identity comes from the URL path parameter; the CRAWL_PLAN_MANAGEMENT
    # capability gate is composed inside each router.
    app.include_router(crawl_sources_router, dependencies=[Depends(path_candidate_id)])
    app.include_router(crawl_plans_router, dependencies=[Depends(path_candidate_id)])
    app.include_router(crawl_runs_router, dependencies=[Depends(path_candidate_id)])
    app.include_router(crawl_permissions_router, dependencies=[Depends(path_candidate_id)])
    # Section-6 inbox router (tasks 6.7-6.8). Per-candidate path-param router.
    app.include_router(inbox_router, dependencies=[Depends(path_candidate_id)])
    app.include_router(agent_runs_router, dependencies=[Depends(path_candidate_id)])
    app.include_router(agent_console_router, dependencies=[Depends(path_candidate_id)])
    # Phase 9: notification routes (SSE stream + recovery). Per-candidate
    # path-param router.
    app.include_router(notifications_router, dependencies=[Depends(path_candidate_id)])
    # Section-7 application-workspace router (tasks 7.8). Additive paths only
    # (detail / prepare / channels / channel / package / timeline /
    # confirm-external-submission / state); candidate ownership resolved from
    # the path.
    app.include_router(
        application_workspace_router, dependencies=[Depends(path_candidate_id)]
    )
    # Section-9 email-payload router (tasks 9.1, 9.6). Additive paths only
    # (recruiting-contacts list + submission-preview). Performs NO provider
    # side effects (the actual send is Section 10); responses carry
    # Cache-Control: no-store.
    app.include_router(email_payloads_router, dependencies=[Depends(path_candidate_id)])
    # Section-10 system-managed-send router (tasks 10.1-10.8, 10.11). Additive
    # paths only (confirm / status / reconcile); gated on the
    # SYSTEM_MANAGED_SEND capability which stays DENIED at the contract layer.
    app.include_router(system_send_router, dependencies=[Depends(path_candidate_id)])
    # Section-12 mail-intelligence router (tasks 12.5-12.6). Per-candidate
    # path-param router; additive paths only (extract/proposal, list, get,
    # accept, reject). Proposals are review-only; application state changes
    # ONLY through the USER-sourced transition path on acceptance (Iron Rule
    # 2); responses carry Cache-Control: no-store.
    app.include_router(mail_intelligence_router, dependencies=[Depends(path_candidate_id)])
    # Section-11 Gmail read-sync router (tasks 11.7, 11.10). Per-candidate
    # path-param router; additive paths only. Gated on the GMAIL_READ
    # capability, which stays DENIED at the contract layer until a separate
    # qualification change releases it (Iron Rule 7); responses carry
    # Cache-Control: no-store and bounded cursor pagination.
    app.include_router(mail_sync_router, dependencies=[Depends(path_candidate_id)])
    # Section-13 reply-draft + follow-up router (tasks 13.7-13.8, 13.10).
    # Additive paths only. Drafts are review-only; high-risk categories are
    # permanently denied system send; auto-send is permanently denied;
    # responses carry Cache-Control: no-store and bounded cursor pagination.
    app.include_router(reply_drafts_router, dependencies=[Depends(path_candidate_id)])

    # Review endpoint (plan v0.4 §2.7 / §3 Stage 3): the human fallback for
    # A/B-escalated approvals. Mounted when the runtime actually compiled the
    # graph, non-PRODUCTION only. The console login is gone (auth-rm Task 9),
    # so the endpoint trusts the loopback reviewer: decisions are recorded with
    # the fixed actor "local-reviewer" (actor_type=USER) under the per-actor
    # Redis rate limit, and the kernel still enforces the approval owner
    # binding at decision time.
    if (
        resolved.environment is not RuntimeEnvironment.PRODUCTION
        and isinstance(probe, RuntimeResources)
        and probe.career_graph is not None
        and probe.review_mapping is not None
        and probe.side_effect_kernel is not None
    ):
        install_review_endpoint(
            app,
            rate_limiter=RedisAuthRateLimiter(probe.redis_sync),
            review_mapping=probe.review_mapping,
            career_graph=probe.career_graph,
            side_effect_kernel=probe.side_effect_kernel,
        )
    # -----------------------------------------------------------------------
    # OpenAPI: declare standard error responses on every path
    # -----------------------------------------------------------------------
    _install_openapi_error_responses(app)

    return app


# ---------------------------------------------------------------------------
# OpenAPI customisation
# ---------------------------------------------------------------------------

_ERROR_RESPONSE_SCHEMAS: dict[str, dict[str, object]] = {
    "401": {
        "description": "Unauthorized — missing or invalid credentials",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "examples": {
                    "unauthorized": {
                        "summary": "UNAUTHORIZED",
                        "value": {
                            "error": {
                                "code": "UNAUTHORIZED",
                                "message": "Authentication required",
                                "retryable": False,
                                "details": None,
                                "trace_id": "abc123",
                            }
                        },
                    },
                    "invalid_credentials": {
                        "summary": "INVALID_CREDENTIALS",
                        "value": {
                            "error": {
                                "code": "INVALID_CREDENTIALS",
                                "message": "Invalid credentials",
                                "retryable": False,
                                "details": None,
                                "trace_id": "abc123",
                            }
                        },
                    },
                },
            }
        },
    },
    "403": {
        "description": "Forbidden — CSRF, bootstrap, profile requirement, or policy denial",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "examples": {
                    "csrf_rejected": {
                        "summary": "CSRF_REJECTED",
                        "value": {
                            "error": {
                                "code": "CSRF_REJECTED",
                                "message": "CSRF token rejected",
                                "retryable": False,
                                "details": None,
                                "trace_id": "abc123",
                            }
                        },
                    },
                    "bootstrap_closed": {
                        "summary": "BOOTSTRAP_CLOSED",
                        "value": {
                            "error": {
                                "code": "BOOTSTRAP_CLOSED",
                                "message": "Bootstrap is no longer available",
                                "retryable": False,
                                "details": None,
                                "trace_id": "abc123",
                            }
                        },
                    },
                    "candidate_profile_required": {
                        "summary": "CANDIDATE_PROFILE_REQUIRED",
                        "value": {
                            "error": {
                                "code": "CANDIDATE_PROFILE_REQUIRED",
                                "message": "Candidate profile is required",
                                "retryable": False,
                                "details": None,
                                "trace_id": "abc123",
                            }
                        },
                    },
                    "denied_policy": {
                        "summary": "DENIED_POLICY",
                        "value": {
                            "error": {
                                "code": "DENIED_POLICY",
                                "message": "Action denied by policy",
                                "retryable": False,
                                "details": None,
                                "trace_id": "abc123",
                            }
                        },
                        "smart_intake_disabled": {
                            "summary": "SMART_INTAKE_DISABLED",
                            "value": {
                                "error": {
                                    "code": "SMART_INTAKE_DISABLED",
                                    "message": "Smart intake is disabled",
                                    "retryable": False,
                                    "details": None,
                                    "trace_id": "abc123",
                                }
                            },
                        },
                    },
                },
            }
        },
    },
    "404": {
        "description": "Resource not found",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "example": {
                    "error": {
                        "code": "NOT_FOUND",
                        "message": "Resource not found",
                        "retryable": False,
                        "details": None,
                        "trace_id": "abc123",
                    }
                },
            }
        },
    },
    "410": {
        "description": "Preview expired or was purged",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "example": {
                    "error": {
                        "code": "SMART_PREVIEW_EXPIRED",
                        "message": "Smart intake preview has expired",
                        "retryable": False,
                        "details": None,
                        "trace_id": "abc123",
                    }
                },
            }
        },
    },
    "409": {
        "description": "Conflict — resource already exists or state conflict",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "example": {
                    "error": {
                        "code": "CONFLICT",
                        "message": "Resource conflict",
                        "retryable": False,
                        "details": None,
                        "trace_id": "abc123",
                    }
                },
            }
        },
    },
    "422": {
        "description": "Validation error — request body failed schema validation",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "example": {
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "Request validation failed",
                        "retryable": False,
                        "details": {"issues": []},
                        "trace_id": "abc123",
                    }
                },
            }
        },
    },
    "429": {
        "description": "Rate limited — too many requests (retryable)",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "example": {
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "Too many requests",
                        "retryable": True,
                        "details": None,
                        "trace_id": "abc123",
                    }
                },
            }
        },
    },
    "503": {
        "description": "Service unavailable — dependency not ready (retryable)",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "example": {
                    "error": {
                        "code": "DEPENDENCY_NOT_READY",
                        "message": "Service dependency is not ready",
                        "retryable": True,
                        "details": None,
                        "trace_id": "abc123",
                    }
                },
            }
        },
    },
}


def _install_openapi_error_responses(app: FastAPI) -> None:
    """Inject standard error response declarations into every OpenAPI path."""
    original_openapi = app.openapi

    def custom_openapi() -> dict[str, object]:
        schema = original_openapi()
        if "openapi_error_responses_installed" in schema:
            return schema

        # Ensure ErrorResponse schema exists in components
        components = schema.setdefault("components", {})  # type: ignore[arg-type]
        schemas = components.setdefault("schemas", {})  # type: ignore[arg-type]
        if "ErrorResponse" not in schemas:
            schemas["ErrorResponse"] = {
                "type": "object",
                "required": ["error"],
                "properties": {
                    "error": {
                        "type": "object",
                        "required": ["code", "message", "retryable", "trace_id"],
                        "properties": {
                            "code": {
                                "type": "string",
                                "enum": [
                                    "UNAUTHORIZED",
                                    "CSRF_REJECTED",
                                    "INVALID_CREDENTIALS",
                                    "RATE_LIMITED",
                                    "BOOTSTRAP_CLOSED",
                                    "CANDIDATE_PROFILE_REQUIRED",
                                    "NOT_FOUND",
                                    "CONFLICT",
                                    "DEPENDENCY_NOT_READY",
                                    "VALIDATION_ERROR",
                                    "BAD_REQUEST",
                                    "FORBIDDEN",
                                    "METHOD_NOT_ALLOWED",
                                    "INTERNAL_ERROR",
                                    "INVALID_STATE",
                                    "STALE_PAYLOAD",
                                    "UNAVAILABLE_DEPENDENCY",
                                    "DENIED_POLICY",
                                    "UNRESOLVED_EMAIL_LINK",
                                    "RECONCILIATION_REQUIRED",
                                    "PAYLOAD_TOO_LARGE",
                                    "SMART_INTAKE_DISABLED",
                                    "SMART_PREVIEW_NOT_FOUND",
                                    "SMART_PREVIEW_IN_PROGRESS",
                                    "IDEMPOTENCY_KEY_REUSED",
                                    "STALE_SMART_INTAKE_PREVIEW",
                                    "SMART_PREVIEW_EXPIRED",
                                ],
                            },
                            "message": {"type": "string"},
                            "retryable": {"type": "boolean", "default": False},
                            "details": {},
                            "trace_id": {"type": "string"},
                        },
                    }
                },
            }

        # Inject error responses into every path/operation
        paths = cast("dict[str, dict[str, dict[str, Any]]]", schema.get("paths", {}))
        for _path, methods in paths.items():
            for method, operation in methods.items():
                if method in ("parameters", "summary", "description", "servers"):
                    continue
                responses = operation.setdefault("responses", {})
                for status, response_def in _ERROR_RESPONSE_SCHEMAS.items():
                    if status not in responses:
                        responses[status] = response_def  # type: ignore[assignment]

        schema["openapi_error_responses_installed"] = True  # type: ignore[assignment]
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


app = create_app()
