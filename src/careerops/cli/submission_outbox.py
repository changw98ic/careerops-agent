from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from time import sleep

from sqlalchemy.engine import Engine

from careerops.application.outbox import OutboxPublisher
from careerops.application.submission_outbox import (
    SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX,
    DeterministicSyntheticSubmissionProvider,
    SyntheticSubmissionOutboxSink,
)
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.outbox import PostgresOutboxStore
from careerops.infrastructure.database.submission_outbox import (
    PostgresSyntheticSubmissionCoordinator,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish synthetic submission outbox events.",
    )
    parser.add_argument(
        "--owner",
        default="synthetic-submission-publisher",
        help="bounded outbox lease owner identifier",
    )
    parser.add_argument("--limit", type=int, default=10, help="maximum events to claim")
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=30,
        help="outbox lease duration in seconds",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=0,
        help="poll continuously at this interval; 0 processes one batch and exits",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="maximum safe synthetic delivery attempts before terminal failure",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.poll_seconds < 0 or args.poll_seconds > 60:
        raise SystemExit("--poll-seconds must be between 0 and 60")
    if args.max_attempts < 2 or args.max_attempts > 10:
        raise SystemExit("--max-attempts must be between 2 and 10")

    settings = Settings(database_role=DatabaseCapabilityRole.OUTBOX)
    engine = create_database_engine(settings)
    publisher = _publisher(
        engine,
        owner=args.owner,
        max_attempts=args.max_attempts,
    )
    try:
        while True:
            result = publisher.publish_batch(
                owner=args.owner,
                now=datetime.now(UTC),
                lease_for=timedelta(seconds=args.lease_seconds),
                limit=args.limit,
            )
            print(
                "synthetic submission outbox publish: "
                f"claimed={result.claimed} published={result.published} "
                f"deferred={result.deferred} failed={result.failed}"
            )
            if args.poll_seconds == 0:
                return 1 if result.failed else 0
            sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


def _publisher(
    engine: Engine,
    *,
    owner: str,
    max_attempts: int,
) -> OutboxPublisher:
    sink = SyntheticSubmissionOutboxSink(
        PostgresSyntheticSubmissionCoordinator(engine, owner=owner),
        provider=DeterministicSyntheticSubmissionProvider(),
    )
    return OutboxPublisher(
        PostgresOutboxStore(engine),
        sink,
        max_attempts=max_attempts,
        event_key_prefix=SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX,
    )


if __name__ == "__main__":
    raise SystemExit(main())
