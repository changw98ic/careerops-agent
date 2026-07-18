from __future__ import annotations

from datetime import UTC, datetime

from careerops.config import get_settings
from careerops.infrastructure.auth import create_console_auth_service
from careerops.infrastructure.database.engine import create_database_engine


def main() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        credential = create_console_auth_service(engine).issue_bootstrap_token(
            now=datetime.now(UTC)
        )
    finally:
        engine.dispose()
    print(credential.token)
    print(f"expires_at={credential.expires_at.isoformat()}")


if __name__ == "__main__":
    main()
