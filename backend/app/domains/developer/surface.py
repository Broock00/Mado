"""Which endpoints an API key can call, read off the application itself.

Two things need this answer and neither should be told it twice: the OpenAPI
document, which ought to say which scope an endpoint wants rather than leaving
a partner to discover it through a 403, and the SDK generator, which must cover
exactly the key-accessible surface and nothing else.

Derived rather than declared. A hand-written list of scoped endpoints is
correct until the next route is added, and the failure is quiet in the
direction that matters - a new endpoint silently missing from the SDK, or worse,
documented with the wrong scope. The scope is already recorded on the
dependency by :func:`app.api.deps.caller_with_scope`; this walks the routes and
reads it back.

Session-authenticated endpoints are deliberately excluded. Generating a client
method for an endpoint a key can never call produces an SDK whose methods
mostly return 401, and a partner cannot tell which of those is their mistake -
which is exactly the confusion spec 55.03 s49 rules out with "the SDK should
not introduce functionality unavailable through the underlying API".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.routing import APIRoute, iter_route_contexts

# The header a key travels in. One place, because it appears in the OpenAPI
# document, in every generated client, and in the examples in their READMEs.
KEY_HEADER = "X-Mado-Api-Key"

# The OpenAPI extension the scope is published under. `x-` prefixed so it is
# valid OpenAPI and survives any generator that respects extensions.
SCOPE_EXTENSION = "x-mado-scope"


def _scope_of(dependant) -> str | None:
    """The scope a route wants, found anywhere in its dependency tree.

    Recursive because scoped dependencies compose: publishing goes through
    `verified_experience_writer`, which depends on `ExperienceWriter`, which is
    the one carrying the tag.
    """
    scope = getattr(dependant.call, "mado_scope", None)
    if scope:
        return scope
    for child in dependant.dependencies:
        found = _scope_of(child)
        if found:
            return found
    return None


def _names(app) -> dict[tuple[str, str], str]:
    """``(method, path) -> endpoint function name``.

    Used for method names in generated clients in preference to the OpenAPI
    `operationId`, which FastAPI builds by gluing the path and verb onto the
    function name: `list_my_posts_api_v1_posts_get`. Nobody would write that in
    their own code, and an SDK people rename on import has failed at the one
    thing it is for.
    """
    names: dict[tuple[str, str], str] = {}
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.route, APIRoute):
            continue
        for method in context.methods or ():
            names[(method.lower(), context.path)] = context.name
    return names


def scoped_routes(app) -> dict[tuple[str, str], str]:
    """``(method, path) -> scope`` for everything a key can reach.

    Walked with FastAPI's own `iter_route_contexts` rather than by reading
    `app.routes` directly. Routers are included lazily, so the top-level list
    holds wrappers rather than endpoints - and more importantly this is the
    same enumeration the OpenAPI document is built from, so the paths here are
    the paths there. Matching them by hand means reconstructing prefixes, and a
    near-miss produces an SDK missing exactly the routes whose prefix was
    guessed wrong.
    """
    found: dict[tuple[str, str], str] = {}
    for context in iter_route_contexts(app.routes):
        route = context.route
        if not isinstance(route, APIRoute):
            continue
        scope = _scope_of(route.dependant)
        if scope is None:
            continue
        for method in context.methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            found[(method.lower(), context.path)] = scope
    return found


def stamp_scopes(schema: dict[str, Any], app) -> dict[str, Any]:
    """Write the scope of each key-accessible operation into the OpenAPI doc.

    So that the published specification answers "what does this key need"
    without a partner having to provoke a 403 to find out.
    """
    scopes = scoped_routes(app)
    for (method, path), scope in scopes.items():
        operation = (schema.get("paths", {}).get(path) or {}).get(method)
        if operation is not None:
            operation[SCOPE_EXTENSION] = scope
    return schema


@dataclass(slots=True)
class Parameter:
    name: str
    location: str  # path | query
    required: bool
    schema: dict[str, Any] = field(default_factory=dict)
    description: str | None = None

    @property
    def identifier(self) -> str:
        """A name usable as a variable in a generated client."""
        cleaned = "".join(ch if ch.isalnum() else "_" for ch in self.name)
        return cleaned.lstrip("_") or "value"


@dataclass(slots=True)
class Operation:
    """One method the generated client will have."""

    operation_id: str
    name: str
    method: str
    path: str
    scope: str
    summary: str
    description: str
    parameters: list[Parameter] = field(default_factory=list)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None

    @property
    def path_parameters(self) -> list[Parameter]:
        return [p for p in self.parameters if p.location == "path"]

    @property
    def query_parameters(self) -> list[Parameter]:
        return [p for p in self.parameters if p.location == "query"]

    @property
    def has_body(self) -> bool:
        return self.request_schema is not None


def _ref_name(schema: dict[str, Any] | None) -> str | None:
    if not schema:
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        return ref.rsplit("/", 1)[-1]
    return None


def describe(schema: dict[str, Any], app) -> list[Operation]:
    """Turn the OpenAPI document into the operations a client needs.

    Sorted by path then method so a regenerated SDK differs only where the API
    differs - a client library whose diff is dominated by reordering is one
    nobody will review before shipping.
    """
    scopes = scoped_routes(app)
    names = _names(app)
    operations: list[Operation] = []

    for (method, path), scope in scopes.items():
        raw = (schema.get("paths", {}).get(path) or {}).get(method)
        if raw is None:
            continue

        parameters = [
            Parameter(
                name=item["name"],
                location=item["in"],
                required=bool(item.get("required")),
                schema=item.get("schema") or {},
                description=item.get("description"),
            )
            for item in raw.get("parameters", [])
            # The key header is supplied by the client itself, so it is not an
            # argument the caller passes to every single method.
            if item["in"] in {"path", "query"} and item["name"].lower() != KEY_HEADER.lower()
        ]

        body = (
            raw.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )

        response = None
        for status in ("200", "201", "202"):
            candidate = (
                raw.get("responses", {})
                .get(status, {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            if candidate:
                response = candidate
                break

        operations.append(
            Operation(
                operation_id=raw.get("operationId") or f"{method}_{path}",
                name=names.get((method, path)) or f"{method}_{path}",
                method=method,
                path=path,
                scope=scope,
                summary=raw.get("summary") or "",
                description=(raw.get("description") or "").strip(),
                # Path parameters in the order they appear in the path, then
                # query parameters. Alphabetical order looks tidier and is
                # wrong: `/posts/{experience_id}/events/{event_id}/cancel`
                # would generate `cancel(eventId, experienceId)`, so a caller
                # passing them in the order the URL reads swaps two UUIDs that
                # are the same type. Nothing catches that - not the compiler,
                # not the API, which simply reports the first id as missing.
                parameters=sorted(
                    parameters,
                    key=lambda p: (
                        (0, path.find("{" + p.name + "}"))
                        if p.location == "path"
                        else (1, 0 if p.required else 1)
                    ),
                ),
                request_schema=body,
                response_schema=response,
            )
        )

    return sorted(operations, key=lambda o: (o.path, o.method))


def referenced_models(operations: list[Operation], schema: dict[str, Any]) -> dict[str, Any]:
    """Every component schema the given operations can reach, transitively.

    The whole `components.schemas` block would drag the entire platform's
    internals into a partner's type definitions - moderation queues, admin
    payloads, things a key cannot see. Following the references from the
    operations keeps the generated types to what the SDK actually returns.
    """
    components = schema.get("components", {}).get("schemas", {})
    wanted: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            name = _ref_name(node)
            if name and name not in wanted:
                wanted.add(name)
                visit(components.get(name))
            for key, value in node.items():
                if key != "$ref":
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    for operation in operations:
        visit(operation.request_schema)
        visit(operation.response_schema)

    return {name: components[name] for name in sorted(wanted) if name in components}
