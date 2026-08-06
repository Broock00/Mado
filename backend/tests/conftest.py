"""Shared test configuration.

The important thing here is the API availability gate.

The integration tests run against a live server. They used to *skip* when that
server was unreachable, which meant a completely broken build reported 117
passed, 48 skipped and a green tick - the exact tests that would have caught the
breakage were the ones that quietly stepped aside. A skip is indistinguishable
from a pass at a glance, and in CI nobody is glancing.

So the default is now to **fail** when the API is unreachable, and skipping is
opt-in via ``MADO_SKIP_API_TESTS=1``. A developer running unit tests without
starting the stack sets it deliberately; CI never does, so a missing API is
loud.
"""

from __future__ import annotations

import os

import httpx
import pytest

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")

# Opt out explicitly. Named for what it does rather than for an environment, so
# nobody sets it in CI thinking it means something else.
SKIP_REQUESTED = os.environ.get("MADO_SKIP_API_TESTS", "").lower() in {"1", "true", "yes"}


def _api_reachable() -> bool:
    try:
        return httpx.get(f"{BASE_URL}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


API_REACHABLE = _api_reachable()


def requires_api() -> None:
    """Guard for a module of integration tests.

    Fails rather than skips, unless skipping was explicitly requested. Called
    from a module-level fixture so the message appears once per module instead of
    once per test.
    """
    if API_REACHABLE:
        return
    if SKIP_REQUESTED:
        pytest.skip(f"API not reachable at {BASE_URL} (MADO_SKIP_API_TESTS is set)")
    pytest.fail(
        f"The API is not reachable at {BASE_URL}, so the integration tests cannot "
        f"run. Start it with `python run.py`, point MADO_TEST_API_URL elsewhere, "
        f"or set MADO_SKIP_API_TESTS=1 to skip them deliberately.",
        pytrace=False,
    )


@pytest.fixture(scope="session", autouse=True)
def _report_api_state() -> None:
    """Say once, up front, whether the integration tests will really run."""
    if not API_REACHABLE and SKIP_REQUESTED:
        print(f"\n[tests] API unreachable at {BASE_URL}; integration tests SKIPPED by request.\n")


@pytest.fixture
def anyio_backend() -> str:
    """Pin anyio to asyncio so async tests do not also try trio."""
    return "asyncio"
