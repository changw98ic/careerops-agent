"""Tests for application idempotency, job detail completeness, and list pagination stability."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.api.routes.jobs import router as jobs_router


def _app(repo: MagicMock | None) -> FastAPI:
    app = FastAPI()
    app.state.job_read_repository = repo
    app.include_router(jobs_router)
    return app


def _job_detail(job_id: str | None = None) -> dict:
    return {
        "id": job_id or str(uuid4()),
        "company_id": str(uuid4()),
        "canonical_title": "Senior Engineer",
        "aggregate_state": "active",
        "current_apply_url": "https://example.com/apply",
        "aggregate_status": "open",
        "postings": [
            {
                "id": str(uuid4()),
                "source_id": str(uuid4()),
                "external_id": "ext-123",
                "canonical_url": "https://example.com/job/123",
                "source_state": "active",
                "first_seen_at": "2026-01-01T00:00:00Z",
                "last_seen_at": "2026-07-01T00:00:00Z",
                "source": {
                    "type": "greenhouse",
                    "identifier": "gh-123",
                    "base_url": "https://boards.greenhouse.io",
                    "state": "active",
                },
            }
        ],
        "versions": [{"version": 1, "title": "Senior Engineer"}],
        "merge_decisions": [],
    }


def _job_list(total: int = 3, cursor: str | None = None) -> dict:
    items = [
        {
            "id": str(uuid4()),
            "company_id": str(uuid4()),
            "canonical_title": f"Job {i}",
            "aggregate_state": "active",
        }
        for i in range(total)
    ]
    return {
        "items": items,
        "total": total,
        "next_cursor": cursor,
    }


class TestJobDetailCompleteness:
    def test_detail_includes_canonical_job_with_postings(self) -> None:
        job_id = str(uuid4())
        repo = MagicMock()
        repo.get_job_detail.return_value = _job_detail(job_id)
        app = _app(repo)
        resp = TestClient(app).get(f"/api/v1/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        cj = body["canonical_job"]
        assert cj["id"] == job_id
        assert cj["canonical_title"] == "Senior Engineer"
        assert cj["aggregate_state"] == "active"
        assert cj["current_apply_url"] == "https://example.com/apply"
        assert cj["aggregate_status"] == "open"
        assert len(cj["postings"]) == 1
        posting = cj["postings"][0]
        assert posting["external_id"] == "ext-123"
        assert posting["source"]["type"] == "greenhouse"

    def test_detail_includes_versions_and_merge_decisions(self) -> None:
        repo = MagicMock()
        detail = _job_detail()
        detail["versions"] = [{"version": 1, "title": "v1"}, {"version": 2, "title": "v2"}]
        detail["merge_decisions"] = [{"decision": "merged", "source": "ext-123"}]
        repo.get_job_detail.return_value = detail
        app = _app(repo)
        resp = TestClient(app).get(f"/api/v1/jobs/{detail['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["versions"]) == 2
        assert len(body["merge_decisions"]) == 1

    def test_detail_returns_404_for_unknown_job(self) -> None:
        repo = MagicMock()
        repo.get_job_detail.return_value = None
        app = _app(repo)
        resp = TestClient(app).get(f"/api/v1/jobs/{uuid4()}")
        assert resp.status_code == 404

    def test_detail_returns_503_when_repo_missing(self) -> None:
        app = _app(None)
        resp = TestClient(app).get(f"/api/v1/jobs/{uuid4()}")
        assert resp.status_code == 503

    def test_detail_posting_source_can_be_null(self) -> None:
        repo = MagicMock()
        detail = _job_detail()
        detail["postings"][0]["source"] = None
        repo.get_job_detail.return_value = detail
        app = _app(repo)
        resp = TestClient(app).get(f"/api/v1/jobs/{detail['id']}")
        assert resp.status_code == 200
        assert resp.json()["canonical_job"]["postings"][0]["source"] is None


class TestListPaginationStability:
    def test_first_page_returns_correct_size_and_total(self) -> None:
        repo = MagicMock()
        repo.list_canonical_jobs.return_value = _job_list(total=10, cursor="cursor-5")
        app = _app(repo)
        resp = TestClient(app).get("/api/v1/jobs?limit=5")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 10
        assert body["total"] == 10
        assert body["next_cursor"] == "cursor-5"
        assert body["has_more"] is True

    def test_last_page_has_no_cursor(self) -> None:
        repo = MagicMock()
        repo.list_canonical_jobs.return_value = _job_list(total=3, cursor=None)
        app = _app(repo)
        resp = TestClient(app).get("/api/v1/jobs?limit=5")
        assert resp.status_code == 200
        body = resp.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False

    def test_empty_list_returns_zero_total(self) -> None:
        repo = MagicMock()
        repo.list_canonical_jobs.return_value = _job_list(total=0, cursor=None)
        app = _app(repo)
        resp = TestClient(app).get("/api/v1/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["has_more"] is False

    def test_cursor_forwarded_to_repository(self) -> None:
        repo = MagicMock()
        repo.list_canonical_jobs.return_value = _job_list(total=5)
        app = _app(repo)
        TestClient(app).get("/api/v1/jobs?cursor=abc&limit=10")
        repo.list_canonical_jobs.assert_called_once_with(
            cursor="abc",
            limit=10,
            state=None,
            q=None,
        )

    def test_state_filter_forwarded_to_repository(self) -> None:
        repo = MagicMock()
        repo.list_canonical_jobs.return_value = _job_list(total=2)
        app = _app(repo)
        TestClient(app).get("/api/v1/jobs?state=active&q=engineer")
        repo.list_canonical_jobs.assert_called_once_with(
            cursor=None,
            limit=50,
            state="active",
            q="engineer",
        )

    def test_cache_control_header_is_no_store(self) -> None:
        repo = MagicMock()
        repo.list_canonical_jobs.return_value = _job_list(total=1)
        app = _app(repo)
        resp = TestClient(app).get("/api/v1/jobs")
        assert resp.headers.get("cache-control") == "no-store"


class TestApplicationIdempotency:
    """Repeated identical requests should return the same result."""

    def test_double_get_same_job_returns_identical_body(self) -> None:
        job_id = str(uuid4())
        repo = MagicMock()
        repo.get_job_detail.return_value = _job_detail(job_id)
        app = _app(repo)
        client = TestClient(app)
        r1 = client.get(f"/api/v1/jobs/{job_id}")
        r2 = client.get(f"/api/v1/jobs/{job_id}")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()

    def test_double_list_same_cursor_returns_identical_body(self) -> None:
        repo = MagicMock()
        repo.list_canonical_jobs.return_value = _job_list(total=5, cursor="next")
        app = _app(repo)
        client = TestClient(app)
        r1 = client.get("/api/v1/jobs?cursor=page1&limit=5")
        r2 = client.get("/api/v1/jobs?cursor=page1&limit=5")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()
