"""Client libraries, generated from the API's own description (spec DEV-002).

Spec 55.03 s49 is explicit that an SDK "should not introduce functionality
unavailable through the underlying API", and the implementation plan draws the
arrow as *API Specification -> SDK Generator*. So these are generated, not
written. A hand-written client is a second description of the API that agrees
with the first only on the day it is written; the ways it then drifts are all
silent, and all discovered by a partner rather than by us.

What is generated covers exactly the surface an API key can reach - see
:mod:`app.domains.developer.surface`. A client with methods for endpoints a key
can never call is worse than one without them, because every 401 then looks
like a credentials problem.

Two languages. TypeScript because the platform is a web platform and that is
what partners integrating with it write; Python because it is what the scripts
doing scheduled imports are written in. Both are dependency-free - TypeScript
on `fetch`, Python on the standard library - because asking a partner to adopt
a transitive dependency tree to call six endpoints is how an SDK ends up
reimplemented with `curl`.

Spec 55.02 s28 asks that official SDKs mirror API capabilities, be versioned,
provide typed models, support authentication, include retry logic,
documentation and working examples. Each of those is a section below.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any

from app.domains.developer.keys import ANY_SCOPE, SCOPES
from app.domains.developer.surface import (
    KEY_HEADER,
    Operation,
    describe,
    referenced_models,
)

# How many times a generated client retries, and how long it waits. Kept here
# rather than in the templates so both languages promise the same thing - an
# SDK whose behaviour depends on which one you picked is a support conversation
# waiting to happen.
MAX_ATTEMPTS = 3
BACKOFF_BASE_MS = 250
BACKOFF_CAP_MS = 8000


@dataclass(slots=True)
class Bundle:
    """A downloadable SDK: several files and the version they describe."""

    language: str
    label: str
    version: str
    filename: str
    files: dict[str, str] = field(default_factory=dict)

    def archive(self) -> bytes:
        """Zip the files, deterministically.

        A fixed timestamp on every entry so the same API produces byte-identical
        downloads. Otherwise a partner diffing yesterday's SDK against today's
        sees every file as changed and stops diffing.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(self.files):
                info = zipfile.ZipInfo(f"mado-sdk-{self.language}/{name}", (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, self.files[name])
        return buffer.getvalue()


# --------------------------------------------------------------- naming


def type_name(raw: str) -> str:
    """A schema name a human would type.

    FastAPI names generic models by gluing the parameter on with underscores -
    `CollectionEnvelope_OwnExperienceOut_`. Left alone that trailing underscore
    ends up in a partner's own type annotations forever.
    """
    parts = [part for part in raw.split("_") if part]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def camel(raw: str) -> str:
    head, *rest = raw.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _wrap(text: str, width: int = 74) -> list[str]:
    """Break a description into lines without a dependency on `textwrap`'s
    paragraph handling, which collapses the blank lines these docstrings use."""
    lines: list[str] = []
    for paragraph in (text or "").splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            if current and len(current) + len(word) + 1 > width:
                lines.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            lines.append(current)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _scope_note(operation: Operation) -> str:
    if operation.scope == ANY_SCOPE:
        return "Any valid key may call this."
    return f"Requires the `{operation.scope}` scope."


# ----------------------------------------------------------- TypeScript


def _ts_type(schema: dict[str, Any] | None) -> str:
    if not schema:
        return "unknown"

    ref = schema.get("$ref")
    if isinstance(ref, str):
        return type_name(ref.rsplit("/", 1)[-1])

    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"]) or "unknown"

    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [_ts_type(item) for item in schema[key]]
            # `{"type": "null"}` is how an optional field arrives.
            parts = [p for p in parts if p != "null"] + (["null"] if "null" in parts else [])
            return " | ".join(dict.fromkeys(parts)) or "unknown"

    if "allOf" in schema:
        parts = [_ts_type(item) for item in schema["allOf"]]
        return " & ".join(dict.fromkeys(parts)) or "unknown"

    kind = schema.get("type")
    if kind == "array":
        inner = _ts_type(schema.get("items"))
        return f"Array<{inner}>"
    if kind == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"Record<string, {_ts_type(extra)}>"
        return "Record<string, unknown>"
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(kind, "unknown")


def _ts_models(models: dict[str, Any]) -> str:
    out: list[str] = []
    for raw_name, schema in models.items():
        name = type_name(raw_name)
        title = schema.get("description") or schema.get("title") or ""
        if title:
            out.append("/**")
            out.extend(f" * {line}" if line else " *" for line in _wrap(title))
            out.append(" */")

        if "enum" in schema:
            out.append(f"export type {name} = {_ts_type(schema)}\n")
            continue

        properties = schema.get("properties") or {}
        if not properties:
            out.append(f"export type {name} = {_ts_type(schema)}\n")
            continue

        required = set(schema.get("required") or [])
        out.append(f"export interface {name} {{")
        for prop, prop_schema in properties.items():
            note = prop_schema.get("description")
            if note:
                out.extend(f"  /** {line} */" for line in _wrap(note, 70)[:1])
            optional = "" if prop in required else "?"
            out.append(f"  {json.dumps(prop)}{optional}: {_ts_type(prop_schema)}")
        out.append("}\n")
    return "\n".join(out)


def _ts_method(operation: Operation) -> str:
    args: list[str] = []
    for parameter in operation.path_parameters:
        args.append(f"{camel(parameter.identifier)}: {_ts_type(parameter.schema)}")

    query = operation.query_parameters
    if query:
        fields = ", ".join(
            f"{json.dumps(p.name)}{'' if p.required else '?'}: {_ts_type(p.schema)}"
            for p in query
        )
        required_query = any(p.required for p in query)
        args.append(f"query{'' if required_query else '?'}: {{ {fields} }}")

    if operation.has_body:
        args.append(f"body: {_ts_type(operation.request_schema)}")

    returns = _ts_type(operation.response_schema) if operation.response_schema else "void"

    path = operation.path
    for parameter in operation.path_parameters:
        path = path.replace(f"{{{parameter.name}}}", f"${{{camel(parameter.identifier)}}}")

    doc = [line for line in [operation.summary, "", operation.description] if line is not None]
    lines = ["  /**"]
    for line in _wrap("\n".join(doc).strip()):
        lines.append(f"   * {line}" if line else "   *")
    lines.append("   *")
    lines.append(f"   * {_scope_note(operation)}")
    lines.append("   */")
    lines.append(
        f"  async {camel(operation.name)}({', '.join(args)}): Promise<{returns}> {{"
    )
    lines.append(f"    return this.request({json.dumps(operation.method.upper())}, `{path}`, {{")
    lines.append(f"      query: {'query' if query else 'undefined'},")
    lines.append(f"      body: {'body' if operation.has_body else 'undefined'},")
    lines.append("    })")
    lines.append("  }")
    return "\n".join(lines)


def _typescript(operations: list[Operation], models: dict[str, Any], version: str) -> str:
    methods = "\n\n".join(_ts_method(operation) for operation in operations)
    return f"""/**
 * Mado API client for TypeScript.
 *
 * Generated from the Mado OpenAPI description - do not edit by hand. Anything
 * changed here is lost the next time the SDK is downloaded, and the API is the
 * thing that decides what is true.
 *
 * SDK version {version}
 */

export class MadoError extends Error {{
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly retryAfter: number | null

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null,
    retryAfter: number | null,
  ) {{
    super(message)
    this.name = 'MadoError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.retryAfter = retryAfter
  }}
}}

export interface MadoClientOptions {{
  /** Your key, from the developer page. Sent as the {KEY_HEADER} header. */
  apiKey: string
  /** Defaults to the production API. */
  baseUrl?: string
  /** Supply your own for tests, or for a runtime without a global fetch. */
  fetch?: typeof fetch
  /** Total attempts per call, including the first. Defaults to {MAX_ATTEMPTS}. */
  maxAttempts?: number
  /** Abort a single attempt after this long. Defaults to 30000. */
  timeoutMs?: number
}}

{_ts_models(models)}
const DEFAULT_BASE_URL = 'https://api.mado.et'

function sleep(ms: number): Promise<void> {{
  return new Promise((resolve) => setTimeout(resolve, ms))
}}

export class MadoClient {{
  private readonly apiKey: string
  private readonly baseUrl: string
  private readonly fetchImpl: typeof fetch
  private readonly maxAttempts: number
  private readonly timeoutMs: number

  constructor(options: MadoClientOptions) {{
    if (!options.apiKey) throw new Error('An API key is required.')
    this.apiKey = options.apiKey
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\\/+$/, '')
    this.fetchImpl = options.fetch ?? globalThis.fetch
    this.maxAttempts = options.maxAttempts ?? {MAX_ATTEMPTS}
    this.timeoutMs = options.timeoutMs ?? 30000
  }}

  /**
   * One request, with retries.
   *
   * A 429 is always retried: the request was refused before it was processed,
   * so repeating it cannot repeat an effect. A 5xx is retried only for GET,
   * because a failed POST may have applied before the failure and repeating it
   * would create a second listing - a duplicate nobody asked for is worse than
   * an error the caller can see and decide about.
   *
   * `Retry-After` is honoured when the server sends one. It knows when the
   * window opens; a client guessing shorter simply gets refused again.
   */
  private async request<T>(
    method: string,
    path: string,
    options: {{ query?: Record<string, unknown>; body?: unknown }} = {{}},
  ): Promise<T> {{
    const url = new URL(this.baseUrl + path)
    for (const [key, value] of Object.entries(options.query ?? {{}})) {{
      if (value !== undefined && value !== null) url.searchParams.set(key, String(value))
    }}

    let lastError: unknown
    for (let attempt = 1; attempt <= this.maxAttempts; attempt += 1) {{
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), this.timeoutMs)
      try {{
        const response = await this.fetchImpl(url.toString(), {{
          method,
          headers: {{
            '{KEY_HEADER}': this.apiKey,
            'Content-Type': 'application/json',
            Accept: 'application/json',
          }},
          body: options.body === undefined ? undefined : JSON.stringify(options.body),
          signal: controller.signal,
        }})

        if (response.ok) {{
          if (response.status === 204) return undefined as T
          return (await response.json()) as T
        }}

        const retryAfterHeader = response.headers.get('Retry-After')
        const retryAfter = retryAfterHeader ? Number(retryAfterHeader) : null
        const payload = await response.json().catch(() => ({{}}))
        const error = new MadoError(
          response.status,
          payload?.error?.code ?? 'UNKNOWN',
          payload?.error?.message ?? response.statusText,
          response.headers.get('X-Request-ID'),
          Number.isFinite(retryAfter) ? retryAfter : null,
        )

        const retryable =
          response.status === 429 || (response.status >= 500 && method === 'GET')
        if (!retryable || attempt === this.maxAttempts) throw error
        await sleep(error.retryAfter ? error.retryAfter * 1000 : this.backoff(attempt))
        lastError = error
      }} catch (caught) {{
        if (caught instanceof MadoError) {{
          if (attempt === this.maxAttempts) throw caught
          lastError = caught
          continue
        }}
        // A transport failure or timeout: nothing was answered, so the same
        // rule applies as for a 5xx.
        if (method !== 'GET' || attempt === this.maxAttempts) throw caught
        lastError = caught
        await sleep(this.backoff(attempt))
      }} finally {{
        clearTimeout(timer)
      }}
    }}
    throw lastError
  }}

  /** Exponential, with jitter so a fleet of clients does not retry in step. */
  private backoff(attempt: number): number {{
    const ceiling = Math.min({BACKOFF_CAP_MS}, {BACKOFF_BASE_MS} * 2 ** (attempt - 1))
    return Math.random() * ceiling
  }}

{methods}
}}
"""


# --------------------------------------------------------------- Python


def _py_type(schema: dict[str, Any] | None) -> str:
    if not schema:
        return "Any"

    ref = schema.get("$ref")
    if isinstance(ref, str):
        return f'"{type_name(ref.rsplit("/", 1)[-1])}"'

    if "enum" in schema:
        values = ", ".join(repr(value) for value in schema["enum"])
        return f"Literal[{values}]" if values else "Any"

    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [_py_type(item) for item in schema[key]]
            parts = [p for p in parts if p != "None"] + (["None"] if "None" in parts else [])
            return " | ".join(dict.fromkeys(parts)) or "Any"

    if "allOf" in schema:
        # No intersection type in Python; the first reference is the useful one.
        return _py_type(schema["allOf"][0]) if schema["allOf"] else "Any"

    kind = schema.get("type")
    if kind == "array":
        return f"list[{_py_type(schema.get('items'))}]"
    if kind == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"dict[str, {_py_type(extra)}]"
        return "dict[str, Any]"
    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }.get(kind, "Any")


def _py_models(models: dict[str, Any]) -> str:
    out: list[str] = []
    for raw_name, schema in models.items():
        name = type_name(raw_name)
        properties = schema.get("properties") or {}

        if "enum" in schema or not properties:
            out.append(f"{name} = {_py_type(schema).strip(chr(34))}")
            out.append("")
            continue

        required = set(schema.get("required") or [])
        # `total=False` with explicit Required[...] rather than two classes:
        # most fields on these payloads are optional, and a partner reading the
        # file should see the whole shape in one place.
        out.append(f"class {name}(TypedDict, total=False):")
        description = schema.get("description") or schema.get("title")
        if description:
            out.append('    """')
            out.extend(f"    {line}" for line in _wrap(description, 70))
            out.append('    """')
            out.append("")
        for prop, prop_schema in properties.items():
            rendered = _py_type(prop_schema)
            if prop in required:
                rendered = f"Required[{rendered}]"
            out.append(f"    {prop}: {rendered}")
        out.append("")
    return "\n".join(out)


def _py_method(operation: Operation) -> str:
    args = ["self"]
    for parameter in operation.path_parameters:
        args.append(f"{parameter.identifier}: {_py_type(parameter.schema).strip(chr(34))}")
    if operation.has_body:
        args.append(f"body: {_py_type(operation.request_schema).strip(chr(34))}")

    query = operation.query_parameters
    if query:
        args.append("*")
        for parameter in sorted(query, key=lambda p: not p.required):
            rendered = _py_type(parameter.schema).strip(chr(34))
            if parameter.required:
                args.append(f"{parameter.identifier}: {rendered}")
                continue
            # An optional query parameter is usually already nullable in the
            # schema, and appending unconditionally produced `str | None | None`.
            # Valid Python, but a partner reading it reasonably wonders what
            # the generator did not understand about their API.
            nullable = "None" in [part.strip() for part in rendered.split("|")]
            suffix = "" if nullable else " | None"
            args.append(f"{parameter.identifier}: {rendered}{suffix} = None")

    returns = (
        _py_type(operation.response_schema).strip(chr(34))
        if operation.response_schema
        else "None"
    )

    path = operation.path
    for parameter in operation.path_parameters:
        path = path.replace(f"{{{parameter.name}}}", f"{{{parameter.identifier}}}")

    lines = [f"    def {operation.name}({', '.join(args)}) -> {returns}:"]
    doc = "\n".join(
        line for line in [operation.summary, "", operation.description] if line is not None
    ).strip()
    lines.append('        """' + (_wrap(doc)[0] if doc else operation.name))
    for line in _wrap(doc)[1:]:
        lines.append(f"        {line}" if line else "")
    lines.append("")
    lines.append(f"        {_scope_note(operation)}")
    lines.append('        """')

    if query:
        pairs = ", ".join(f'"{p.name}": {p.identifier}' for p in query)
        lines.append(f"        query = {{{pairs}}}")
    lines.append(
        f'        return self._request("{operation.method.upper()}", f"{path}",'
        f' query={"query" if query else "None"},'
        f' body={"body" if operation.has_body else "None"})'
    )
    return "\n".join(lines)


def _python(operations: list[Operation], models: dict[str, Any], version: str) -> str:
    methods = "\n\n".join(_py_method(operation) for operation in operations)
    return f'''"""Mado API client for Python.

Generated from the Mado OpenAPI description - do not edit by hand. Anything
changed here is lost the next time the SDK is downloaded, and the API is the
thing that decides what is true.

Standard library only, deliberately: a partner should be able to drop this file
into a scheduled script without adopting a dependency tree to call
{len(operations)} endpoints.

SDK version {version}
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal, Required, TypedDict

DEFAULT_BASE_URL = "https://api.mado.et"
KEY_HEADER = "{KEY_HEADER}"
MAX_ATTEMPTS = {MAX_ATTEMPTS}
BACKOFF_BASE_SECONDS = {BACKOFF_BASE_MS / 1000}
BACKOFF_CAP_SECONDS = {BACKOFF_CAP_MS / 1000}


class MadoError(Exception):
    """A refusal from the API, with everything needed to report it."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"[{{status}} {{code}}] {{message}}")
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after


{_py_models(models)}

class MadoClient:
    """Talks to the Mado API with an API key.

    The key travels in a header and never in the URL. Query strings are logged
    by proxies, browsers and load balancers, so a credential in one should be
    considered published the moment it is sent.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        max_attempts: int = MAX_ATTEMPTS,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("An API key is required.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max_attempts
        self._timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        """One request, with retries.

        A 429 is always retried: the request was refused before it was
        processed, so repeating it cannot repeat an effect. A 5xx is retried
        only for GET, because a failed POST may have applied before the failure
        and repeating it would create a second listing - a duplicate nobody
        asked for is worse than an error the caller can see and decide about.

        `Retry-After` is honoured when the server sends one. It knows when the
        window opens; a client guessing shorter simply gets refused again.
        """
        url = self._base_url + path
        params = {{k: v for k, v in (query or {{}}).items() if v is not None}}
        if params:
            url = f"{{url}}?{{urllib.parse.urlencode(params, doseq=True)}}"

        payload = None if body is None else json.dumps(body).encode()
        last: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            request = urllib.request.Request(url, data=payload, method=method)
            request.add_header(KEY_HEADER, self._api_key)
            request.add_header("Accept", "application/json")
            if payload is not None:
                request.add_header("Content-Type", "application/json")

            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    if response.status == 204:
                        return None
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                error = self._as_error(exc)
                retryable = exc.code == 429 or (exc.code >= 500 and method == "GET")
                if not retryable or attempt == self._max_attempts:
                    raise error from None
                time.sleep(error.retry_after or self._backoff(attempt))
                last = error
            except urllib.error.URLError as exc:
                # Nothing was answered, so the same rule applies as for a 5xx.
                if method != "GET" or attempt == self._max_attempts:
                    raise
                time.sleep(self._backoff(attempt))
                last = exc

        raise last if last else RuntimeError("unreachable")

    @staticmethod
    def _as_error(exc: urllib.error.HTTPError) -> MadoError:
        try:
            payload = json.loads(exc.read() or b"{{}}")
        except (ValueError, OSError):
            payload = {{}}
        detail = payload.get("error") or {{}}
        retry_after = exc.headers.get("Retry-After")
        return MadoError(
            exc.code,
            detail.get("code", "UNKNOWN"),
            detail.get("message", exc.reason or "Request failed"),
            exc.headers.get("X-Request-ID"),
            float(retry_after) if retry_after and retry_after.isdigit() else None,
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential, with jitter so a fleet of clients does not retry in step."""
        ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        return random.random() * ceiling

{methods}
'''


# ------------------------------------------------------------- packaging


def _scope_table(operations: list[Operation]) -> str:
    used = sorted({o.scope for o in operations if o.scope != ANY_SCOPE})
    rows = "\n".join(f"| `{scope}` | {SCOPES.get(scope, '')} |" for scope in used)
    return f"| Scope | What it allows |\n| --- | --- |\n{rows}"


def _endpoint_table(operations: list[Operation], naming) -> str:
    rows = "\n".join(
        f"| `{naming(o.name)}` | `{o.method.upper()} {o.path}` | "
        f"{'any key' if o.scope == ANY_SCOPE else '`' + o.scope + '`'} |"
        for o in operations
    )
    return f"| Method | Endpoint | Scope |\n| --- | --- | --- |\n{rows}"


def _readme(language: str, label: str, version: str, operations: list[Operation]) -> str:
    is_ts = language == "typescript"
    naming = camel if is_ts else (lambda name: name)
    install = (
        "Copy `mado.ts` into your project. It has no dependencies beyond a\n"
        "runtime `fetch` - Node 18+, Deno, Bun and every current browser have one."
        if is_ts
        else "Copy `mado.py` into your project. It uses the standard library only,\n"
        "and needs Python 3.11 or later."
    )
    example = "example.ts" if is_ts else "example.py"
    return f"""# Mado {label} SDK

Version {version}. Generated from the Mado API description - if this file and
the API disagree, the API is right and this SDK is out of date.

## Installing

{install}

## Authenticating

Create a key on the developer page and give it only the scopes it needs. The
key travels in the `{KEY_HEADER}` header, never in the URL: query strings are
written to proxy logs, browser history and load-balancer traces, so a
credential in one should be treated as published the moment it is sent.

Keep the key out of your source. Read it from the environment, as the example
does.

{_scope_table(operations)}

A key with the wrong scope is refused with `403 SCOPE_REQUIRED`, which names the
scope it wanted - you do not have to guess.

## What it can do

This SDK covers exactly the endpoints an API key can reach, and nothing else.
Anything the website can do that is missing here is missing on purpose: it needs
a signed-in person rather than a key.

{_endpoint_table(operations, naming)}

## Errors

Every failure raises `MadoError`, carrying the HTTP status, Mado's own error
code, the message, and the `X-Request-ID` of the request. Quote that request id
if you report a problem - it finds the exact call in our logs.

## Retries

Requests are retried up to {MAX_ATTEMPTS} times in total, with exponential
backoff and jitter.

- **429** is always retried. The request was refused before it was processed,
  so repeating it cannot repeat an effect. `Retry-After` is honoured when sent.
- **5xx and transport failures** are retried for `GET` only. A failed write may
  have applied before the failure, and a silently duplicated listing is worse
  than an error you can see.

Nothing else is retried. A 4xx will fail the same way every time.

## Versioning

The version above is the API version plus a short digest of the endpoint
surface. If you regenerate and the digest changes, the API surface changed.

## Example

See `{example}`.
"""


def _example(language: str, operations: list[Operation]) -> str:
    if language == "typescript":
        return """/** A first call, to check the key and the scopes are right. */

import { MadoClient, MadoError } from './mado'

const client = new MadoClient({
  apiKey: process.env.MADO_API_KEY ?? '',
  // Point at your own environment while developing.
  baseUrl: process.env.MADO_BASE_URL,
})

async function main() {
  // Cheapest possible check that the key works, before anything is written.
  const me = await client.whoami()
  console.log('key belongs to', me.data)

  const mine = await client.listMyPosts()
  console.log(`${mine.data.length} listings`)

  for (const post of mine.data) {
    console.log(' -', post.title, post.status)
  }
}

main().catch((error) => {
  if (error instanceof MadoError) {
    // The request id is the fastest way for us to find what happened.
    console.error(`${error.code}: ${error.message} (request ${error.requestId})`)
    process.exit(1)
  }
  throw error
})
"""
    return '''"""A first call, to check the key and the scopes are right."""

import os

from mado import MadoClient, MadoError

client = MadoClient(
    os.environ["MADO_API_KEY"],
    # Point at your own environment while developing.
    base_url=os.environ.get("MADO_BASE_URL", "https://api.mado.et"),
)

try:
    # Cheapest possible check that the key works, before anything is written.
    me = client.whoami()
    print("key belongs to", me["data"])

    mine = client.list_my_posts()
    print(len(mine["data"]), "listings")
    for post in mine["data"]:
        print(" -", post["title"], post["status"])
except MadoError as error:
    # The request id is the fastest way for us to find what happened.
    raise SystemExit(f"{error.code}: {error} (request {error.request_id})")
'''


LANGUAGES: dict[str, str] = {
    "typescript": "TypeScript",
    "python": "Python",
}


def version_of(operations: list[Operation], api_version: str) -> str:
    """The API version plus a digest of the surface it describes.

    Two SDKs built from the same endpoints get the same version, so a partner
    can tell whether a re-download is worth taking. The digest covers method,
    path and scope - the things whose change breaks a caller - rather than the
    whole schema, which shifts whenever a description is reworded.
    """
    material = "\n".join(f"{o.method} {o.path} {o.scope}" for o in operations)
    digest = hashlib.sha256(material.encode()).hexdigest()[:8]
    return f"{api_version}+{digest}"


def build(app, language: str) -> Bundle:
    """Generate the SDK for one language from the running application."""
    if language not in LANGUAGES:
        raise KeyError(language)

    schema = app.openapi()
    operations = describe(schema, app)
    models = referenced_models(operations, schema)
    version = version_of(operations, app.version)
    label = LANGUAGES[language]

    if language == "typescript":
        files = {
            "mado.ts": _typescript(operations, models, version),
            "example.ts": _example(language, operations),
        }
    else:
        files = {
            "mado.py": _python(operations, models, version),
            "example.py": _example(language, operations),
        }

    files["README.md"] = _readme(language, label, version, operations)
    return Bundle(
        language=language,
        label=label,
        version=version,
        filename=f"mado-sdk-{language}-{version.replace('+', '-')}.zip",
        files=files,
    )


def catalogue(app) -> list[dict[str, Any]]:
    """What is on offer, for the developer page to list."""
    bundles = [build(app, language) for language in LANGUAGES]
    return [
        {
            "language": bundle.language,
            "label": bundle.label,
            "version": bundle.version,
            "filename": bundle.filename,
            "files": sorted(bundle.files),
        }
        for bundle in bundles
    ]
