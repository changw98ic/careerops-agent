# langgraph ships without bundled pyright stubs.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false
from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import Engine, text
from temporalio.client import Client

from careerops.application.ports.readiness import (
    ReadinessReport,
    ReadinessState,
)
from careerops.config import DeploymentMode, RuntimeEnvironment, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.storage.local import LocalContentAddressedStorage
from careerops.integrations.fake_side_effect_provider import SideEffectProvider
from careerops.observability.metrics import Metrics
from careerops.orchestration.mapping_store import ReviewMappingStore

_COMPONENTS = ("database", "redis", "temporal", "storage")


def _langgraph_conn_string(raw_url: str) -> str:
    """Return a psycopg URL pinned to the dedicated LangGraph schema."""

    conn_string = raw_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(conn_string)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["options"] = "-csearch_path=langgraph,public"
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


class AsyncRedisClient(Protocol):
    async def ping(self) -> bool: ...

    async def aclose(self, close_connection_pool: bool | None = None) -> None: ...


class SyncRedisClient(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...

    def close(self) -> None: ...


class _AttachmentPathResolver:
    """Adapter exposing ``LocalContentAddressedStorage.path_for_digest`` as the
    ``AttachmentPathResolver.get(content_hash)`` protocol the Gmail send
    provider expects (content hash -> on-disk file path)."""

    def __init__(self, storage: LocalContentAddressedStorage) -> None:
        self._storage = storage

    def get(self, content_hash: str) -> str | Path | None:
        return self._storage.path_for_digest(content_hash)


class RuntimeResources:
    """Process-owned clients and bounded, non-sensitive readiness probes."""

    def __init__(self, settings: Settings, *, metrics: Metrics | None = None) -> None:
        self._settings = settings
        self._metrics = metrics
        is_prod = settings.environment is RuntimeEnvironment.PRODUCTION
        enforce_role = is_prod or settings.deployment_mode is DeploymentMode.COMPOSE_LOOPBACK
        self.database: Engine = create_database_engine(settings, enforce_role=enforce_role)
        self.redis = cast(
            "AsyncRedisClient",
            AsyncRedis.from_url(  # pyright: ignore[reportUnknownMemberType]
                settings.redis_url.get_secret_value(),
                decode_responses=False,
                socket_connect_timeout=settings.readiness_timeout_seconds,
                socket_timeout=settings.readiness_timeout_seconds,
            ),
        )
        self.redis_sync = cast(
            "SyncRedisClient",
            SyncRedis.from_url(  # pyright: ignore[reportUnknownMemberType]
                settings.redis_url.get_secret_value(),
                decode_responses=False,
                socket_connect_timeout=settings.readiness_timeout_seconds,
                socket_timeout=settings.readiness_timeout_seconds,
            ),
        )
        self.storage = LocalContentAddressedStorage(
            settings.storage_root,
            max_object_bytes=settings.storage_max_object_bytes,
        )
        self._temporal: Client | None = None
        self._temporal_lock = asyncio.Lock()
        self._closed = False
        # Phase-0 capability resolver (end-to-end-career-application-loop
        # Decision 11). A single shared instance backs BOTH the LangGraph
        # review stack and the new Section-2 external-effect projections. It
        # reads trusted flag state from ``Settings`` and is fail-closed for
        # any capability it has not been explicitly taught.
        from careerops.orchestration.capability_resolver import (
            SettingsCapabilityResolver,
        )

        self.capability_resolver = SettingsCapabilityResolver(settings)
        # LangGraph review stack (plan v0.4 §2.4 / §3 Stage 3). v1 is single-
        # process (MemorySaver + InMemorySideEffectStore + InMemoryReviewMapping
        # Store share this process's lifetime). PRODUCTION is fail-closed: the
        # in-memory stack is NEVER built there, so the review endpoint cannot
        # silently degrade durable state. The attributes stay ``None`` in
        # PRODUCTION and ``app.py`` declines to mount the review router.
        self.career_graph: object | None = None
        self.review_mapping: ReviewMappingStore | None = None
        self.side_effect_kernel: object | None = None
        self.model_client: object | None = None
        self.gmail_token_store: object | None = None
        self._build_career_graph_stack()
        # Production graph construction can stop early when the optional
        # LangGraph checkpointer is unavailable. Agent routes still need the
        # shared model capability boundary, so build the provider client
        # independently; the factory remains disabled by default.
        if self.model_client is None:
            from careerops.model_gateway.factory import create_model_client

            self.model_client = create_model_client(
                settings.model_provider,
                base_url=settings.model_base_url,
                api_key=settings.model_api_key.get_secret_value(),
                model=settings.model_name,
                usage_recorder=metrics,
            )

        # Read repositories and application services for API routes.
        # Always use Postgres-backed repos when a database URL is configured.
        # InMemory imports are kept for tests that mock the database.
        from careerops.infrastructure.database.postgres_application_cycle_repo import (
            PostgresApplicationCycleRepository,
        )
        from careerops.infrastructure.database.postgres_application_repo import (
            PostgresApplicationRepository,
        )
        from careerops.infrastructure.database.postgres_contact_repo import (
            PostgresContactRepository,
        )
        from careerops.infrastructure.database.postgres_crawl_repo import (
            PostgresCrawlAttemptRepository,
            PostgresCrawlPermissionRepository,
            PostgresCrawlPlanRepository,
            PostgresCrawlRunRepository,
            PostgresCrawlSourceRepository,
        )
        from careerops.infrastructure.database.postgres_evidence_repo import (
            PostgresEvidenceRepository,
        )
        from careerops.infrastructure.database.postgres_job_repo import (
            PostgresJobReadRepository,
        )
        from careerops.infrastructure.database.postgres_matching_repo import (
            PostgresMatchingReadRepository,
        )
        from careerops.infrastructure.database.postgres_profile_repo import (
            PostgresProfileRepository,
        )

        self.matching_read_repo = PostgresMatchingReadRepository(self.database)
        self.job_read_repo = PostgresJobReadRepository(self.database)
        self.contact_repo = PostgresContactRepository(self.database)
        # ``PostgresApplicationRepository`` doubles as the ResumeRepository,
        # PackageRepository and FollowUpRepository and already carries the
        # Section-2 lifecycle methods (content-addressed resume dedupe,
        # eligible-resume filtering, cycle/package/payload linkage).
        self.application_repo = PostgresApplicationRepository(self.database)
        # Section-2 server-side-candidate-owned repos (tasks 2.2 / 2.5 / 2.6).
        # Every method on these repos scopes by a server-resolved
        # ``candidate_id``; there is NO unscoped or in-memory fallback — a
        # request that reaches them while the DB is down surfaces a
        # sqlalchemy error, and ``api.app`` exposes them through DI helpers
        # that turn a missing repo into ``DependencyNotReadyError`` (503).
        self.profile_repo = PostgresProfileRepository(self.database)
        self.evidence_repo = PostgresEvidenceRepository(self.database)
        self.application_cycle_repo = PostgresApplicationCycleRepository(self.database)
        # Section-4 crawl source / plan / run repos (tasks 4.1-4.3). Same
        # pattern as the Section-2 candidate-owned repos: Postgres-backed, no
        # in-memory fallback, scoped by the server-resolved candidate. A route
        # that reaches them while the DB is down surfaces a sqlalchemy error;
        # ``api.app`` exposes them through DI helpers that turn a missing repo
        # into ``DependencyNotReadyError`` (503).
        self.crawl_source_repo = PostgresCrawlSourceRepository(self.database)
        self.crawl_plan_repo = PostgresCrawlPlanRepository(self.database)
        self.crawl_run_repo = PostgresCrawlRunRepository(self.database)
        self.crawl_permission_repo = PostgresCrawlPermissionRepository(self.database)
        self.crawl_attempt_repo = PostgresCrawlAttemptRepository(self.database)
        from careerops.infrastructure.database.agent_console_repo import (
            AgentActionRepository,
        )
        from careerops.infrastructure.database.postgres_notification_repo import (
            PostgresNotificationRepository,
        )

        self.agent_action_repo = AgentActionRepository(self.database)
        self.notification_repo = PostgresNotificationRepository(self.database)

        from careerops.application.agent_runtime import AgentRuntime
        from careerops.application.agent_services import (
            InterviewPreparationService,
            ResumeReviewService,
        )
        from careerops.infrastructure.database.postgres_agent_run_repo import (
            PostgresAgentRunRepository,
        )
        from careerops.model_gateway.base import StructuredModelClient

        self.agent_run_repo = PostgresAgentRunRepository(self.database)
        agent_model: StructuredModelClient = self.model_client
        self.agent_runtime = AgentRuntime(
            self.agent_run_repo,
            capability_resolver=self.capability_resolver,
        )
        self.resume_review_service = ResumeReviewService(
            self.agent_runtime,
            resume_repository=self.application_repo,
            evidence_repository=self.evidence_repo,
            profile_repository=self.profile_repo,
            job_repository=self.job_read_repo,
            model_client=agent_model,
        )
        self.interview_preparation_service = InterviewPreparationService(
            self.agent_runtime,
            resume_repository=self.application_repo,
            evidence_repository=self.evidence_repo,
            profile_repository=self.profile_repo,
            job_repository=self.job_read_repo,
            model_client=agent_model,
        )

        # Section-6 inbox repository (tasks 6.1-6.3). Persists filter
        # decisions and requirement match results. Same Postgres-backed
        # pattern as the Section-2/4 repos.
        from careerops.infrastructure.database.postgres_inbox_repo import (
            PostgresInboxRepository,
        )

        self.inbox_repo = PostgresInboxRepository(self.database)
        from careerops.application.crawl_downstream import CrawlDownstreamService
        from careerops.application.inbox_service import InboxProjectionService
        from careerops.application.matching import MatchOrchestrator

        self.match_orchestrator = MatchOrchestrator(
            data_repository=self.matching_read_repo,
            result_repository=self.matching_read_repo,
        )
        self.inbox_service = InboxProjectionService(
            inbox_repo=self.inbox_repo,
            profile_repo=self.profile_repo,
            evidence_repo=self.evidence_repo,
            job_data_repo=self.matching_read_repo,
            capability_resolver=self.capability_resolver,
            model_client=self.model_client,
        )
        self.crawl_downstream_service = CrawlDownstreamService(
            self.match_orchestrator,
            self.inbox_service,
            self.matching_read_repo,
        )

        # Section-12 mail-intelligence repos (tasks 12.5 / 12.3). Durable
        # EmailEventProposal store + minimized message reader. Same
        # Postgres-backed pattern as the Section-2/4/6 repos: no in-memory
        # fallback, scoped by the server-resolved candidate. No live OAuth /
        # external-write / auto-send flag is enabled here (task 17.6) — these
        # repos are a review-only record store.
        from careerops.infrastructure.database.postgres_mail_repo import (
            PostgresEmailEventProposalRepository,
            PostgresMailMessageRepository,
        )

        self.mail_proposal_repo = PostgresEmailEventProposalRepository(self.database)
        self.mail_message_repo = PostgresMailMessageRepository(self.database)

        # Section-11 Gmail read-sync repos (tasks 11.1-11.6). Dedicated-account
        # connection state + durable sync runs/cursors + thread association
        # links. Same Postgres-backed pattern: no in-memory fallback, scoped by
        # the server-resolved candidate. The GMAIL_READ capability stays DENIED
        # at the contract layer (Iron Rule 7); these repos build the read path
        # but no live OAuth / external-write flag is enabled here (task 17.6).
        from careerops.infrastructure.database.postgres_mail_sync_repo import (
            PostgresMailAccountRepository,
            PostgresSyncRunRepository,
            PostgresThreadLinkRepository,
        )

        self.mail_account_repo = PostgresMailAccountRepository(self.database)
        self.mail_sync_run_repo = PostgresSyncRunRepository(self.database)
        self.mail_thread_link_repo = PostgresThreadLinkRepository(self.database)

        # Section 5 crawl execution service (tasks 5.1, 5.5, 5.6). Wraps the
        # crawl adapter sink + policy evaluation + provenance ingest. The
        # execution service is what Temporal activities (task 5.4) or a direct
        # in-process call invokes to run a PENDING crawl run through to
        # terminal state.
        from careerops.adapters.http_fetcher import fetch, fetch_public_ats
        from careerops.application.crawl_execution import CrawlExecutionService
        from careerops.infrastructure.temporal.crawl_stack import build_real_crawl_sink

        crawl_stack = build_real_crawl_sink(
            self.database,
            fetcher=fetch,
            public_ats_fetcher=fetch_public_ats,
            model_client=self.model_client,
        )
        crawl_sink = crawl_stack.sink
        crawl_agent = crawl_stack.agent
        self.crawl_sink = crawl_sink
        self.crawl_agent = crawl_agent
        # Phase 7.1: Tier 2 budget coordinator (Postgres-backed, durable across
        # worker restarts).  Injected into BoundedTier2Orchestrator when the
        # remaining Tier 2 dependencies (session checker, eligibility checker)
        # are wired.  CrawlActivationService and bounded_tier2 will consume this
        # via the BudgetChecker protocol.
        from careerops.infrastructure.database.postgres_tier2_budget import (
            PostgresTier2Budget,
        )

        self.tier2_budget: PostgresTier2Budget = PostgresTier2Budget(self.database)

        # Phase 7: Bounded Tier 2 orchestrator — wires budget, permission
        # checker, session checker, and policy into a single entry point
        # for the crawl execution loop. The agent is injected directly (PR #7
        # review round 2): the sink no longer holds it, so it travels from the
        # stack factory straight into the orchestrator that drives it.
        from careerops.application.bounded_tier2 import (
            BoundedTier2Orchestrator,
            RepositoryPermissionChecker,
        )
        from careerops.application.crawl_permission_service import (
            CrawlPermissionService,
        )
        from careerops.application.source_queue import SourceQueueService
        from careerops.infrastructure.database.audit import PostgresAuditWriterEngine
        from careerops.infrastructure.database.permission_attention import (
            PostgresPermissionAttentionSink,
            PostgresPermissionAuditSink,
        )

        self.crawl_permission_service = CrawlPermissionService(
            self.crawl_permission_repo,
            self.crawl_source_repo,
            audit_sink=PostgresPermissionAuditSink(PostgresAuditWriterEngine(self.database)),
            attention_sink=PostgresPermissionAttentionSink(
                self.agent_action_repo,
                self.notification_repo,
            ),
        )
        self.source_queue_service = SourceQueueService(
            self.crawl_source_repo,
            self.crawl_attempt_repo,
        )

        self.bounded_tier2 = BoundedTier2Orchestrator(
            sink=crawl_sink,
            budget=self.tier2_budget,
            permission_checker=RepositoryPermissionChecker(self.crawl_permission_repo),
            permission_repo=self.crawl_permission_repo,
            agent=crawl_agent,
        )

        self.crawl_execution_service = CrawlExecutionService(
            run_repository=self.crawl_run_repo,
            plan_repository=self.crawl_plan_repo,
            source_repository=self.crawl_source_repo,
            sink=crawl_sink,
            source_queue=self.source_queue_service,
            permission_service=self.crawl_permission_service,
            tier2=self.bounded_tier2,
            downstream_projector=self.crawl_downstream_service,
        )

    async def check(self) -> ReadinessReport:
        if self._closed:
            return ReadinessReport(
                checks={component: ReadinessState.NOT_READY for component in _COMPONENTS}
            )

        checks = await asyncio.gather(
            self._bounded(self._check_database),
            self._bounded(self._check_redis),
            self._bounded(self._check_temporal),
            self._bounded(self._check_storage),
        )
        return ReadinessReport(checks=dict(zip(_COMPONENTS, checks, strict=True)))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_agent = getattr(self.crawl_agent, "close", None)
        if callable(close_agent):
            await asyncio.to_thread(close_agent)
        await self.redis.aclose(close_connection_pool=True)
        await asyncio.to_thread(self.redis_sync.close)
        await asyncio.to_thread(self.database.dispose)
        self._temporal = None

    async def _bounded(
        self,
        check: Callable[[], Awaitable[None]],
    ) -> ReadinessState:
        try:
            async with asyncio.timeout(self._settings.readiness_timeout_seconds):
                await check()
        except Exception:
            return ReadinessState.NOT_READY
        return ReadinessState.OK

    async def _check_database(self) -> None:
        expected_role = f"careerops_{self._settings.database_role.value}"

        def execute() -> None:
            with self.database.connect() as connection:
                row = connection.execute(
                    text("SELECT 1, current_user::text, current_setting('search_path')")
                ).one()
                if row[0] != 1:
                    raise RuntimeError("database capability is not active")
                # Allow runtime user (which is a member of the expected role)
                if row[1] != expected_role and row[1] != "careerops_runtime":
                    raise RuntimeError("database capability is not active")
                if tuple(part.strip() for part in row[2].split(",")) != (
                    "pg_catalog",
                    "careerops",
                ):
                    raise RuntimeError("database search path is not bounded")

        await asyncio.to_thread(execute)

    async def _check_redis(self) -> None:
        if await self.redis.ping() is not True:
            raise RuntimeError("Redis ping was not acknowledged")

    async def _check_temporal(self) -> None:
        if self._temporal is None:
            async with self._temporal_lock:
                if self._temporal is None:
                    self._temporal = await Client.connect(
                        self._settings.temporal_address,
                        namespace=self._settings.temporal_namespace,
                        lazy=False,
                    )
        healthy = await self._temporal.service_client.check_health(
            timeout=timedelta(seconds=self._settings.readiness_timeout_seconds),
        )
        if not healthy:
            raise RuntimeError("Temporal health check failed")

    async def get_temporal_client(self) -> Client:
        """Return a healthy, lazily connected Temporal client for API enqueueing."""
        if self._closed:
            raise RuntimeError("runtime resources are closed")
        await self._check_temporal()
        if self._temporal is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Temporal client is unavailable")
        return self._temporal

    async def _check_storage(self) -> None:
        await asyncio.to_thread(_verify_storage_directory, self._settings.storage_root)

    def _build_career_graph_stack(self) -> None:
        """Compile the LangGraph review stack onto this runtime.

        PRODUCTION uses durable stores (``PostgresSaver`` +
        ``PostgresSideEffectStore`` + ``GmailSideEffectProvider`` when flags
        allow). Non-production uses in-memory stores
        (``MemorySaver`` + ``InMemorySideEffectStore`` +
        ``FakeSideEffectProvider``).

        The compiled graph is exposed as ``career_graph`` so the review endpoint
        can resume interrupted threads; ``review_mapping`` and
        ``side_effect_kernel`` are exposed so the endpoint can reverse-resolve
        approvals and disambiguate duplicate vs conflicting decisions.
        """
        from careerops.application.side_effect_kernel import SideEffectKernel
        from careerops.model_gateway.factory import create_model_client
        from careerops.orchestration.graph import build_graph
        from careerops.orchestration.mapping_store import InMemoryReviewMappingStore
        from careerops.orchestration.state import ContactDTO, RawJobDTO

        settings = self._settings
        metrics = self._metrics
        is_production = settings.environment is RuntimeEnvironment.PRODUCTION
        # Reuse the shared Phase-0 resolver (Iron Rule: one source of truth
        # for capability decisions across the review stack and the new
        # external-effect projections).
        capability_resolver = self.capability_resolver

        # Checkpointer: PostgresSaver in PRODUCTION (if available), MemorySaver otherwise.
        # PRODUCTION wraps in try/except so the app starts even without a
        # database (fail-open for the non-review endpoints).
        if is_production:
            try:
                checkpointer = self._build_postgres_saver()
            except Exception:
                # Database unavailable in PRODUCTION: review endpoint stays absent.
                return
            from careerops.infrastructure.database.side_effect_postgres import (
                PostgresSideEffectStore,
            )

            side_effect_store: object = PostgresSideEffectStore(self.database)
            side_effect_provider = self._build_side_effect_provider()
        else:
            from langgraph.checkpoint.memory import MemorySaver

            checkpointer = MemorySaver()
            from careerops.infrastructure.database.side_effect_memory import (
                InMemorySideEffectStore,
            )

            side_effect_store = InMemorySideEffectStore()
            side_effect_provider = self._build_side_effect_provider()

        from careerops.infrastructure.database.audit import PostgresAuditWriterEngine

        audit_writer = PostgresAuditWriterEngine(self.database) if is_production else None
        kernel = SideEffectKernel(
            side_effect_store, side_effect_provider, audit_writer=audit_writer
        )  # type: ignore[arg-type]
        review_mapping: ReviewMappingStore = InMemoryReviewMappingStore()

        # Wire LLM token recording via the model client factory (ADR 0006).
        usage_recorder = metrics if metrics is not None else None
        model_client = create_model_client(
            settings.model_provider,
            base_url=settings.model_base_url,
            api_key=settings.model_api_key.get_secret_value(),
            model=settings.model_name,
            usage_recorder=usage_recorder,
        )

        # Wire apply_submitted counter (non-blocking callback).
        send_callback: Callable[[int], None] | None = None
        if metrics is not None:
            send_callback = metrics.record_apply_submitted

        def demo_crawler() -> tuple[RawJobDTO, ...]:
            return ()

        def demo_extractor(jobs: tuple[RawJobDTO, ...]) -> tuple[ContactDTO, ...]:
            del jobs
            return ()

        self.side_effect_kernel = kernel
        self.review_mapping = review_mapping
        self.model_client = model_client
        self.career_graph = build_graph(
            crawler=demo_crawler,
            extractor=demo_extractor,
            resume_text="",
            model_client=model_client,
            kernel=kernel,
            review_mapping=review_mapping,
            capability_resolver=capability_resolver,
            checkpointer=checkpointer,
            send_callback=send_callback,
        )

    def _build_postgres_saver(self) -> Any:
        """Build a ``PostgresSaver`` and run ``setup()`` to create tables.

        ``from_conn_string`` is a context manager in v3 that yields a
        ``PostgresSaver`` backed by a connection pool. We enter the context
        manager, call ``setup()`` to create the checkpoint tables in the
        ``langgraph`` schema, and store the saver for the process lifetime.
        Cleanup happens in ``close()``.
        """
        from langgraph.checkpoint.postgres import PostgresSaver

        raw_url = self._settings.database_url.get_secret_value()
        conn_string = _langgraph_conn_string(raw_url)
        ctx = PostgresSaver.from_conn_string(conn_string)
        saver: PostgresSaver = ctx.__enter__()  # type: ignore[attr-defined]
        saver.setup()
        # Store the context manager so it stays alive; the underlying pool
        # will be cleaned up when the process exits (or on explicit close).
        self._postgres_saver_ctx = ctx  # type: ignore[attr-defined]
        return saver

    def _build_side_effect_provider(self) -> SideEffectProvider:
        """Build the side-effect provider.

        Returns ``GmailSideEffectProvider`` (real Gmail API send) when both
        ``auto_send_enabled`` and ``external_writes_enabled`` are True and a
        usable OAuth token file exists; otherwise ``FakeSideEffectProvider``.
        If the flags are on but no usable token is found, this fails loud so
        the misconfiguration surfaces at startup instead of silently using fake.
        """
        settings = self._settings
        if settings.auto_send_enabled and settings.external_writes_enabled:
            gmail = self._build_gmail_provider()
            if gmail is not None:
                return gmail
            raise RuntimeError(
                "auto_send_enabled/external_writes_enabled are on but no usable "
                "Gmail send token was found at secrets/gmail_send_token.json "
                "(run scripts/gmail_auth_send.py to authorize gmail.send)"
            )

        from careerops.integrations.fake_side_effect_provider import (
            FakeSideEffectProvider,
        )

        return FakeSideEffectProvider()

    def _build_gmail_provider(self) -> SideEffectProvider | None:
        """Build a ``GmailSideEffectProvider`` backed by a shared token store.

        Loads ``secrets/gmail_send_token.json`` into a single
        :class:`~careerops.integrations.gmail_token_store.GmailTokenStore`,
        then constructs ``GmailSender`` against that store. The store is
        retained on ``self.gmail_token_store`` so the Gmail read path
        (``GmailReader``) reuses the same refresh cadence — ``gmail.send`` and
        ``gmail.readonly`` were granted together, so one refresh token serves
        both directions.

        Dual-layer refresh (spec ``proactive-trigger-loop``): a scheduled
        activity refreshes hourly and the send path refreshes just before send
        when the cached token is near expiry. The initial ``refresh_now`` here
        makes a broken credential fail fast at startup.

        Returns None when the token file is absent. Raises when the file exists
        but is incomplete or the initial refresh fails.
        """
        from pathlib import Path

        from careerops.integrations.gmail_sender import GmailSender
        from careerops.integrations.gmail_side_effect_provider import (
            GmailSideEffectProvider,
        )
        from careerops.integrations.gmail_token_store import GmailTokenStore

        token_file = Path("secrets/gmail_send_token.json")
        if not token_file.exists():
            return None
        store = GmailTokenStore.from_token_file(token_file)
        store.refresh_now()  # fail-fast on a broken credential + cache token
        self.gmail_token_store = store  # share with the read path (GmailReader)
        return GmailSideEffectProvider(
            GmailSender(store),
            storage=_AttachmentPathResolver(self.storage),
        )


def _verify_storage_directory(root: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("storage root is not a directory")
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK, follow_symlinks=False):
            raise RuntimeError("storage root is not accessible")
    finally:
        with suppress(OSError):
            os.close(descriptor)


def create_runtime_resources(settings: Settings) -> RuntimeResources:
    return RuntimeResources(settings)
