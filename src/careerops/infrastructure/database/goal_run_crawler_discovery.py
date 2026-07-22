from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn, cast

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.sql import Executable

from careerops.application.goal_run_crawler_discovery import (
    GoalRunCrawlerDiscoveryRepositoryError,
    GoalRunReviewedCrawlerResult,
    GoalRunReviewedCrawlerResultQuery,
    goal_run_reviewed_crawler_result_from_mapping,
)

_SQLSTATE_REASON_CODES = {
    "40001": "GOAL_RUN_CRAWLER_DISCOVERY_RETRY_SERIALIZATION",
    "40P01": "GOAL_RUN_CRAWLER_DISCOVERY_RETRY_DEADLOCK",
    "42501": "GOAL_RUN_CRAWLER_DISCOVERY_FORBIDDEN",
    "P0001": "GOAL_RUN_CRAWLER_DISCOVERY_REJECTED",
}


class PostgresGoalRunCrawlerDiscoveryRepository:
    """Read reviewed crawler result state through the G010 SECURITY DEFINER function only."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError(
                "goal-run crawler discovery repository requires an explicit transaction"
            )
        self._connection = connection

    def reviewed_result(
        self,
        query: GoalRunReviewedCrawlerResultQuery,
    ) -> GoalRunReviewedCrawlerResult:
        try:
            value = self._connection.execute(
                goal_run_reviewed_crawler_result_statement(query)
            ).scalar_one()
        except DBAPIError as exc:
            _raise_sanitized_database_error(exc)
        except SQLAlchemyError:
            raise GoalRunCrawlerDiscoveryRepositoryError(
                "GOAL_RUN_CRAWLER_DISCOVERY_DATABASE_ERROR"
            ) from None
        if not isinstance(value, Mapping):
            raise GoalRunCrawlerDiscoveryRepositoryError(
                "GOAL_RUN_CRAWLER_DISCOVERY_RESULT_INVALID"
            )
        return goal_run_reviewed_crawler_result_from_mapping(cast("Mapping[str, object]", value))


def goal_run_reviewed_crawler_result_statement(
    query: GoalRunReviewedCrawlerResultQuery,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_reviewed_crawler_result(
            _uuid_param("p_actor_id", query.actor_id),
            _uuid_param("p_goal_run_id", query.goal_run_id),
            _uuid_param("p_registry_id", query.registry_id),
            _text_param("p_source_id", query.source_id),
        ).label("reviewed_crawler_result")
    )


def _uuid_param(key: str, value: object) -> sa.BindParameter[Any]:
    return sa.bindparam(key, value, type_=postgresql.UUID(as_uuid=True))


def _text_param(key: str, value: str) -> sa.BindParameter[str]:
    return sa.bindparam(key, value, type_=sa.Text())


def _raise_sanitized_database_error(exc: DBAPIError) -> NoReturn:
    sqlstate = _sqlstate(exc)
    reason_code = _SQLSTATE_REASON_CODES.get(
        sqlstate or "",
        "GOAL_RUN_CRAWLER_DISCOVERY_DATABASE_ERROR",
    )
    raise GoalRunCrawlerDiscoveryRepositoryError(reason_code, sqlstate=sqlstate) from None


def _sqlstate(exc: DBAPIError) -> str | None:
    origin = exc.orig
    value = getattr(origin, "sqlstate", None) or getattr(origin, "pgcode", None)
    if isinstance(value, str) and len(value) == 5:
        return value
    return None


def compile_query_for_test(
    statement: sa.ClauseElement | Executable,
    *,
    literal_binds: bool = True,
) -> str:
    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": literal_binds},
        )
    )


__all__ = [
    "PostgresGoalRunCrawlerDiscoveryRepository",
    "compile_query_for_test",
    "goal_run_reviewed_crawler_result_statement",
]
