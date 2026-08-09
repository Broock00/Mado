"""Request tracing (spec OPERATIONS-42 §5, §10).

Where a request's time actually went, and a trace id that survives the hop from
the browser into the API.

**This is not distributed tracing, and does not pretend to be.** OpenTelemetry
with a collector is the right answer for a fleet of services calling each other.
Mado is one deployable (spec 70.02), so the interesting question is never "which
service was slow" - there is one - it is "which *dependency* was slow inside
this request", and that needs a timer and a contextvar rather than a collector,
an exporter and a sidecar. What is implemented is the part that has to be right
either way: W3C trace context is parsed and propagated, so the day this becomes
several services the ids already line up, and nothing about the format has to
change.

**Every request gets a timing breakdown.** A log line saying a request took
1,800ms tells you to go and look; a line saying 1,750ms of it was the model
gateway tells you what to do. Spans are accumulated by name rather than kept as
a tree: a tree of forty database calls is a profiler's output, and what an
operator needs at three in the morning is six numbers.

**It also goes back to the browser.** The breakdown is emitted as
`Server-Timing`, which browsers render in the network panel next to their own
measurements - so a slow page can be attributed to the server or ruled out
without anybody opening a dashboard.

**Never fails a request.** Every function here swallows its own errors. Timing
instrumentation that can raise is a way of turning a slow request into a broken
one.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from contextvars import ContextVar

from app.core import metrics
from app.core.logging import get_logger

logger = get_logger("mado.tracing")

TRACEPARENT_HEADER = "traceparent"

# version-traceid-spanid-flags, all lowercase hex. The all-zero trace id and
# span id are explicitly invalid in the spec, and a caller sending one is
# usually a misconfigured proxy rather than a real trace.
_TRACEPARENT = re.compile(
    r"^(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-"
    r"(?P<flags>[0-9a-f]{2})$"
)

_trace_id: ContextVar[str | None] = ContextVar("mado_trace_id", default=None)
_parent_span: ContextVar[str | None] = ContextVar("mado_parent_span", default=None)
_spans: ContextVar[dict[str, float] | None] = ContextVar("mado_spans", default=None)

# Longer than this and the access log line is promoted to a warning carrying the
# breakdown. One second is well past anything this API does when it is healthy,
# and rare enough that the warnings stay readable.
SLOW_REQUEST_SECONDS = 1.0

# A single request should not name dozens of different spans. If it does,
# something is generating span names from data - an experience id, a URL - which
# is the cardinality problem again, one request at a time.
MAX_SPANS = 24


def _random_hex(bytes_count: int) -> str:
    return os.urandom(bytes_count).hex()


def parse_traceparent(header: str | None) -> tuple[str, str] | None:
    """Return (trace_id, parent_span_id) from a W3C traceparent, or None."""
    if not header:
        return None
    match = _TRACEPARENT.match(header.strip())
    if match is None:
        return None
    trace_id, span_id = match.group("trace_id"), match.group("span_id")
    if set(trace_id) == {"0"} or set(span_id) == {"0"}:
        # Explicitly invalid in the specification. Treated as absent rather than
        # rejected: a broken upstream header should not fail the request.
        return None
    return trace_id, span_id


def start_request(traceparent: str | None) -> tuple[str, str]:
    """Begin a request's trace. Returns (trace_id, span_id).

    Continues an inbound trace where there is one, so a browser that started a
    trace and the API log line for the request it caused share an id.
    """
    inherited = parse_traceparent(traceparent)
    trace_id = inherited[0] if inherited else _random_hex(16)
    span_id = _random_hex(8)

    _trace_id.set(trace_id)
    _parent_span.set(span_id)
    _spans.set({})
    return trace_id, span_id


def current_trace_id() -> str | None:
    return _trace_id.get()


def traceparent_for_outbound() -> str | None:
    """The header to send on a call we make, so the far end can join this trace.

    Sampled flag is `01` throughout: nothing here samples, because the volume
    that makes sampling necessary is not the volume this platform has.
    """
    trace_id, span_id = _trace_id.get(), _parent_span.get()
    if not trace_id or not span_id:
        return None
    return f"00-{trace_id}-{span_id}-01"


def add_span(name: str, seconds: float) -> None:
    """Add an already-measured duration to this request's breakdown.

    For callers that own their own timer - the SQLAlchemy cursor events, which
    fire as callbacks and cannot wrap anything in a `with`.
    """
    try:
        store = _spans.get()
        if store is None:
            # Outside a request - a scheduled job, a test, a startup probe.
            # There is simply nowhere to hang it.
            return
        if name not in store and len(store) >= MAX_SPANS:
            return
        store[name] = store.get(name, 0.0) + seconds
    except Exception:  # noqa: BLE001 - never fail a request over a timer
        logger.debug("span_record_failed", span=name)


@contextlib.contextmanager
def span(name: str):
    """Time a named region and add it to this request's breakdown.

    Accumulating rather than nesting: three database calls in one request appear
    as one `db` total, which is the number an operator acts on. Re-entrant by
    construction - an inner span with the same name simply adds to the total,
    which is what you want when a helper is called from several places.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        add_span(name, time.perf_counter() - started)


@contextlib.contextmanager
def dependency(name: str):
    """Time an external call, count its outcome, and add it to the breakdown.

    One helper rather than three call sites, because the failure mode of manual
    instrumentation is a `try` that increments the success counter and an
    `except` that forgets to increment the failure one - so the dashboard says
    the dependency is healthy while it is down.
    """
    started = time.perf_counter()
    outcome = "ok"
    try:
        with span(name):
            yield
    except Exception:
        outcome = "error"
        raise
    finally:
        elapsed = time.perf_counter() - started
        metrics.dependency_calls.inc(name, outcome)
        metrics.dependency_duration.observe(elapsed, name)


def breakdown() -> dict[str, float]:
    """This request's spans, in milliseconds, largest first."""
    store = _spans.get() or {}
    return {
        name: round(seconds * 1000, 2)
        for name, seconds in sorted(store.items(), key=lambda item: -item[1])
    }


def server_timing_header() -> str | None:
    """The breakdown as a `Server-Timing` value, or None if nothing was timed.

    Metric names are restricted to a token by the specification, so anything a
    span name might contain that is not a letter, digit, dash or underscore is
    replaced rather than sent - an invalid header is dropped silently by the
    browser, which is the worst kind of broken.
    """
    parts = [
        f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)};dur={millis}"
        for name, millis in breakdown().items()
    ]
    return ", ".join(parts) if parts else None
