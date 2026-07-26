"""One-time re-crawl script to populate missing description and apply_url.

Inserts new job_posting_versions with populated fields. Safe to run
multiple times (idempotent via content_hash unique constraint).
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import sqlalchemy as sa
from sqlalchemy import text

from careerops.adapters.http_fetcher import fetch
from careerops.adapters.job_sources import (
    AshbyAdapter,
    GreenhouseDetailAdapter,
    LeverAdapter,
)
from careerops.config import get_settings
from careerops.infrastructure.database.engine import create_database_engine


def _content_hash(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def re_crawl() -> None:
    settings = get_settings()
    engine = create_database_engine(settings, enforce_role=False)

    gh_detail = GreenhouseDetailAdapter()
    ashby_adapter = AshbyAdapter()
    lever_adapter = LeverAdapter()

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT DISTINCT ON (jpv.job_posting_id)
                   jpv.id, jpv.job_posting_id, jpv.source_url, jpv.structured_data,
                   jp.external_id, js.source_type, js.source_identifier
            FROM job_posting_versions jpv
            JOIN job_postings jp ON jp.id = jpv.job_posting_id
            JOIN job_sources js ON js.id = jp.source_id
            ORDER BY jpv.job_posting_id, jpv.captured_at DESC
        """)
        ).fetchall()

    print(f"Found {len(rows)} posting versions to check")

    updated = 0
    skipped = 0
    errors = 0

    for row in rows:
        posting_id = row[1]
        source_url = row[2]
        structured_data = row[3] if isinstance(row[3], dict) else json.loads(row[3])
        external_id = row[4]
        source_type = row[5]
        source_identifier = row[6]

        desc = structured_data.get("description", "")
        apply_url = structured_data.get("apply_url", "")

        needs_desc = len(desc) < 50
        needs_url = not apply_url

        if not needs_desc and not needs_url:
            skipped += 1
            continue

        detail_url = None
        new_desc = None
        new_url = None

        try:
            if source_type == "greenhouse":
                base = source_url.rstrip("/")
                detail_url = (
                    f"{base}/{external_id}"
                    if base.endswith("/jobs")
                    else f"{base}/jobs/{external_id}"
                )
                resp = fetch(detail_url)
                data = json.loads(resp.body)
                record = gh_detail.fetch_job(
                    data, source_url=detail_url, fetched_at=resp.fetched_at
                )
                if needs_desc and record.description:
                    new_desc = record.description
                if needs_url and record.url:
                    new_url = record.url

            elif source_type == "ashby":
                resp = fetch(source_url)
                data = json.loads(resp.body)
                result = ashby_adapter.list_jobs(data)
                for j in result.jobs:
                    if j.external_id == external_id:
                        if needs_desc and j.description:
                            new_desc = j.description
                        if needs_url and j.url:
                            new_url = j.url
                        break

            elif source_type == "lever":
                resp = fetch(source_url)
                data = json.loads(resp.body)
                result = lever_adapter.list_jobs(data)
                for j in result.jobs:
                    if j.external_id == external_id:
                        if needs_desc and j.description:
                            new_desc = j.description
                        if needs_url and j.url:
                            new_url = j.url
                        break

            if new_desc or new_url:
                updated_data = dict(structured_data)
                if new_desc and needs_desc:
                    updated_data["description"] = new_desc
                if new_url and needs_url:
                    updated_data["apply_url"] = new_url

                new_hash = _content_hash(updated_data)
                now = datetime.now(tz=UTC)

                with engine.begin() as update_conn:
                    update_conn.execute(
                        sa.text("""
                        INSERT INTO job_posting_versions
                            (id, job_posting_id, content_hash, source_url, parser_version,
                             structured_data, changed_fields, captured_at)
                        VALUES (:id, :posting_id, :hash, :source_url, :parser_version,
                                :data, :changed, :captured)
                        ON CONFLICT (job_posting_id, content_hash) DO NOTHING
                    """),
                        {
                            "id": str(uuid.uuid4()),
                            "posting_id": str(posting_id),
                            "hash": new_hash,
                            "source_url": source_url,
                            "parser_version": "re-crawl-v1",
                            "data": json.dumps(updated_data),
                            "changed": json.dumps([]),
                            "captured": now.isoformat(),
                        },
                    )
                updated += 1
                print(
                    f"  ✅ {source_identifier}/{external_id}: "
                    f"desc={bool(new_desc)} url={bool(new_url)}"
                )
            else:
                skipped += 1

        except Exception as e:
            errors += 1
            print(f"  ❌ {source_identifier}/{external_id}: {e}")

    print(f"\nDone: {updated} updated, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    re_crawl()
