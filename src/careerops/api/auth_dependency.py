"""FastAPI dependency for API route identity.

The console login and its session/CSRF auth layer are being removed
(``require_api_auth`` / ``require_web_auth`` / ``require_candidate_id`` /
``reject_candidate_substitution`` were deleted in the auth-rm migration).
Candidate identity now comes from the URL path
(``/api/v1/candidates/{candidate_id}/...``); this module keeps the single
remaining dependency that validates that path-supplied id.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request

from careerops.api.errors import DependencyNotReadyError, NotFoundError


async def path_candidate_id(candidate_id: UUID, request: Request) -> UUID:
    """Resolve ``candidate_id`` from the request PATH and validate existence.

    Path-param replacement for the former session-resolved candidate id in the
    post-auth console: identity is supplied by the URL
    (``/api/v1/candidates/{candidate_id}/...``) rather than an authenticated
    session. Existence is verified through
    ``request.app.state.candidate_service.get(...)`` (wired by ``create_app``).

    Raises:
        DependencyNotReadyError (503): ``candidate_service`` is not present on
            ``app.state`` (misconfigured runtime).
        NotFoundError (404): no candidate exists for the supplied id.
    """
    svc = getattr(request.app.state, "candidate_service", None)
    if svc is None:
        raise DependencyNotReadyError("candidate_service is not configured")
    if svc.get(candidate_id) is None:
        raise NotFoundError(f"candidate {candidate_id} not found")
    return candidate_id
