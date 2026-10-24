"""Inputs supplied by the real-model acceptance environment."""

import pytest


def pytest_addoption(parser: pytest.Parser):
    parser.addoption("--model", help="Actual model ID returned by setup-ai-gateway")
