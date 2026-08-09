"""Cross-cutting HTTP middleware (spec 55.01 §23, §32; OPERATIONS-42 §5).

One pass over every request, doing four things that all need the same timer:
assign correlation ids, record metrics, accumulate the timing breakdown, and
write the access log line.

They are together rather than in four middlewares because each additional layer
is another frame on every request and another place the ordering can be got
subtly wrong - and because they are genuinely one concern: what happened, how
long it took, and how to find it again.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core import metrics, tracing
from app.core.context import set_request_id
from app.core.logging import get_logger

logger = get_logger("mado.http")

REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"


def route_template(request: Request) -> str:
    """The matched route pattern, never the resolved path.

    `/api/v1/experiences/{experience_id}` rather than
    `/api/v1/experiences/9f1c.../`. One metric series per experience is the
    unbounded-label problem that takes a monitoring system down, and it arrives
    disguised as a perfectly reasonable path label.

    Falls back to `unmatched` rather than to the raw path: a 404 sweep from a
    vulnerability scanner would otherwise create a series per URL it guessed,
    which is the same failure with a more interesting cause.
    """
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not template:
        return "unmatched"

    # A route reached through an included router reports its path relative to
    # the prefix it was mounted under - `/search`, not `/api/v1/search`. Left
    # alone, two routers that happen to share a sub-path would share a metric
    # series and the graph would be the sum of two unrelated endpoints.
    #
    # The prefix is recovered by substituting this request's path parameters
    # back into the template and subtracting the result from the real path,
    # rather than by reading `root_path` - which this version of FastAPI does
    # not set for an included router, and which is the sort of thing that
    # changes between releases.
    resolved = template
    for name, value in (request.path_params or {}).items():
        resolved = resolved.replace("{" + name + "}", str(value))

    path = request.url.path
    if resolved and path.endswith(resolved) and path != resolved:
        return path[: -len(resolved)] + template
    return template


def status_class(status_code: int) -> str:
    """`2xx`, `4xx`, `5xx`. The exact code is in the log line.

    A dashboard asks how many requests are failing, not which of the nine
    varieties of 4xx they were - and the coarse label keeps the series count
    down for a question nobody asks of a graph.
    """
    return f"{status_code // 100}xx"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Correlate, measure and log every request.

    An inbound ``X-Request-ID`` is honoured so a trace started by the web client
    or an upstream gateway stays continuous, and a W3C ``traceparent`` is
    continued for the same reason.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or f"req_{uuid.uuid4().hex[:16]}"
        set_request_id(request_id)
        request.state.request_id = request_id

        trace_id, _ = tracing.start_request(request.headers.get(tracing.TRACEPARENT_HEADER))
        request.state.trace_id = trace_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - started
            # Recorded as 5xx here as well as in the exception handler, because
            # an exception that escapes the handler chain would otherwise be the
            # one class of failure missing from the metrics.
            route = route_template(request)
            metrics.http_requests.inc(request.method, route, "5xx")
            metrics.http_duration.observe(elapsed, request.method, route)
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                trace_id=trace_id,
                duration_ms=round(elapsed * 1000, 2),
                spans=tracing.breakdown(),
            )
            raise

        elapsed = time.perf_counter() - started
        route = route_template(request)

        metrics.http_requests.inc(request.method, route, status_class(response.status_code))
        metrics.http_duration.observe(elapsed, request.method, route)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[TRACE_ID_HEADER] = trace_id

        # Only when a route actually resolved one. Claiming `Content-Language:
        # en` on a response that contains no prose is a small lie that a cache
        # will happily act on.
        language = getattr(request.state, "language", None)
        if language:
            response.headers["Content-Language"] = language
            # Tells a shared cache that this URL has more than one
            # representation. Without it, the first Amharic response served
            # through a proxy becomes everybody's.
            existing_vary = response.headers.get("Vary")
            response.headers["Vary"] = (
                f"{existing_vary}, Accept-Language" if existing_vary else "Accept-Language"
            )
        timing = tracing.server_timing_header()
        if timing:
            # Rendered by browsers in the network panel beside their own
            # measurements, so a slow page can be attributed to the server
            # without anybody opening a dashboard.
            response.headers["Server-Timing"] = timing

        spans = tracing.breakdown()
        # Promoted to a warning past the threshold, carrying the breakdown. A
        # slow request that looks identical to a fast one in the log is a slow
        # request nobody investigates.
        emit = logger.warning if elapsed >= tracing.SLOW_REQUEST_SECONDS else logger.info
        emit(
            "request_completed",
            method=request.method,
            path=request.url.path,
            route=route,
            status=response.status_code,
            trace_id=trace_id,
            duration_ms=round(elapsed * 1000, 2),
            **({"spans": spans} if spans else {}),
        )
        return response
