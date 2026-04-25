"""Shared pytest fixtures.

Three synthetic intake transcripts feed the eval harness in
tests/test_brief_extraction.py. The unit tests in tests/test_models.py do
not need them.
"""

from __future__ import annotations
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Load .env once at the start of the test session so eval tests can reach
# Gemini. If the file is absent we fall back to whatever is in the actual
# environment.
load_dotenv()

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def transcript_abdominal() -> str:
    return (FIXTURES_DIR / "transcript_abdominal.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def transcript_chest_pain_redflag() -> str:
    return (FIXTURES_DIR / "transcript_chest_pain_redflag.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def transcript_fatigue_multisystem() -> str:
    return (FIXTURES_DIR / "transcript_fatigue_multisystem.txt").read_text(encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_api_key: test calls Gemini and is skipped without GOOGLE_API_KEY",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip eval tests when GOOGLE_API_KEY is not set, with a clear reason.

    Marker: @pytest.mark.requires_api_key. Eval tests use it; unit tests don't.
    """
    if os.environ.get("GOOGLE_API_KEY"):
        return
    skip = pytest.mark.skip(reason="GOOGLE_API_KEY not set; skipping live-API eval test")
    for item in items:
        if "requires_api_key" in item.keywords:
            item.add_marker(skip)
