from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep

from careerops.application.crawler_outbox import (
    CRAWLER_EXECUTION_EVENT_KEY_PREFIX,
    CrawlerExecutionOutboxSink,
    CrawlSourcesCliRunner,
)
from careerops.application.outbox import OutboxPublisher
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.crawler_outbox import (
    PostgresCrawlerExecutionDispatchReader,
    PostgresCrawlerExecutionOutboxStore,
)
from careerops.infrastructure.database.engine import create_database_engine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish reviewed crawler execution outbox events.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root containing the reviewed crawler artifacts",
    )
    parser.add_argument(
        "--owner",
        default="crawler-execution-publisher",
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
        help="maximum safe pre-execution delivery attempts before terminal failure",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.poll_seconds < 0 or args.poll_seconds > 60:
        raise SystemExit("--poll-seconds must be between 0 and 60")
    if args.max_attempts < 2 or args.max_attempts > 10:
        raise SystemExit("--max-attempts must be between 2 and 10")
    root = args.root.resolve()
    settings = Settings(database_role=DatabaseCapabilityRole.OUTBOX)
    engine = create_database_engine(settings)
    store = PostgresCrawlerExecutionOutboxStore(engine)
    sink = CrawlerExecutionOutboxSink(
        PostgresCrawlerExecutionDispatchReader(engine, root=root),
        CrawlSourcesCliRunner(),
        root=root,
    )
    publisher = OutboxPublisher(
        store,
        sink,
        max_attempts=args.max_attempts,
        event_key_prefix=CRAWLER_EXECUTION_EVENT_KEY_PREFIX,
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
                "crawler outbox publish: "
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


if __name__ == "__main__":
    raise SystemExit(main())
