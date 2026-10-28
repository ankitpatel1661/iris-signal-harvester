"""`.env.example` must document every setting the application reads.

A configuration template that has drifted from the code is worse than none: it tells an
operator a variable does not exist when it does, and the gap is invisible until something
behaves unexpectedly in production. Six settings had drifted out of the template before this
test existed, including the one `make demo` relies on to point the fixture source at the
generated corpus.

This is the same kind of guard as ``test_architecture.py``: a documented claim turned into an
enforced one, so it cannot quietly regress.
"""

from __future__ import annotations

import re
from pathlib import Path

from iris_signal_harvester.config import Settings

ENV_PREFIX = "IRIS_"
TEMPLATE = Path(__file__).resolve().parents[1] / ".env.example"

# Compose-only variables. These configure the throwaway local PostGIS container in
# docker-compose.yml; they are not read by Settings and have no business being in it.
COMPOSE_ONLY = {
    "IRIS_POSTGRES_USER",
    "IRIS_POSTGRES_PASSWORD",
    "IRIS_POSTGRES_DB",
    "IRIS_DB_HOST_PORT",
}


def _documented_variables() -> set[str]:
    """Variable names in the template, whether set or shown commented out."""
    text = TEMPLATE.read_text(encoding="utf-8")
    return set(re.findall(rf"^#?\s*({ENV_PREFIX}[A-Z0-9_]+)=", text, re.MULTILINE))


def _settings_variables() -> set[str]:
    return {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}


def test_every_setting_appears_in_the_env_template() -> None:
    missing = _settings_variables() - _documented_variables()
    assert not missing, (
        "these settings are read by the application but are not in .env.example: "
        + ", ".join(sorted(missing))
    )


def test_the_template_documents_no_variable_the_application_ignores() -> None:
    """A template entry that does nothing is a lie an operator will act on."""
    stray = _documented_variables() - _settings_variables() - COMPOSE_ONLY
    assert not stray, "these variables are in .env.example but no setting reads them: " + ", ".join(
        sorted(stray)
    )


def test_the_template_carries_no_real_secret() -> None:
    """The template must stay safe to commit: no default DSN, no real contact address."""
    text = TEMPLATE.read_text(encoding="utf-8")

    dsn_assignments = re.findall(r"^\s*IRIS_DB_DSN=(.+)$", text, re.MULTILINE)
    assert not dsn_assignments, (
        "IRIS_DB_DSN must stay unset in the template - the application deliberately has no "
        f"default and secrets come from the environment; found: {dsn_assignments}"
    )

    user_agent = re.search(r"^IRIS_USER_AGENT=(.+)$", text, re.MULTILINE)
    assert user_agent is not None, "the template must show IRIS_USER_AGENT"
    assert "example.org" in user_agent.group(1), (
        "the committed user-agent must use a placeholder contact address, not a real one"
    )
