"""Wiring + default-deny contract for Section 11 mail-sync (tasks 11.7/11.10).

Mirrors the established ``test_e2e_wiring_task_2_11`` style:

- ``create_app`` exposes the Section-11 service + repos on ``app.state`` when
  ``RuntimeResources`` is the probe (so routes pull them via
  ``require_repository`` → 503 if absent).
- The mail-sync router is structurally gated on the ``GMAIL_READ`` capability,
  which stays DENIED at the contract layer (Iron Rule 7). The capability
  resolver returns a denied decision by default (``google_oauth_enabled`` is
  false), so every Section-11 route is default-deny until a separate
  qualification change releases it. Real Gmail OAuth is NOT enabled here
  (task 17.6).

No live DB / OAuth / external-write flag is exercised.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from careerops.api.app import create_app
from careerops.api.capability_dependency import require_capability
from careerops.api.errors import DeniedPolicyError
from careerops.config import RuntimeEnvironment, Settings
from careerops.orchestration.capability_resolver import (
    CapabilityKind,
    SettingsCapabilityResolver,
)


def _default_settings() -> Settings:
    return Settings.model_validate({"environment": RuntimeEnvironment.TEST})


def test_create_app_exposes_section11_mail_sync_service() -> None:
    """RuntimeResources wires MailSyncService + the three repos on app.state."""
    app = create_app(console_auth_service=MagicMock())
    assert hasattr(app.state, "mail_sync_service")
    assert app.state.mail_sync_service is not None
    assert app.state.mail_account_repository is not None
    assert app.state.mail_sync_run_repository is not None
    assert app.state.mail_thread_link_repository is not None


@pytest.mark.asyncio
async def test_gmail_read_denied_by_default() -> None:
    """GMAIL_READ is denied unless ``google_oauth_enabled`` flips (Iron Rule 7).

    The mail-sync router gates every route on this capability, so the whole
    Section-11 surface is default-deny. This is the contract-layer gate; the
    service-layer logic is exercised in test_section11_mail_sync.
    """
    resolver = SettingsCapabilityResolver(_default_settings())
    decision = resolver.decide(CapabilityKind.GMAIL_READ)
    assert decision.released is False


@pytest.mark.asyncio
async def test_require_capability_gmail_read_denies_when_oauth_disabled() -> None:
    """The mail-sync router's gate denies when GMAIL_READ is not released.

    Mirrors ``test_require_capability_denies_send_by_default``. A released
    gate would require the dependency to be available too (Iron Rule 3:
    dependency-not-ready beats denied), so we only assert the denied branch
    here.
    """
    state = SimpleNamespace(
        capability_resolver=SettingsCapabilityResolver(_default_settings())
    )
    dependency = require_capability(CapabilityKind.GMAIL_READ)
    request = MagicMock()
    request.app = SimpleNamespace(state=state)
    with pytest.raises(DeniedPolicyError):
        await dependency(request)
