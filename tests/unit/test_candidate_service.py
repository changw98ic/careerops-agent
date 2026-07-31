from uuid import uuid4

from careerops.application.candidate_service import CandidateService


class _FakeRepo:
    def __init__(self):
        self.rows = []
        self._by_id = {}

    def list_all(self, *, limit=50):
        return list(self.rows)[:limit]

    def create(self, candidate):
        self.rows.append(candidate)
        self._by_id[candidate.id] = candidate
        return candidate

    def get(self, cid):
        return self._by_id.get(cid)


def test_create_and_list():
    svc = CandidateService(_FakeRepo())
    c = svc.create("Alice")
    assert c.display_name == "Alice"
    assert svc.list_all() == [c]
    assert svc.get(c.id) is c
    assert svc.get(uuid4()) is None
    assert svc.list_all(limit=0) == []
