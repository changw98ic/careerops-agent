"""Extract recruiting emails from crawled job descriptions and sync to contacts table.

Queries all latest job_posting_versions, extracts emails from
structured_data->>'description' using regex, filters noise using
careerops.application.contact_extraction.is_noise, matches emails to
companies via job_postings -> job_sources -> companies, and inserts into
careerops.contacts table (ON CONFLICT DO NOTHING).

Safe to run multiple times (idempotent).

Usage:
    python3 scripts/sync_contacts.py
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import text

from careerops.application.contact_extraction import EMAIL_RE, is_noise
from careerops.config import get_settings
from careerops.infrastructure.database.engine import create_database_engine


def sync_contacts() -> None:
    settings = get_settings()
    engine = create_database_engine(settings, enforce_role=False)

    with engine.connect() as conn:
        # Get all latest job_posting_versions with their company_id
        rows = conn.execute(text("""
            SELECT DISTINCT ON (jpv.job_posting_id)
                   jpv.job_posting_id,
                   jpv.structured_data,
                   jpv.source_url,
                   js.company_id,
                   c.name AS company_name
            FROM careerops.job_posting_versions jpv
            JOIN careerops.job_postings jp ON jp.id = jpv.job_posting_id
            JOIN careerops.job_sources js ON js.id = jp.source_id
            JOIN careerops.companies c ON c.id = js.company_id
            ORDER BY jpv.job_posting_id, jpv.captured_at DESC
        """)).fetchall()

    print(f"Found {len(rows)} latest job posting versions")

    inserted = 0
    skipped_noise = 0
    skipped_no_context = 0
    skipped_duplicate = 0
    errors = 0
    seen: set[tuple[str, str]] = set()  # (company_id, email)

    for row in rows:
        posting_id = row[0]
        structured_data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        source_url = row[2]
        company_id = str(row[3])
        company_name = row[4]

        description = structured_data.get("description", "")
        if not description:
            continue

        # Strip HTML tags for better email extraction
        clean_desc = re.sub(r"<[^>]+>", " ", description)
        clean_desc = re.sub(r"\s+", " ", clean_desc).strip()

        for match in EMAIL_RE.finditer(clean_desc):
            email = match.group(0).strip().rstrip(".,;:)")

            # Dedup within this run
            dedup_key = (company_id, email.lower())
            if dedup_key in seen:
                skipped_duplicate += 1
                continue

            # Filter noise
            context_start = max(0, match.start() - 160)
            context_end = min(len(clean_desc), match.end() + 160)
            context = clean_desc[context_start:context_end]

            if is_noise(email, context):
                skipped_noise += 1
                continue

            seen.add(dedup_key)

            # Insert into contacts table
            try:
                with engine.begin() as insert_conn:
                    insert_conn.execute(
                        text("""
                            INSERT INTO careerops.contacts
                                (id, company_id, email, name, role, source,
                                 source_url, source_text, publicly_listed,
                                 domain_match, confidence, allowed_actions)
                            VALUES (:id, :company_id, :email, :name, :role, :source,
                                    :source_url, :source_text, :publicly_listed,
                                    :domain_match, :confidence, :allowed_actions)
                            ON CONFLICT (company_id, email) DO NOTHING
                        """),
                        {
                            "id": str(uuid.uuid4()),
                            "company_id": company_id,
                            "email": email,
                            "name": "",
                            "role": "",
                            "source": "job_page",
                            "source_url": source_url,
                            "source_text": context[:500],
                            "publicly_listed": True,
                            "domain_match": True,
                            "confidence": "high",
                            "allowed_actions": json.dumps(["display", "review", "draft_reply", "initiate_contact"]),
                        },
                    )
                inserted += 1
                print(f"  + {email} -> {company_name}")
            except Exception as e:
                errors += 1
                print(f"  ERROR {email} -> {company_name}: {e}")

    print(f"\nDone: {inserted} inserted, {skipped_noise} noise filtered, "
          f"{skipped_duplicate} duplicates, {skipped_no_context} no context, "
          f"{errors} errors")


if __name__ == "__main__":
    sync_contacts()
