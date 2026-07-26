"""FastAPI dependency factory for Phase-0 capability gating.

end-to-end-career-application-loop task 2.11 (design Decision 11, Iron Rules 3
+ 5). New projections/repos that touch an external-effect capability route
through the shared :class:`SettingsCapabilityResolver` exposed on
``RuntimeResources.capability_resolver``. The defaults are load-bearing:

- profile read + crawl-plan read paths stay usable (capability released by
  default subject to downstream source policy);
- gmail-read / system-managed-send / auto-send stay DENIED at the contract
  layer until the relevant OAuth / external-write / qualification gate flips
  in a separate change. ``AUTO_SEND`` is permanently denied.

Iron Rule 3 precedence — dependency-not-ready (503) wins over capability
denial (403): if the resolver itself is not wired into ``app.state`` (the app
was built with a non-``RuntimeResources`` probe), the path is
*unavailable*, not *denied*, and the dependency raises
:class:`DependencyNotReadyError`. Per-capability dependency availability
(provider/model/worker down) is composed at the route via
:func:`require_capability`'s ``dependency_available`` argument using
:func:`is_external_effect_path_usable` so the precedence lives in one place.

This module deliberately ships ONLY the dependency surface — it adds no route
handlers. Section-3+ routes compose these dependencies with their
repo-specific lookups.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request

from careerops.api.errors import DeniedPolicyError, DependencyNotReadyError
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
    SettingsCapabilityResolver,
    is_external_effect_path_usable,
)

__all__ = [
    "require_capability",
    "require_repository",
]


def _resolver_or_503(request: Request) -> SettingsCapabilityResolver:
    """Return the shared capability resolver from ``app.state`` or 503.

    The resolver is wired by ``create_app`` only when the readiness probe is a
    :class:`RuntimeResources`. Its absence means the runtime never assembled
    the trusted-flag gate, so a released-vs-denied answer cannot be trusted —
    surface ``DependencyNotReadyError`` rather than guessing "denied" (which
    would hide a wiring bug) or "allowed" (which would break default-deny).
    """
    resolver = getattr(request.app.state, "capability_resolver", None)
    if resolver is None:
        raise DependencyNotReadyError(
            "capability resolver is not wired; runtime dependencies unavailable"
        )
    return resolver


def require_capability(
    kind: CapabilityKind,
    *,
    dependency_available: bool = True,
) -> Callable[[Request], Awaitable[CapabilityDecision]]:
    """Build a FastAPI dependency that gates a route on a Phase-0 capability.

    ``dependency_available`` lets the route compose a per-capability
    dependency signal (DB up, model reachable, provider connected) with the
    flag decision. Iron Rule 3 makes dependency-not-ready win over capability
    denial: a released capability whose dependency is down surfaces as
    ``DependencyNotReadyError`` (503), and a denied capability whose
    dependency is also down still surfaces as 503 — never as a silent 403 that
    hides the missing dependency.

    Usage in a Section-3+ router::

        @router.post("/send")
        async def send(
            _gate: Annotated[
                CapabilityDecision,
                Depends(require_capability(CapabilityKind.SYSTEM_MANAGED_SEND)),
            ],
        ) -> ...: ...

    Released capabilities still pass through their own downstream
    source-policy / review_gate / outbox gates; this dependency is the
    contract-layer gate only.
    """

    async def _dependency(request: Request) -> CapabilityDecision:
        resolver = _resolver_or_503(request)
        usable, reason = is_external_effect_path_usable(
            resolver,
            kind,
            dependency_available=dependency_available,
        )
        if usable:
            return resolver.decide(kind)
        if reason == "dependency_not_ready":
            raise DependencyNotReadyError(f"{kind.value} backing dependency is not ready")
        raise DeniedPolicyError(reason)

    return _dependency


def require_repository(request: Request, name: str) -> object:
    """Return ``app.state.<name>`` or raise ``DependencyNotReadyError`` (503).

    For the new Section-2 repos (profile/evidence/application_cycle) the
    runtime constructs a Postgres-backed instance whenever
    :class:`RuntimeResources` is used; there is NO in-memory fallback. If a
    route reaches this helper and the repo is absent (non-RuntimeResources
    probe, or a typo in the wiring), the request fails loudly as 503 instead
    of silently degrading to an empty result.
    """
    repo = getattr(request.app.state, name, None)
    if repo is None:
        raise DependencyNotReadyError(f"{name} is not wired; runtime dependencies unavailable")
    return repo
