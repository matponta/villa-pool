"""Shared pytest fixtures for the villa_pool tests."""
from __future__ import annotations

import pytest

from custom_components.villa_pool.config_flow import PICKERS
from custom_components.villa_pool.const import CONF_NOTIFY_TARGET, DEFAULT_NOTIFY_TARGET

pytest_plugins = "pytest_homeassistant_custom_component"

# The config-entry data a freshly-completed flow would produce: every picker at
# its verified §2 default, and the cover picker (which has no default) absent.
ENTRY_DATA = {
    key: default for key, default, _domains in PICKERS if default
} | {CONF_NOTIFY_TARGET: DEFAULT_NOTIFY_TARGET}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in every test."""
    yield
