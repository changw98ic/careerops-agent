"""drop console_users and console_sessions tables

Revision ID: 0039
Revises: 0038

The console login model was removed by the auth-removal refactor (all
service/web code deleted). 0039 drops the two remaining auth tables:
``console_users`` and ``console_sessions``.

Drop order matters:
- ``console_sessions.user_id`` references ``console_users.id``
  (``fk_console_sessions_user_id_console_users``, ondelete CASCADE), so
  sessions go first -- its own FK is dropped together with the table.
- ``bootstrap_tokens.used_by_user_id`` also references ``console_users.id``
  (``fk_bootstrap_tokens_used_by_user_id_console_users``, ondelete RESTRICT)
  and that constraint must be dropped first -- otherwise PostgreSQL refuses
  the ``DROP TABLE console_users``. ``bootstrap_tokens`` itself is kept;
  only its outgoing reference to ``console_users`` is removed.

No other table references these two: ``candidates`` has no FK into
``console_users`` (``console_users.candidate_id`` points OUT to
``candidates.id`` and goes away with the table), and nothing references
``console_sessions`` except its own self-reference.

Downgrade is a documented no-op: the old auth model is gone and the tables
cannot be meaningfully restored.
"""

from __future__ import annotations

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("console_sessions", schema="careerops")
    op.drop_constraint(
        "fk_bootstrap_tokens_used_by_user_id_console_users",
        "bootstrap_tokens",
        schema="careerops",
        type_="foreignkey",
    )
    op.drop_table("console_users", schema="careerops")


def downgrade() -> None:
    # 不恢复（旧 auth 模型已删）。保留空 downgrade。
    pass
