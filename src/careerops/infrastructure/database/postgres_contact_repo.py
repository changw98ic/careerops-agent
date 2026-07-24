"""PostgreSQL-backed implementation of the ContactRepository protocol."""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine, RowMapping

from careerops.domain.contacts import (
    ContactAction,
    ContactConfidence,
    ContactSource,
    RecruitingContact,
)
from careerops.infrastructure.database.schema import contacts


class PostgresContactRepository:
    """ContactRepository backed by the ``contacts`` PostgreSQL table."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_by_email(self, company_id: UUID, email: str) -> RecruitingContact | None:
        stmt = (
            sa.select(contacts)
            .where(contacts.c.company_id == company_id, contacts.c.email == email)
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._to_domain(row) if row else None

    def find_by_company(self, company_id: UUID) -> list[RecruitingContact]:
        stmt = (
            sa.select(contacts)
            .where(contacts.c.company_id == company_id)
            .order_by(contacts.c.created_at, contacts.c.id)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._to_domain(row) for row in rows]

    def save(self, contact: RecruitingContact) -> None:
        stmt = (
            pg_insert(contacts)
            .values(
                id=contact.id,
                company_id=contact.company_id,
                email=contact.email,
                name=contact.name,
                role=contact.role,
                source=contact.source.value,
                source_url=contact.source_url,
                source_text=contact.source_text,
                publicly_listed=contact.publicly_listed,
                domain_match=contact.domain_match,
                confidence=contact.confidence.value,
                allowed_actions=[a.value for a in contact.allowed_actions],
                verified_at=contact.verified_at,
            )
            .on_conflict_do_update(
                index_elements=["company_id", "email"],
                set_={
                    "name": contact.name,
                    "role": contact.role,
                    "source": contact.source.value,
                    "source_url": contact.source_url,
                    "source_text": contact.source_text,
                    "publicly_listed": contact.publicly_listed,
                    "domain_match": contact.domain_match,
                    "confidence": contact.confidence.value,
                    "allowed_actions": [a.value for a in contact.allowed_actions],
                    "verified_at": contact.verified_at,
                },
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    @staticmethod
    def _to_domain(row: RowMapping) -> RecruitingContact:
        allowed = tuple(ContactAction(v) for v in row["allowed_actions"])
        return RecruitingContact(
            id=row["id"],
            company_id=row["company_id"],
            email=row["email"],
            name=row["name"],
            role=row["role"],
            source=ContactSource(row["source"]),
            source_url=row["source_url"],
            source_text=row["source_text"],
            publicly_listed=row["publicly_listed"],
            domain_match=row["domain_match"],
            confidence=ContactConfidence(row["confidence"]),
            allowed_actions=allowed,
            verified_at=row["verified_at"],
            created_at=row["created_at"],
        )
