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

from app.core import asyncio_compat

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


@pytest.fixture(scope="session", autouse=True)
def _remove_what_the_tests_made():
    """Delete the suite's accounts and listings when it finishes.

    The integration tests drive a real API against a real database, so every run
    leaves data behind. Nothing removed it, and it reached 3,038 accounts and
    1,626 listings against 30 real ones - at which point the concierge was
    recommending "An Evening of Spoken Word at Test Venue 0bf7" to the person
    developing it, and "the catalogue is thin" was a reasonable and completely
    wrong conclusion.

    After the whole session rather than after each test: the tests are ordinary
    HTTP calls with no shared transaction to roll back, and per-test cleanup
    would add a round trip to every one of them for the same result.

    Never fails the run. A suite that reports red because it could not tidy up
    afterwards teaches people to ignore red.
    """
    yield

    if not API_REACHABLE:
        # Nothing ran against the database, so there is nothing to remove.
        return
    try:
        from tests.cleanup import purge_blocking

        removed = purge_blocking()
    except Exception as exc:  # noqa: BLE001 - tidying up must not fail the suite
        print(f"\n[tests] could not remove test data: {exc}\n")
        return

    if removed:
        summary = ", ".join(f"{count} {name}" for name, count in removed.items())
        print(f"\n[tests] removed {summary}\n")


def make_moderator(email: str) -> None:
    """Grant moderator rights to an account, by reaching past the API.

    There is deliberately no endpoint that appoints the first moderator - that
    is what `test_moderator_rights_cannot_be_self_granted` protects - so a test
    needing one has to write the column. Shared here rather than copied into
    each module because the awkward part is not the SQL: it is that psycopg
    cannot drive Windows' default ProactorEventLoop, so this has to run on a
    selector loop of its own (see `app/core/asyncio_compat.py`).

    The account is an ordinary `mado-qa.example.org` one, so the session
    teardown removes it with everything else.
    """
    import asyncio

    from sqlalchemy import text

    from app.core.database import SessionFactory

    async def promote() -> None:
        async with SessionFactory() as session:
            await session.execute(
                text(
                    "UPDATE identity.users SET is_moderator = true WHERE id = "
                    "(SELECT user_id FROM identity.user_profiles WHERE email = :email)"
                ),
                {"email": email},
            )
            await session.commit()

    asyncio.run(promote(), loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture
def anyio_backend() -> str:
    """Pin anyio to asyncio so async tests do not also try trio."""
    return "asyncio"


def pytest_asyncio_loop_factories(config, item):
    """Run async tests on a loop psycopg can actually drive.

    The Windows trap documented in `app/core/asyncio_compat.py`, reaching the
    tests: asyncio defaults to `ProactorEventLoop` there, psycopg refuses to run
    on it, and any test touching the database dies with an `InterfaceError` whose
    text is about event loops and says nothing about what the test was doing.

    A loop factory rather than `set_event_loop_policy`, because the policy API is
    deprecated from Python 3.14 and this runs on 3.14. Returning None on every
    other platform leaves the default loop alone, which is already compatible.
    """
    factory = asyncio_compat.loop_factory()
    if factory is None:
        return None
    # Exactly one entry. The mapping is factory *names* to factories and
    # pytest-asyncio parametrises over it, so listing several here would run
    # every async test once per loop rather than choosing between them.
    return {"selector": factory}
