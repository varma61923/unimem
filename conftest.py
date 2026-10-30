"""Shared test configuration for both core-only and optional-async installations."""

import pytest


def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
    # Core-only installations still read the shared config. Register its optional
    # async setting without loading the plugin or altering a configured value.
    if not pluginmanager.hasplugin("asyncio"):
        parser.addini("asyncio_default_fixture_loop_scope", "Default pytest-asyncio fixture scope", default="function")
