from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.engine import Engine

from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.release_evidence import PostgresReleaseEvidenceReader


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read release qualification and bounded autopilot evidence.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="show one release qualification evidence bundle")
    show.add_argument("--qualification-id", type=UUID, required=True)
    show.add_argument("--json", action="store_true", required=True, help="emit JSON")

    trace = subparsers.add_parser("trace-intent", help="trace one action intent evidence chain")
    trace.add_argument("--intent-id", type=UUID, required=True)
    trace.add_argument("--json", action="store_true", required=True, help="emit JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings(database_role=DatabaseCapabilityRole.READONLY)
    engine = create_database_engine(settings)
    try:
        return _run(engine, args)
    finally:
        engine.dispose()


def _run(engine: Engine, args: argparse.Namespace) -> int:
    with engine.connect() as connection:
        reader = PostgresReleaseEvidenceReader(connection)
        if args.command == "show":
            result = reader.show(args.qualification_id)
            if result is None:
                print(_json_dump({"error": "release_qualification_not_found"}))
                return 1
            print(_json_dump(result.to_json()))
            return 0
        if args.command == "trace-intent":
            result = reader.trace_intent(args.intent_id)
            if result is None:
                print(_json_dump({"error": "action_intent_not_found"}))
                return 1
            print(_json_dump(result.to_json()))
            return 0
    raise SystemExit(f"unsupported command: {args.command}")


def _json_dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
