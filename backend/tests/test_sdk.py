"""Generated client libraries (spec DEV-002).

A generator is only as trustworthy as the thing that checks its output, and the
ways this one can fail are quiet. It can emit a client for endpoints a key
cannot call, so every method 401s and the partner blames their key. It can name
a type that was never defined, so the file does not compile and the SDK is
returned unused. It can order two same-typed path parameters differently from
the URL, which nothing catches - not the compiler, not the API, not the caller.

So the tests here mostly read the generated source rather than the generator,
and the retry tests run it: the Python client is executed and driven against a
fake transport, because "retries on 429" written in a docstring is not a
behaviour.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
import zipfile
from io import BytesIO

import pytest

from app.domains.developer.keys import ANY_SCOPE, SCOPES
from app.domains.developer.sdk import LANGUAGES, build, camel, type_name, version_of
from app.domains.developer.surface import KEY_HEADER, describe, scoped_routes
from app.main import app


class _FakeResponse:
    """The little that `urllib.request.urlopen` actually promises."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


@pytest.fixture(scope="module")
def schema():
    return app.openapi()


@pytest.fixture(scope="module")
def operations(schema):
    return describe(schema, app)


@pytest.fixture(scope="module")
def bundles():
    return {language: build(app, language) for language in LANGUAGES}


class TestItCoversTheRightEndpoints:
    def test_every_key_accessible_endpoint_has_a_method(self, operations):
        """The whole promise. An endpoint a key can call but the SDK cannot is
        one a partner writes by hand, next to eleven generated ones."""
        described = {(o.method, o.path) for o in operations}
        assert described == set(scoped_routes(app))

    def test_nothing_a_key_cannot_call_is_included(self, operations, schema):
        """A method for a session-only endpoint returns 401 every time, and the
        partner cannot tell that from a bad key. Spec 55.03 s49: the SDK must
        not offer functionality the underlying API does not give it."""
        every_path = set(schema["paths"])
        offered = {o.path for o in operations}
        assert offered < every_path, "the SDK is offering the entire API"

        allowed = set(SCOPES) | {ANY_SCOPE}
        assert {o.scope for o in operations} <= allowed

    def test_the_surface_is_not_written_down_anywhere(self):
        """Derived from the routes, so adding an endpoint adds it to the SDK.
        A hand-kept list is correct until the next route, and then silently
        incomplete."""
        import inspect

        from app.domains.developer import surface

        source = inspect.getsource(surface.scoped_routes)
        assert "iter_route_contexts" in source

    def test_the_scope_reaches_the_published_description(self, schema):
        """Otherwise the only way to learn an endpoint needs
        `experiences:write` is to be refused after writing the integration."""
        publish = schema["paths"]["/api/v1/posts/{experience_id}/publish"]["post"]
        assert publish["x-mado-scope"] == "experiences:write"

    def test_and_session_only_endpoints_are_left_unmarked(self, schema):
        assert "x-mado-scope" not in schema["paths"]["/api/v1/notifications"]["get"]


class TestArgumentsMatchTheUrl:
    def test_path_parameters_come_in_the_order_the_path_reads(self, operations):
        """The bug this exists for: `/posts/{experience_id}/events/{event_id}`
        sorted alphabetically generates `cancel(eventId, experienceId)`. Both
        are UUID strings, so swapping them type-checks, reaches the API, and
        comes back as "that listing does not exist"."""
        for operation in operations:
            positions = [
                operation.path.index("{" + p.name + "}")
                for p in operation.path_parameters
            ]
            assert positions == sorted(positions), operation.path

    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_the_worst_case_really_is_generated_in_order(self, bundles, language):
        source = bundles[language].files["mado.ts" if language == "typescript" else "mado.py"]
        signature = re.search(r"(cancelEvent|def cancel_event)\((.*?)\)", source)
        assert signature, "the two-parameter endpoint disappeared"
        arguments = signature.group(2)
        assert arguments.index("xperience") < arguments.index("vent_id" if "def " in
            signature.group(1) else "ventId")


class TestTheGeneratedSourceHoldsTogether:
    def test_every_type_the_python_client_names_is_defined(self, bundles):
        """A generated file that references a type it never emitted does not
        run, and the failure lands on the partner rather than on us."""
        source = bundles["python"].files["mado.py"]
        defined = set(re.findall(r"^class (\w+)", source, re.M))
        defined |= set(re.findall(r"^(\w+) = ", source, re.M))
        builtins = {"None", "Any", "float", "int", "str", "bool", "list", "dict"}
        # Only the public methods: the private helpers return the client's own
        # plumbing types, which are hand-written above and not generated.
        used = set(re.findall(r"^    def [a-z]\w*\(self[^)]*\) -> ([\w.\[\]]+):", source, re.M))
        assert used - defined - builtins == set()

    def test_every_type_the_typescript_client_names_is_defined(self, bundles):
        source = bundles["typescript"].files["mado.ts"]
        defined = set(re.findall(r"^export (?:interface|type) (\w+)", source, re.M))
        # `async name(...)` only - `private request<T>` returns its own generic.
        used = set(re.findall(r"^  async \w+\([^)]*\): Promise<(\w+)>", source, re.M))
        assert used, "no methods were generated at all"
        assert used - defined - {"void"} == set()

    def test_the_python_client_compiles(self, bundles):
        compile(bundles["python"].files["mado.py"], "mado.py", "exec")

    def test_the_python_example_compiles(self, bundles):
        compile(bundles["python"].files["example.py"], "example.py", "exec")

    def test_types_are_named_the_way_somebody_would_type_them(self):
        """FastAPI names generic models `Envelope_OwnExperienceOut_`. Left
        alone, that trailing underscore ends up in a partner's annotations."""
        assert type_name("CollectionEnvelope_OwnExperienceOut_") == (
            "CollectionEnvelopeOwnExperienceOut"
        )
        assert camel("list_my_posts") == "listMyPosts"

    def test_only_the_models_the_sdk_returns_are_included(self, bundles, schema):
        """The whole components block would put the moderation queue and the
        admin payloads into a partner's type definitions."""
        source = bundles["typescript"].files["mado.ts"]
        emitted = len(re.findall(r"^export (?:interface|type) ", source, re.M))
        assert emitted < len(schema["components"]["schemas"]) / 2


class TestAuthentication:
    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_the_key_travels_in_a_header(self, bundles, language):
        client = bundles[language].files["mado.ts" if language == "typescript" else "mado.py"]
        assert KEY_HEADER in client

    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_and_never_in_the_url(self, bundles, language):
        """A query string is written to proxy logs, browser history and
        load-balancer traces. A credential in one is published."""
        checked = 0
        for name, body in bundles[language].files.items():
            for line in body.splitlines():
                # Every line that builds a URL or a query string, checked for
                # the credential. Nothing else can put it in one.
                builds_url = any(
                    token in line
                    for token in ("searchParams", "urlencode", "url =", "new URL", "self._base_url")
                )
                if builds_url:
                    checked += 1
                    assert "apiKey" not in line and "api_key" not in line, f"{name}: {line}"
        # Otherwise a renamed variable turns this into a test that inspects
        # nothing and passes for it.
        assert checked, "no URL-building line was found to check"

    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_a_client_without_a_key_refuses_to_be_built(self, bundles, language):
        client = bundles[language].files["mado.ts" if language == "typescript" else "mado.py"]
        assert "An API key is required." in client


class TestRetryingIsRealBehaviour:
    """Run the generated Python client rather than read it.

    "Retries on 429" in a docstring is a claim. These drive the emitted client
    against a fake transport, so the claim has to be true of the code that
    partners actually receive.
    """

    def client(self, bundles, **kwargs):
        namespace: dict = {}
        exec(compile(bundles["python"].files["mado.py"], "mado.py", "exec"), namespace)
        instance = namespace["MadoClient"]("secret-key", base_url="https://x.test", **kwargs)
        return instance, namespace

    def responder(self, monkeypatch, statuses):
        """Answer with each status in turn, recording every attempt."""
        calls: list[str] = []
        remaining = list(statuses)

        def fake_urlopen(request, timeout=None):
            calls.append(request.get_method())
            status = remaining.pop(0) if remaining else 200
            if status >= 400:
                raise urllib.error.HTTPError(
                    request.full_url, status, "nope", {"Retry-After": "0"}, BytesIO(b"{}")
                )
            return _FakeResponse(b'{"data": []}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return calls

    def test_a_rate_limit_is_retried(self, bundles, monkeypatch):
        calls = self.responder(monkeypatch, [429, 200])
        instance, _ = self.client(bundles)
        instance.list_my_posts()
        assert len(calls) == 2

    def test_a_rate_limited_write_is_retried_too(self, bundles, monkeypatch):
        """429 means the request was refused before it was processed, so
        repeating it cannot repeat an effect."""
        calls = self.responder(monkeypatch, [429, 200])
        instance, _ = self.client(bundles)
        instance.publish_post("00000000-0000-0000-0000-000000000000")
        assert len(calls) == 2

    def test_a_server_error_is_retried_for_a_read(self, bundles, monkeypatch):
        calls = self.responder(monkeypatch, [503, 200])
        instance, _ = self.client(bundles)
        instance.list_my_posts()
        assert len(calls) == 2

    def test_but_never_for_a_write(self, bundles, monkeypatch):
        """A failed POST may have applied before it failed. Repeating it
        creates a second listing, and a duplicate nobody asked for is worse
        than an error the caller can see and decide about."""
        calls = self.responder(monkeypatch, [503, 200])
        instance, namespace = self.client(bundles)
        with pytest.raises(namespace["MadoError"]):
            instance.publish_post("00000000-0000-0000-0000-000000000000")
        assert len(calls) == 1

    def test_a_refusal_is_never_retried(self, bundles, monkeypatch):
        """A 403 fails the same way every time; retrying only delays the
        error."""
        calls = self.responder(monkeypatch, [403])
        instance, namespace = self.client(bundles)
        with pytest.raises(namespace["MadoError"]):
            instance.list_my_posts()
        assert len(calls) == 1

    def test_it_gives_up_rather_than_retrying_forever(self, bundles, monkeypatch):
        calls = self.responder(monkeypatch, [429, 429, 429, 429, 429])
        instance, namespace = self.client(bundles)
        with pytest.raises(namespace["MadoError"]):
            instance.list_my_posts()
        assert len(calls) == 3

    def test_the_error_carries_the_request_id(self, bundles, monkeypatch):
        """It is the fastest way to find the call in our logs, and a partner
        who cannot quote one gets a slower answer."""
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "nope",
                {"X-Request-ID": "req_abc123"},
                BytesIO(b'{"error": {"code": "SCOPE_REQUIRED", "message": "no"}}'),
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        instance, namespace = self.client(bundles)
        with pytest.raises(namespace["MadoError"]) as caught:
            instance.list_my_posts()
        assert caught.value.request_id == "req_abc123"
        assert caught.value.code == "SCOPE_REQUIRED"


class TestPackaging:
    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_the_download_carries_documentation_and_an_example(self, bundles, language):
        """Spec 55.02 s28. A client library with no worked example is one every
        partner starts by guessing at."""
        names = set(bundles[language].files)
        assert "README.md" in names
        assert any(name.startswith("example.") for name in names)

    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_the_readme_lists_the_scopes_each_call_needs(self, bundles, language):
        readme = bundles[language].files["README.md"]
        for scope in SCOPES:
            assert scope in readme

    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_the_same_api_produces_the_same_bytes(self, language):
        """Otherwise a partner diffing yesterday's download against today's
        sees every file change and stops diffing."""
        assert build(app, language).archive() == build(app, language).archive()

    def test_the_archive_opens(self, bundles):
        with zipfile.ZipFile(BytesIO(bundles["python"].archive())) as archive:
            assert archive.testzip() is None
            assert any(name.endswith("mado.py") for name in archive.namelist())

    def test_an_unknown_language_is_refused(self):
        with pytest.raises(KeyError):
            build(app, "cobol")


class TestVersioning:
    def test_the_version_names_the_api_it_was_built_from(self, operations):
        assert version_of(operations, "1.0.0").startswith("1.0.0+")

    def test_a_changed_endpoint_changes_the_version(self, operations):
        """So a partner can tell whether the copy in their repository is stale
        without diffing it."""
        fewer = operations[:-1]
        assert version_of(operations, "1.0.0") != version_of(fewer, "1.0.0")

    def test_a_reworded_description_does_not(self, operations):
        """A version that changes when a sentence is edited teaches partners to
        ignore version changes."""
        import copy

        edited = copy.deepcopy(operations)
        edited[0].description = "Completely different prose, same endpoint."
        assert version_of(operations, "1.0.0") == version_of(edited, "1.0.0")
