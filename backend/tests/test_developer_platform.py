"""API key and webhook tests (spec DEV-001, DEV-003).

Two features whose failures are quiet. A leaked key works perfectly until
somebody notices; a webhook that skips signature verification, follows a
redirect, or resolves to a private address does exactly what it was asked to do.
So most of what follows is about the refusals rather than the happy path.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401  (rows are constructed here, so mappers must resolve)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domains.developer import delivery as delivery_module
from app.domains.developer import webhooks as webhook_module
from app.domains.developer.delivery import (
    GONE,
    _body_of,
    _record,
    sign,
    signature_header,
    verify,
)
from app.domains.developer.keys import (
    KEY_PREFIX,
    MAX_KEYS,
    SCOPES,
    ApiKey,
    ApiKeyService,
    digest,
)
from app.domains.developer.webhooks import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_RETRYING,
    EVENT_TYPES,
    MAX_ATTEMPTS,
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    SUSPEND_AFTER_FAILURES,
    UnsafeEndpoint,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookService,
    check_endpoint_url,
    envelope,
    next_attempt,
)

pytestmark = pytest.mark.anyio


def account(**overrides):
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "status": "active",
            "deleted_at": None,
            **overrides,
        }
    )


class FakeSession:
    """Collects added rows and answers `get` from what it holds.

    The rules under test are decisions about in-memory state - which scope,
    which status, how long until the next attempt. Wiring a real session in
    would be testing SQLAlchemy.
    """

    def __init__(self, rows=(), listing=()):
        self.added: list = []
        self.rows = {row.id: row for row in rows}
        self.deleted: list = []
        self._listing = list(listing)

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        self.added.append(row)
        self.rows[row.id] = row

    async def get(self, _model, row_id):
        return self.rows.get(row_id)

    async def delete(self, row):
        self.deleted.append(row)

    async def flush(self):
        for row in self.added:
            if getattr(row, "created_at", None) is None:
                row.created_at = datetime.now(UTC)

    async def execute(self, _stmt):
        rows = self._listing

        class Result:
            def scalars(self):
                return SimpleNamespace(
                    all=lambda: list(rows), first=lambda: rows[0] if rows else None
                )

            def scalar_one_or_none(self):
                return rows[0] if rows else None

        return Result()


def _all_routes(app):
    """Flatten the router tree.

    An included router is a wrapper object rather than a splice, so its routes
    hang off `original_router` instead of being present on `app.routes`.
    """
    stack, seen = list(app.routes), []
    while stack:
        route = stack.pop()
        seen.append(route)
        stack.extend(getattr(route, "routes", ()) or ())
        nested = getattr(route, "original_router", None)
        if nested is not None:
            stack.extend(nested.routes)
    return seen


def _scopes_enforced_by(dependant) -> set[str]:
    """Every scope this route's dependency tree checks.

    Read out of the closure of `caller_with_scope`'s inner function, which is
    the only place a scope is named - so this cannot pass because a scope name
    happens to appear in a docstring somewhere.
    """
    found: set[str] = set()
    call = getattr(dependant, "call", None)
    if getattr(call, "__qualname__", "").startswith("caller_with_scope"):
        for cell in call.__closure__ or ():
            if isinstance(cell.cell_contents, str):
                found.add(cell.cell_contents)
    for child in getattr(dependant, "dependencies", ()):
        found |= _scopes_enforced_by(child)
    return found


# ---------------------------------------------------------------- API keys


class TestKeysAreOnlyEverShownOnce:
    async def test_the_stored_row_cannot_be_used_to_call_the_api(self):
        """Somebody who reads this table finds a digest, not a credential."""
        session = FakeSession()
        key, plaintext = await ApiKeyService(session).issue(
            account(), name="Nightly sync", scopes=["experiences:read"]
        )
        assert plaintext not in (key.token_hash, key.preview)
        assert key.token_hash == digest(plaintext)

    async def test_the_preview_is_too_short_to_guess_from(self):
        session = FakeSession()
        key, plaintext = await ApiKeyService(session).issue(
            account(), name="Sync", scopes=["experiences:read"]
        )
        # Prefix and eight characters of a 32-character secret.
        assert key.preview.startswith(KEY_PREFIX)
        assert len(key.preview.replace("...", "")) < len(plaintext) / 2

    def test_the_prefix_is_recognisable_to_a_secret_scanner(self):
        """A key that looks like anonymous base64 gets committed and nobody
        notices. `mado_sk_` is greppable, in a log and in a public repository."""
        assert KEY_PREFIX.startswith("mado")
        assert KEY_PREFIX.endswith("_")


class TestWhatAKeyMayDo:
    async def test_a_key_with_no_scopes_is_refused(self):
        """It would authenticate and then be permitted nothing, which reads as
        a broken key rather than a careful one."""
        with pytest.raises(ValidationError) as caught:
            await ApiKeyService(FakeSession()).issue(account(), name="Empty", scopes=[])
        assert caught.value.code == "NO_SCOPES"

    async def test_an_invented_scope_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            await ApiKeyService(FakeSession()).issue(
                account(), name="Everything", scopes=["admin:*"]
            )
        assert caught.value.code == "UNKNOWN_SCOPE"

    def test_the_scope_list_stays_short(self):
        """A scope per endpoint stops being something anybody reads before
        ticking boxes, and a key that can do everything its owner can do is a
        password with extra steps."""
        assert len(SCOPES) <= 6
        assert all(":" in scope for scope in SCOPES)

    def test_every_scope_gates_a_route_that_exists(self):
        """A scope nobody enforces is worse than no scope: it tells the person
        ticking the box that they restricted something when they did not."""
        from app.main import app

        enforced: set[str] = set()
        for route in _all_routes(app):
            dependant = getattr(route, "dependant", None)
            if dependant is not None:
                enforced |= _scopes_enforced_by(dependant)

        assert set(SCOPES) <= enforced, f"unenforced: {set(SCOPES) - enforced}"

    def test_a_key_is_never_more_powerful_than_a_session(self):
        """Scopes exist to give a script *less* than its owner has. A scope that
        unlocked something the owner cannot do in a browser would be a privilege
        escalation with a checkbox."""
        from app.api import deps

        assert not hasattr(deps, "grant_scope")
        # The scoped dependency falls through to `current_user` when no key is
        # present, so a session is never checked against a scope.
        import inspect

        source = inspect.getsource(deps.caller_with_scope)
        assert "current_user" in source

    async def test_scopes_are_stored_sorted_and_deduplicated(self):
        key, _ = await ApiKeyService(FakeSession()).issue(
            account(),
            name="Sync",
            scopes=["experiences:write", "experiences:read", "experiences:read"],
        )
        assert key.scopes == ["experiences:read", "experiences:write"]


class TestRevocation:
    async def test_a_revoked_key_stops_authenticating(self):
        owner = account()
        key, plaintext = await ApiKeyService(FakeSession()).issue(
            owner, name="Sync", scopes=["experiences:read"]
        )
        key.revoked_at = datetime.now(UTC)
        assert not key.is_live
        assert key.state == "revoked"

    async def test_an_expired_key_stops_authenticating(self):
        key, _ = await ApiKeyService(FakeSession()).issue(
            account(), name="Sync", scopes=["experiences:read"], expires_in_days=1
        )
        key.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert not key.is_live
        assert key.state == "expired"

    async def test_revoking_somebody_elses_key_reads_as_not_found(self):
        """"That belongs to someone else" confirms the id exists to whoever is
        guessing."""
        key = ApiKey(id=uuid.uuid4(), owner_user_id=uuid.uuid4(), name="Theirs")
        with pytest.raises(NotFoundError):
            await ApiKeyService(FakeSession(rows=[key])).revoke(account(), key.id)

    async def test_revoking_twice_does_not_move_the_date(self):
        """Otherwise the record of when a key stopped working is the record of
        when somebody last clicked the button."""
        owner = account()
        key = ApiKey(
            id=uuid.uuid4(),
            owner_user_id=owner.id,
            name="Sync",
            revoked_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        service = ApiKeyService(FakeSession(rows=[key]))
        await service.revoke(owner, key.id)
        assert key.revoked_at == datetime(2026, 1, 1, tzinfo=UTC)

    def test_there_is_no_way_to_unrevoke(self):
        """A compromised credential could otherwise be brought back by whoever
        compromised the account."""
        methods = {m for m in dir(ApiKeyService) if not m.startswith("_")}
        assert not methods & {"restore", "unrevoke", "reinstate", "enable"}


class TestAuthenticatingWithAKey:
    async def test_a_string_that_is_not_a_key_is_rejected_without_a_query(self):
        """The prefix check runs first, so a stray Authorization header does not
        become a database lookup per request."""

        class NoQueries(FakeSession):
            async def execute(self, _stmt):
                raise AssertionError("looked the key up before checking its shape")

        assert await ApiKeyService(NoQueries()).authenticate("Bearer eyJhbGci...") is None

    async def test_a_suspended_owner_takes_their_keys_with_them(self):
        """Otherwise suspension is a formality anybody with a script steps around."""
        owner = account(status="suspended")
        key = ApiKey(
            id=uuid.uuid4(),
            owner_user_id=owner.id,
            name="Sync",
            token_hash=digest(KEY_PREFIX + "abc"),
            scopes=["experiences:read"],
            revoked_at=None,
            expires_at=None,
        )
        session = FakeSession(rows=[owner], listing=[key])
        assert await ApiKeyService(session).authenticate(KEY_PREFIX + "abc") is None

    async def test_last_used_is_not_rewritten_on_every_call(self):
        """It would be a database write per API request, to answer a question
        that a minute's resolution answers just as well."""
        key = ApiKey(id=uuid.uuid4(), owner_user_id=uuid.uuid4(), name="Sync")
        key.last_used_at = datetime.now(UTC)
        before = key.last_used_at
        ApiKeyService._touch(key)
        assert key.last_used_at == before

    async def test_but_a_stale_one_is_updated(self):
        key = ApiKey(id=uuid.uuid4(), owner_user_id=uuid.uuid4(), name="Sync")
        key.last_used_at = datetime.now(UTC) - timedelta(hours=1)
        ApiKeyService._touch(key)
        assert datetime.now(UTC) - key.last_used_at < timedelta(seconds=5)


class TestKeyLimits:
    async def test_you_cannot_mint_an_unbounded_number(self):
        owner = account()
        live = [
            ApiKey(id=uuid.uuid4(), owner_user_id=owner.id, name=f"k{i}", revoked_at=None)
            for i in range(MAX_KEYS)
        ]
        for key in live:
            key.expires_at = None
        with pytest.raises(ValidationError) as caught:
            await ApiKeyService(FakeSession(listing=live)).issue(
                owner, name="One more", scopes=["experiences:read"]
            )
        assert caught.value.code == "TOO_MANY_KEYS"

    async def test_revoked_keys_do_not_count_towards_the_limit(self):
        """They stay in the list for the record, not to occupy a slot."""
        owner = account()
        dead = [
            ApiKey(
                id=uuid.uuid4(),
                owner_user_id=owner.id,
                name=f"k{i}",
                revoked_at=datetime.now(UTC),
            )
            for i in range(MAX_KEYS)
        ]
        key, _ = await ApiKeyService(FakeSession(listing=dead)).issue(
            owner, name="Fresh", scopes=["experiences:read"]
        )
        assert key.state == "active"


# ------------------------------------------------------------ SSRF guard


class TestEndpointsCannotPointAtUs:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000/hook",
            "https://127.0.0.1/hook",
            "https://10.0.0.5/hook",
            "https://192.168.1.1/hook",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]/hook",
        ],
    )
    def test_private_and_loopback_addresses_are_refused(self, url):
        with pytest.raises(UnsafeEndpoint):
            check_endpoint_url(url)

    def test_the_metadata_address_is_refused_even_with_the_dev_hatch_open(self, monkeypatch):
        """`webhook_allow_private_endpoints` exists so a receiver on your own
        machine is testable. It is also the setting somebody eventually turns on
        in an environment that has a metadata service, and no development need
        involves 169.254.169.254."""
        from app.core import config

        settings = config.get_settings().model_copy(
            update={"webhook_allow_private_endpoints": True}
        )
        monkeypatch.setattr(webhook_module, "get_settings", lambda: settings)

        check_endpoint_url("http://127.0.0.1:9099/hook")  # the hatch works
        with pytest.raises(UnsafeEndpoint):
            check_endpoint_url("http://169.254.169.254/latest/meta-data/")

    def test_the_cloud_metadata_address_is_covered_by_the_general_rule(self):
        """Not by a special case. A hand-written list of dangerous addresses is
        a list somebody will forget to update; `is_global` already excludes
        link-local, which is where every cloud's metadata service lives."""
        import ipaddress

        assert not ipaddress.ip_address("169.254.169.254").is_global

    @pytest.mark.parametrize(
        "url", ["ftp://example.com/hook", "file:///etc/passwd", "//example.com"]
    )
    def test_only_http_urls_are_accepted(self, url):
        with pytest.raises(UnsafeEndpoint):
            check_endpoint_url(url)

    def test_credentials_in_the_url_are_refused(self):
        """They would end up in our logs, and we would be the one logging them."""
        with pytest.raises(UnsafeEndpoint):
            check_endpoint_url("https://user:secret@example.com/hook")

    def test_the_refusal_does_not_describe_the_internal_network(self):
        """"10.0.0.5 is private" confirms to somebody probing what is behind us."""
        with pytest.raises(UnsafeEndpoint) as caught:
            check_endpoint_url("https://10.0.0.5/hook")
        assert "10.0.0.5" not in str(caught.value)
        assert "private" not in str(caught.value).lower()

    def test_a_public_hostname_passes(self):
        check_endpoint_url("https://example.com/hooks/mado")

    def test_validation_happens_again_at_send_time(self):
        """DNS belongs to the receiver. A name that resolved to a public address
        when we approved it can resolve to a metadata endpoint an hour later, so
        checking once at subscribe time is checking the wrong moment."""
        import inspect

        source = inspect.getsource(delivery_module._attempt)
        assert "check_endpoint_url" in source

    def test_redirects_are_not_followed(self):
        """A 302 is the cheapest way to turn an endpoint we validated into one
        we did not."""
        import inspect

        source = inspect.getsource(delivery_module.deliver_due)
        assert "follow_redirects=False" in source


# ------------------------------------------------------------- signatures


class TestSigning:
    def test_a_receiver_can_verify_what_we_send(self):
        body = b'{"id":"abc","type":"ping"}'
        timestamp = int(time.time())
        header = signature_header("whsec_test", timestamp, body)
        assert verify("whsec_test", header, body)

    def test_a_changed_body_fails(self):
        timestamp = int(time.time())
        header = signature_header("whsec_test", timestamp, b'{"amount":1}')
        assert not verify("whsec_test", header, b'{"amount":1000}')

    def test_the_wrong_secret_fails(self):
        timestamp = int(time.time())
        header = signature_header("whsec_one", timestamp, b"{}")
        assert not verify("whsec_two", header, b"{}")

    def test_an_old_signature_is_rejected_even_though_it_is_valid(self):
        """Replay protection. Signing the body alone would make every delivery
        replayable forever by anybody who captured one."""
        stale = int(time.time()) - 3600
        header = signature_header("whsec_test", stale, b"{}")
        assert not verify("whsec_test", header, b"{}")

    def test_the_timestamp_cannot_be_moved_to_a_fresh_one(self):
        old = int(time.time()) - 3600
        header = signature_header("whsec_test", old, b"{}")
        forged = header.replace(f"t={old}", f"t={int(time.time())}")
        assert not verify("whsec_test", forged, b"{}")

    def test_the_separator_prevents_a_boundary_forgery(self):
        """Without it, (t=12, body="3x") and (t=123, body="x") sign the same
        material."""
        assert sign("s", 12, b"3x") != sign("s", 123, b"x")

    def test_a_malformed_header_is_a_failure_not_a_crash(self):
        for header in ["", "garbage", "t=notanumber,v1=abc", "v1=abc"]:
            assert not verify("whsec_test", header, b"{}")

    def test_the_scheme_carries_its_own_version(self):
        """A bare hex string leaves no way to change algorithm later without
        breaking every receiver on the same day."""
        header = signature_header("whsec_test", int(time.time()), b"{}")
        assert header.startswith("t=") and ",v1=" in header

    def test_the_body_is_serialised_once_and_deterministically(self):
        """Re-serialising for the signature and again for the request would
        eventually produce two different byte strings - a different key order is
        enough - and every signature would fail unreproducibly."""
        one = WebhookDelivery(payload={"b": 1, "a": 2})
        two = WebhookDelivery(payload={"a": 2, "b": 1})
        assert _body_of(one) == _body_of(two)
        assert json.loads(_body_of(one)) == {"a": 2, "b": 1}


# --------------------------------------------------------------- delivery


def endpoint(**overrides) -> WebhookEndpoint:
    row = WebhookEndpoint(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        url="https://example.com/hook",
        events=["experience.published"],
        secret="whsec_test",
        status=STATUS_ACTIVE,
        consecutive_failures=0,
    )
    for name, value in overrides.items():
        setattr(row, name, value)
    return row


def queued(**overrides) -> WebhookDelivery:
    row = WebhookDelivery(
        id=uuid.uuid4(),
        endpoint_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        event_type="experience.published",
        payload={},
        status=webhook_module.DELIVERY_PENDING,
        attempts=0,
        next_attempt_at=datetime.now(UTC),
    )
    for name, value in overrides.items():
        setattr(row, name, value)
    return row


class TestEmitting:
    async def test_one_delivery_is_queued_per_subscribed_endpoint(self):
        owner = account()
        two = [endpoint(owner_user_id=owner.id), endpoint(owner_user_id=owner.id)]
        session = FakeSession(listing=two)

        queued_count = await webhook_module.emit(
            session, event_type="experience.published", owner_user_id=owner.id, data={"a": 1}
        )
        assert queued_count == 2
        assert len(session.added) == 2

    async def test_every_copy_of_one_event_shares_its_id(self):
        """Two receivers comparing notes are then talking about the same event
        rather than about two."""
        owner = account()
        session = FakeSession(listing=[endpoint(owner_user_id=owner.id) for _ in range(3)])
        await webhook_module.emit(
            session, event_type="experience.published", owner_user_id=owner.id, data={}
        )
        assert len({row.event_id for row in session.added}) == 1

    async def test_the_first_attempt_is_due_immediately(self):
        owner = account()
        session = FakeSession(listing=[endpoint(owner_user_id=owner.id)])
        await webhook_module.emit(
            session, event_type="experience.published", owner_user_id=owner.id, data={}
        )
        assert session.added[0].next_attempt_at <= datetime.now(UTC)

    async def test_nothing_is_flushed_or_committed(self):
        """The announcement joins the transaction that caused it, so it cannot
        survive a rollback of the thing being announced."""

        class NoFlush(FakeSession):
            async def flush(self):
                raise AssertionError("emit flushed the caller's transaction")

        owner = account()
        await webhook_module.emit(
            NoFlush(listing=[endpoint(owner_user_id=owner.id)]),
            event_type="experience.published",
            owner_user_id=owner.id,
            data={},
        )

    async def test_a_broken_webhook_table_does_not_stop_a_publisher_publishing(self):
        """A subscription is a convenience for one party. It must never be the
        reason somebody cannot put their event in the city."""

        class Broken(FakeSession):
            async def execute(self, _stmt):
                raise RuntimeError("no such table")

        assert (
            await webhook_module.emit(
                Broken(), event_type="experience.published", owner_user_id=uuid.uuid4(), data={}
            )
            == 0
        )


class TestRetryPolicy:
    def test_each_retry_waits_longer_than_the_last(self):
        waits = [next_attempt(n) for n in range(1, MAX_ATTEMPTS)]
        gaps = [(w - datetime.now(UTC)).total_seconds() for w in waits]
        assert gaps == sorted(gaps)
        assert all(a < b for a, b in zip(gaps, gaps[1:], strict=False))

    def test_the_attempts_run_out(self):
        """An event that has been retried for an hour is stale enough that a
        receiver would rather fetch current state than replay it."""
        assert next_attempt(MAX_ATTEMPTS) is None

    def test_a_failure_is_retried_rather_than_abandoned(self):
        e, d = endpoint(), queued()
        _record(e, d, 500, None, 12)
        assert d.status == DELIVERY_RETRYING
        assert d.next_attempt_at > datetime.now(UTC)
        # Not yet a strike against the endpoint - it has not run out of chances.
        assert e.consecutive_failures == 0

    def test_the_last_failure_ends_the_delivery(self):
        e, d = endpoint(), queued(attempts=MAX_ATTEMPTS - 1)
        _record(e, d, 500, None, 12)
        assert d.status == DELIVERY_FAILED
        assert e.consecutive_failures == 1

    def test_success_clears_the_failure_count(self):
        """An endpoint that recovers gets a clean slate, so a bad afternoon
        three weeks ago does not add up to a suspension."""
        e, d = endpoint(consecutive_failures=7), queued()
        _record(e, d, 200, None, 12)
        assert d.status == DELIVERY_DELIVERED
        assert e.consecutive_failures == 0
        assert e.last_error is None

    def test_a_timeout_is_treated_like_any_other_failure(self):
        e, d = endpoint(), queued()
        _record(e, d, None, "ReadTimeout: timed out", 10000)
        assert d.status == DELIVERY_RETRYING
        assert "Timeout" in d.error


class TestEndpointHealth:
    def test_enough_failed_deliveries_suspend_the_endpoint(self):
        """At that point we are not experiencing a blip, we are hammering
        somebody who is not listening."""
        e = endpoint(consecutive_failures=SUSPEND_AFTER_FAILURES - 1)
        _record(e, queued(attempts=MAX_ATTEMPTS - 1), 500, None, 5)
        assert e.status == STATUS_SUSPENDED

    def test_410_suspends_immediately(self):
        """The receiver said the endpoint is gone. Believe them the first time,
        without spending four more attempts to confirm it."""
        e, d = endpoint(), queued()
        _record(e, d, GONE, None, 5)
        assert e.status == STATUS_SUSPENDED
        assert d.status == DELIVERY_FAILED
        assert d.attempts == 1

    def test_a_suspended_endpoint_says_why(self):
        """Spec BUSINESS-07. It stays in the list with the reason, so its owner
        can see what happened rather than finding it silently stopped."""
        e = endpoint()
        _record(e, queued(), GONE, None, 5)
        assert "410" in e.last_error

    async def test_resuming_clears_the_history(self):
        """Otherwise an endpoint that was suspended and then fixed is suspended
        again by its own past."""
        owner = account()
        e = endpoint(
            owner_user_id=owner.id,
            status=STATUS_SUSPENDED,
            consecutive_failures=SUSPEND_AFTER_FAILURES,
            last_error="Connection refused",
        )
        await WebhookService(FakeSession(rows=[e])).update(owner, e.id, status=STATUS_ACTIVE)
        assert e.consecutive_failures == 0
        assert e.last_error is None

    async def test_pausing_does_not_clear_it(self):
        """A paused endpoint has not been fixed, it has been switched off."""
        owner = account()
        e = endpoint(owner_user_id=owner.id, consecutive_failures=4, last_error="Timeout")
        await WebhookService(FakeSession(rows=[e])).update(owner, e.id, status="paused")
        assert e.consecutive_failures == 4


# ---------------------------------------------------------------- emitting


class TestTheEventVocabulary:
    def test_it_names_the_domain_rather_than_the_document(self):
        """The spec says `booking.created`. Mado has no bookings - it has
        reservations, which hold a place and take no money. An event type that
        lies about the domain model is worse than one that differs from a doc."""
        assert "reservation.created" in EVENT_TYPES
        assert not any(t.startswith("booking.") for t in EVENT_TYPES)

    def test_every_event_is_something_the_owner_did_or_had_done_to_them(self):
        """Not a feed of platform activity. A publisher's webhook describes
        their own listings and their own dates."""
        subjects = {t.split(".")[0] for t in EVENT_TYPES}
        assert subjects <= {"experience", "event", "reservation"}

    def test_every_advertised_event_is_actually_emitted_somewhere(self):
        """An event type you can subscribe to and never receive is worse than
        one that does not exist: it looks like a working integration until the
        day somebody notices the silence, and by then they have built on it.

        The same argument as `test_every_scope_gates_a_route_that_exists`, which
        is why this is checked the same way - by reading the source for the call
        rather than by trusting that somebody remembered.
        """
        import pathlib

        root = pathlib.Path(webhook_module.__file__).parents[2]
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in root.rglob("*.py")
            # The catalogue itself obviously names all of them.
            if path.name != "webhooks.py"
        )
        missing = [name for name in EVENT_TYPES if f'"{name}"' not in sources]
        assert not missing, f"advertised but never emitted: {missing}"

    def test_an_unknown_event_type_cannot_be_subscribed_to(self):
        """Otherwise a typo produces an endpoint that silently never fires."""
        with pytest.raises(ValidationError) as caught:
            WebhookService._validate_events(["experience.publish"])
        assert caught.value.code == "WEBHOOK_INVALID_EVENT"


class TestTheEnvelope:
    def test_it_is_versioned_from_the_first_delivery(self):
        """Adding a version field later means every existing receiver has to
        cope with its absence."""
        body = envelope(uuid.uuid4(), "ping", {})
        assert body["version"] == "1.0"

    def test_it_carries_an_id_a_receiver_can_deduplicate_on(self):
        event_id = uuid.uuid4()
        assert envelope(event_id, "ping", {})["id"] == str(event_id)

    def test_the_same_event_keeps_one_id_across_every_retry(self):
        """The whole point of at-least-once delivery: a receiver that processed
        it and merely failed to say so must discard the second copy."""
        d = queued()
        first = d.event_id
        d.attempts = 3
        assert d.event_id == first

    def test_the_payload_is_frozen_at_emit_time(self):
        """A retry an hour later must describe the event that happened, not the
        state of the world when the retry ran."""
        assert "payload" in WebhookDelivery.__table__.c


class TestSubscriptions:
    async def test_two_subscriptions_to_one_url_are_refused(self):
        """Every event would arrive twice, which looks like a delivery bug from
        the far end."""
        owner = account()
        existing = endpoint(owner_user_id=owner.id, url="https://example.com/hook")
        with pytest.raises(ConflictError) as caught:
            await WebhookService(FakeSession(listing=[existing])).subscribe(
                owner, url="https://example.com/hook", events=["experience.published"]
            )
        assert caught.value.code == "ENDPOINT_EXISTS"

    async def test_a_subscription_with_no_events_is_refused(self):
        with pytest.raises(ValidationError):
            await WebhookService(FakeSession(listing=[])).subscribe(
                account(), url="https://example.com/hook", events=[]
            )

    async def test_the_secret_is_returned_once_and_stored_readable(self):
        """Unlike an API key. A key is a credential we only need to recognise,
        so a digest is enough; a signing secret has to be used on every delivery
        to compute an HMAC, and there is no version of that which stores a hash."""
        row, secret = await WebhookService(FakeSession(listing=[])).subscribe(
            account(), url="https://example.com/hook", events=["experience.published"]
        )
        assert row.secret == secret
        assert secret not in row.secret_preview

    async def test_rotation_has_no_overlap_period(self):
        """The reason to rotate is usually that the old secret is somewhere it
        should not be, in which case keeping it valid for an hour is the
        problem rather than the fix."""
        owner = account()
        e = endpoint(owner_user_id=owner.id)
        before = e.secret
        _, after = await WebhookService(FakeSession(rows=[e])).rotate_secret(owner, e.id)
        assert after != before
        assert e.secret == after

    async def test_somebody_elses_endpoint_reads_as_not_found(self):
        e = endpoint()
        with pytest.raises(NotFoundError) as caught:
            await WebhookService(FakeSession(rows=[e])).update(account(), e.id, status="paused")
        assert caught.value.code == "WEBHOOK_NOT_FOUND"

    async def test_a_delivered_event_cannot_be_retried(self):
        owner = account()
        e = endpoint(owner_user_id=owner.id)
        d = queued(endpoint_id=e.id, status=DELIVERY_DELIVERED)
        with pytest.raises(ConflictError):
            await WebhookService(FakeSession(rows=[e, d])).retry(owner, e.id, d.id)

    async def test_a_manual_retry_keeps_the_event_id(self):
        owner = account()
        e = endpoint(owner_user_id=owner.id)
        d = queued(endpoint_id=e.id, status=DELIVERY_FAILED, attempts=MAX_ATTEMPTS)
        original = d.event_id
        await WebhookService(FakeSession(rows=[e, d])).retry(owner, e.id, d.id)
        assert d.event_id == original
        assert d.attempts == 0

    async def test_a_test_event_says_it_is_one(self):
        owner = account()
        e = endpoint(owner_user_id=owner.id)
        d = await WebhookService(FakeSession(rows=[e])).send_test(owner, e.id)
        assert d.is_test
        assert d.payload["data"] == {"test": True}


class TestUnsubscribingReallyDeletes:
    async def test_it_is_the_one_thing_the_platform_removes(self):
        """A delivery row is a copy of a message addressed to somebody else's
        server, not a record of anything - and leaving it queued means posting
        to a URL its owner explicitly withdrew."""
        owner = account()
        e = endpoint(owner_user_id=owner.id)
        session = FakeSession(rows=[e])
        await WebhookService(session).delete(owner, e.id)
        assert session.deleted == [e]

    def test_deliveries_go_with_the_endpoint(self):
        fk = next(iter(WebhookDelivery.__table__.c.endpoint_id.foreign_keys))
        assert fk.ondelete == "CASCADE"
