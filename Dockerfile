FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN groupadd --system --gid 10001 careerops \
    && useradd --system --uid 10001 --gid careerops --home-dir /app careerops \
    && mkdir -p /app/data/objects \
        /app/datasets/manifests \
        /app/datasets/private/crawler-execution-claims \
        /app/datasets/private/crawler-execution-output-locks \
        /app/datasets/private/crawler-execution-reviews \
        /app/datasets/private/crawler-execution-snapshots \
    && chown -R careerops:careerops /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY datasets/manifests ./datasets/manifests
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

RUN uv sync --frozen --no-dev --no-cache \
    && chown -R careerops:careerops /app

USER careerops

CMD ["careerops"]
