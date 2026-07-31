"""Test-wide hermeticity: stop Settings from reading the repo .env file.

The checked-in/runtime ``.env`` holds real values for the running app (model
provider credentials, feature flags). Tests must be hermetic — they construct
``Settings`` to assert default-denied / disabled behavior, so leaking the app's
``.env`` into them would make the suite depend on whatever the operator
happened to configure. Here we disable ``Settings``' ``env_file`` source for the
whole suite; ``os.environ`` (``CAREEROPS_*``) still works for tests that set
values explicitly, and integration tests pass their disposable DB via env.
"""

from __future__ import annotations

from careerops.config import Settings

# Mutate the class config before any test constructs Settings. pydantic-settings
# builds its sources from model_config at instantiation, so this takes effect
# for every Settings()/model_validate in the suite.
Settings.model_config["env_file"] = None  # type: ignore[index]
