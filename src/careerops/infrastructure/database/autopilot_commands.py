from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql import Executable

from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.application.autopilot_commands import (
    AutopilotCommandRejected,
    AutopilotGrantScopeDraft,
    CampaignGrantVersionRef,
    CreateCampaignGrantCommand,
    GrantRevocationResult,
    RevokeGrantCommand,
    SupersedeGrantCommand,
)
from careerops.infrastructure.database.audit import PostgresAuditWriter
from careerops.infrastructure.database.schema import (
    autopilot_campaigns,
    autopilot_grant_revocations,
    autopilot_grant_versions,
)


class PostgresAutopilotCommandStore:
    """Append-only campaign grant commands scoped to a caller-owned transaction."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("autopilot grant commands require an explicit transaction")
        self._connection = connection
        self._audit = PostgresAuditWriter(connection)

    def create_campaign_with_grant(
        self,
        command: CreateCampaignGrantCommand,
    ) -> CampaignGrantVersionRef:
        campaign_id = uuid4()
        grant_version_id = uuid4()
        self._connection.execute(
            sa.insert(autopilot_campaigns).values(
                id=campaign_id,
                owner_user_id=command.actor_id,
                name=command.campaign.name,
                objective=command.campaign.objective,
                criteria=dict(command.campaign.criteria),
                exclusions=list(command.campaign.exclusions),
                created_by=str(command.actor_id),
            )
        )
        self._insert_grant_version(
            campaign_id=campaign_id,
            grant_version_id=grant_version_id,
            version=1,
            scope=command.grant_scope,
        )
        self._append_audit(
            event_type="autopilot.campaign_grant.created",
            actor_id=command.actor_id,
            resource_type="autopilot_campaign",
            resource_id=campaign_id,
            trace_id=command.trace_id,
            event_data={
                "campaign_id": str(campaign_id),
                "grant_version_id": str(grant_version_id),
                "version": 1,
            },
        )
        return CampaignGrantVersionRef(
            campaign_id=campaign_id,
            grant_version_id=grant_version_id,
            version=1,
        )

    def supersede_grant(
        self,
        command: SupersedeGrantCommand,
    ) -> CampaignGrantVersionRef:
        self._lock_grant_revocation_state(command.current_grant_version_id)
        row = self._lock_owned_campaign(command.campaign_id, command.actor_id)
        if row is None:
            raise AutopilotCommandRejected("CAMPAIGN_NOT_FOUND_OR_NOT_OWNED")

        current = self._select_grant_for_campaign(
            campaign_id=command.campaign_id,
            grant_version_id=command.current_grant_version_id,
        )
        if current is None:
            raise AutopilotCommandRejected("CAMPAIGN_GRANT_NOT_FOUND")
        if self._select_revocation(command.current_grant_version_id) is not None:
            raise AutopilotCommandRejected("CAMPAIGN_GRANT_ALREADY_REVOKED")

        next_version = self._next_grant_version(command.campaign_id)
        replacement_id = uuid4()
        self._insert_grant_version(
            campaign_id=command.campaign_id,
            grant_version_id=replacement_id,
            version=next_version,
            scope=command.replacement_scope,
        )
        revocation_id = uuid4()
        self._insert_revocation(
            revocation_id=revocation_id,
            grant_version_id=command.current_grant_version_id,
            revoked_by_user_id=command.actor_id,
            superseded_by_grant_version_id=replacement_id,
            reason=command.reason,
        )
        self._append_audit(
            event_type="autopilot.campaign_grant.superseded",
            actor_id=command.actor_id,
            resource_type="autopilot_campaign",
            resource_id=command.campaign_id,
            trace_id=command.trace_id,
            event_data={
                "campaign_id": str(command.campaign_id),
                "previous_grant_version_id": str(command.current_grant_version_id),
                "replacement_grant_version_id": str(replacement_id),
                "version": next_version,
                "revocation_id": str(revocation_id),
            },
        )
        return CampaignGrantVersionRef(
            campaign_id=command.campaign_id,
            grant_version_id=replacement_id,
            version=next_version,
        )

    def revoke_grant(
        self,
        command: RevokeGrantCommand,
    ) -> GrantRevocationResult:
        self._lock_grant_revocation_state(command.grant_version_id)
        grant = self._lock_owned_grant_campaign(
            grant_version_id=command.grant_version_id,
            actor_id=command.actor_id,
        )
        if grant is None:
            raise AutopilotCommandRejected("CAMPAIGN_GRANT_NOT_FOUND_OR_NOT_OWNED")

        existing = self._select_revocation(command.grant_version_id)
        if existing is not None:
            return GrantRevocationResult(
                revocation_id=cast("UUID", existing["id"]),
                grant_version_id=command.grant_version_id,
                campaign_id=cast("UUID", grant["campaign_id"]),
                superseded_by_grant_version_id=cast(
                    "UUID | None", existing["superseded_by_grant_version_id"]
                ),
                newly_created=False,
            )

        revocation_id = uuid4()
        inserted = self._insert_revocation_once(
            revocation_id=revocation_id,
            grant_version_id=command.grant_version_id,
            revoked_by_user_id=command.actor_id,
            superseded_by_grant_version_id=None,
            reason=command.reason,
        )
        if inserted is None:
            existing = self._select_revocation(command.grant_version_id)
            if existing is None:
                raise RuntimeError("revocation insert conflicted but existing row was not found")
            return GrantRevocationResult(
                revocation_id=cast("UUID", existing["id"]),
                grant_version_id=command.grant_version_id,
                campaign_id=cast("UUID", grant["campaign_id"]),
                superseded_by_grant_version_id=cast(
                    "UUID | None", existing["superseded_by_grant_version_id"]
                ),
                newly_created=False,
            )
        self._append_audit(
            event_type="autopilot.campaign_grant.revoked",
            actor_id=command.actor_id,
            resource_type="autopilot_grant_version",
            resource_id=command.grant_version_id,
            trace_id=command.trace_id,
            event_data={
                "campaign_id": str(grant["campaign_id"]),
                "grant_version_id": str(command.grant_version_id),
                "revocation_id": str(revocation_id),
            },
        )
        return GrantRevocationResult(
            revocation_id=revocation_id,
            grant_version_id=command.grant_version_id,
            campaign_id=cast("UUID", grant["campaign_id"]),
            superseded_by_grant_version_id=None,
            newly_created=True,
        )

    def _insert_grant_version(
        self,
        *,
        campaign_id: UUID,
        grant_version_id: UUID,
        version: int,
        scope: AutopilotGrantScopeDraft,
    ) -> None:
        self._connection.execute(
            sa.insert(autopilot_grant_versions).values(
                id=grant_version_id,
                campaign_id=campaign_id,
                version=version,
                subject_actor=scope.subject_actor,
                allowed_action_kinds=list(scope.allowed_action_kinds),
                allowed_channels=list(scope.allowed_channels),
                allowed_target_hosts=list(scope.allowed_target_hosts),
                material_hashes=list(scope.material_hashes),
                max_total_submissions=scope.max_total_submissions,
                max_daily_submissions=scope.max_daily_submissions,
                max_per_company=scope.max_per_company,
                policy_ruleset_version=scope.policy_ruleset_version,
                release_version=scope.release_version,
                expires_at=scope.expires_at,
            )
        )

    def _insert_revocation_once(
        self,
        *,
        revocation_id: UUID,
        grant_version_id: UUID,
        revoked_by_user_id: UUID,
        superseded_by_grant_version_id: UUID | None,
        reason: str,
    ) -> RowMapping | None:
        return (
            self._connection.execute(
                _insert_revocation_once_statement(
                    revocation_id=revocation_id,
                    grant_version_id=grant_version_id,
                    revoked_by_user_id=revoked_by_user_id,
                    superseded_by_grant_version_id=superseded_by_grant_version_id,
                    reason=reason,
                )
            )
            .mappings()
            .first()
        )

    def _insert_revocation(
        self,
        *,
        revocation_id: UUID,
        grant_version_id: UUID,
        revoked_by_user_id: UUID,
        superseded_by_grant_version_id: UUID | None,
        reason: str,
    ) -> None:
        self._connection.execute(
            sa.insert(autopilot_grant_revocations).values(
                id=revocation_id,
                grant_version_id=grant_version_id,
                revoked_by_user_id=revoked_by_user_id,
                superseded_by_grant_version_id=superseded_by_grant_version_id,
                reason=reason,
            )
        )

    def _lock_owned_campaign(self, campaign_id: UUID, actor_id: UUID) -> RowMapping | None:
        return (
            self._connection.execute(_lock_owned_campaign_statement(campaign_id, actor_id))
            .mappings()
            .first()
        )

    def _lock_grant_revocation_state(self, grant_version_id: UUID) -> None:
        self._connection.execute(_lock_grant_revocation_state_statement(grant_version_id))

    def _lock_owned_grant_campaign(
        self,
        *,
        grant_version_id: UUID,
        actor_id: UUID,
    ) -> RowMapping | None:
        return (
            self._connection.execute(
                _lock_owned_grant_campaign_statement(grant_version_id, actor_id)
            )
            .mappings()
            .first()
        )

    def _select_grant_for_campaign(
        self,
        *,
        campaign_id: UUID,
        grant_version_id: UUID,
    ) -> RowMapping | None:
        return (
            self._connection.execute(
                sa.select(
                    autopilot_grant_versions.c.id,
                    autopilot_grant_versions.c.version,
                ).where(
                    autopilot_grant_versions.c.campaign_id == campaign_id,
                    autopilot_grant_versions.c.id == grant_version_id,
                )
            )
            .mappings()
            .first()
        )

    def _select_revocation(self, grant_version_id: UUID) -> RowMapping | None:
        return (
            self._connection.execute(
                sa.select(
                    autopilot_grant_revocations.c.id,
                    autopilot_grant_revocations.c.superseded_by_grant_version_id,
                ).where(autopilot_grant_revocations.c.grant_version_id == grant_version_id)
            )
            .mappings()
            .first()
        )

    def _next_grant_version(self, campaign_id: UUID) -> int:
        next_version = sa.func.coalesce(sa.func.max(autopilot_grant_versions.c.version), 0) + 1
        version = self._connection.scalar(
            sa.select(next_version).where(autopilot_grant_versions.c.campaign_id == campaign_id)
        )
        if not isinstance(version, int):
            raise RuntimeError("database did not return a grant version")
        return version

    def _append_audit(
        self,
        *,
        event_type: str,
        actor_id: UUID,
        resource_type: str,
        resource_id: UUID,
        trace_id: str,
        event_data: dict[str, JsonValue],
    ) -> None:
        self._audit.append(
            AuditEventDraft(
                event_type=event_type,
                actor_type=AuditActorType.USER,
                actor_id=str(actor_id),
                resource_type=resource_type,
                resource_id=resource_id,
                trace_id=trace_id,
                event_data=event_data,
            )
        )


def _lock_owned_campaign_statement(campaign_id: UUID, actor_id: UUID) -> sa.Select[tuple[UUID]]:
    return (
        sa.select(autopilot_campaigns.c.id)
        .where(
            autopilot_campaigns.c.id == campaign_id,
            autopilot_campaigns.c.owner_user_id == actor_id,
        )
        .with_for_update(of=autopilot_campaigns)
    )


def _lock_owned_grant_campaign_statement(
    grant_version_id: UUID,
    actor_id: UUID,
) -> sa.Select[tuple[UUID, UUID]]:
    return (
        sa.select(
            autopilot_campaigns.c.id.label("campaign_id"),
            autopilot_grant_versions.c.id.label("grant_version_id"),
        )
        .select_from(
            autopilot_grant_versions.join(
                autopilot_campaigns,
                autopilot_campaigns.c.id == autopilot_grant_versions.c.campaign_id,
            )
        )
        .where(
            autopilot_grant_versions.c.id == grant_version_id,
            autopilot_campaigns.c.owner_user_id == actor_id,
        )
        .with_for_update(of=autopilot_campaigns)
    )


def _lock_grant_revocation_state_statement(grant_version_id: UUID) -> sa.TextClause:
    return sa.text(
        """
        SELECT pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(
                :lock_key,
                0
            )
        )
        """
    ).bindparams(lock_key=f"careerops:autopilot-grant-state:{grant_version_id}")


def _insert_revocation_once_statement(
    *,
    revocation_id: UUID,
    grant_version_id: UUID,
    revoked_by_user_id: UUID,
    superseded_by_grant_version_id: UUID | None,
    reason: str,
) -> Executable:
    return (
        postgresql.insert(autopilot_grant_revocations)
        .values(
            id=revocation_id,
            grant_version_id=grant_version_id,
            revoked_by_user_id=revoked_by_user_id,
            superseded_by_grant_version_id=superseded_by_grant_version_id,
            reason=reason,
        )
        .on_conflict_do_nothing(index_elements=[autopilot_grant_revocations.c.grant_version_id])
        .returning(
            autopilot_grant_revocations.c.id,
            autopilot_grant_revocations.c.superseded_by_grant_version_id,
        )
    )


def compile_query_for_test(statement: sa.ClauseElement | Executable) -> str:
    """Return deterministic PostgreSQL SQL for unit tests without opening a database."""

    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
