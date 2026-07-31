"""drop the console auth tables (console_users, console_sessions, bootstrap_tokens)

Revision ID: 0039
Revises: 0038

The console login model was removed by the auth-removal refactor (all
service/web code deleted). 0039 drops the three remaining auth tables:
``console_users``, ``console_sessions`` and ``bootstrap_tokens``.

Drop order matters: ``console_sessions.user_id`` and
``bootstrap_tokens.used_by_user_id`` both reference ``console_users.id``
(``fk_console_sessions_user_id_console_users`` ondelete CASCADE,
``fk_bootstrap_tokens_used_by_user_id_console_users`` ondelete RESTRICT), so
the referencing tables go first -- their own FKs are dropped together with
them -- and ``console_users`` last.

No other table references them: ``candidates`` has no FK into
``console_users`` (``console_users.candidate_id`` points OUT to
``candidates.id`` and goes away with the table), and nothing references
``console_sessions`` except its own self-reference.

Downgrade is a documented no-op: the old auth model is gone and the tables
cannot be meaningfully restored. The downgrades of 0002/0013/0014 guard on
table existence so the chain stays navigable for round-trip tests.
"""

from __future__ import annotations

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("console_sessions", schema="careerops")
    op.drop_table("bootstrap_tokens", schema="careerops")
    op.drop_table("console_users", schema="careerops")


def downgrade() -> None:
    # 不恢复（旧 auth 模型已删）。保留空 downgrade。
    pass
