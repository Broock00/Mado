"""Metrics (spec OPERATIONS-42 §5).

Counters and histograms, exposed in the Prometheus text format at `/metrics`.

**Written here rather than pulled in.** `prometheus_client` is the obvious
choice and would be a reasonable one. It is not used because the part of it this
platform needs - four hundred lines of registry and a text serialiser - is
smaller than the part it does not: multiprocess mode, a GC collector, a WSGI
app, a push gateway. The house style is already to own each boundary and keep
the dependency list short (see `app/integrations/`), and there is one thing a
hand-written registry can do that the library deliberately does not, which is
the next paragraph. Swap it in the day this needs exemplars or native
histograms.

**Label values are capped.** The classic production incident with metrics is a
label whose values are unbounded - a user id, a URL path with an id in it, an
error string - which turns one metric into a million series and takes the
scrape, then the time-series database, then the dashboards with it. Every metric
here has a series ceiling; past it, new label combinations collapse into
`overflow` and a warning is logged once. A metric that loses detail is
recoverable. A monitoring system that falls over during the incident it was
meant to explain is not.

**Nothing here is on the critical path.** Recording is a dictionary update under
no lock: this is a single-process asyncio application, so there is no true
concurrency between coroutines to race on, and a metric is never worth an
exception. Every public function swallows its own errors.

**Cheap by construction.** Counters are integers and histograms are fixed bucket
arrays, so memory is bounded by the series ceiling rather than by traffic, and a
scrape walks that same fixed set.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Sequence

from app.core.logging import get_logger

logger = get_logger("mado.metrics")

# How many distinct label combinations one metric may hold before the rest
# collapse into a single `overflow` series. Generous for anything with sensible
# labels - the route templates, status classes and provider names used here come
# to a few dozen - and low enough to notice a mistake before it matters.
MAX_SERIES = 200

# Seconds. Chosen around what this API actually does rather than by powers of
# ten: most reads are tens of milliseconds, a plan or a concierge turn is
# seconds, and anything past ten is a timeout in disguise.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

_OVERFLOW = "overflow"


def _escape(value: str) -> str:
    """Escape a label value for the text exposition format.

    Backslash first: escaping it after the others would double-escape the
    backslashes they introduce.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Metric:
    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labels = tuple(labels)
        self._overflowed = False
        # One lock for the whole registry would serialise unrelated metrics; one
        # per metric is enough, and these are held for a dictionary write. Held
        # at all because the scheduler's jobs and the request path are different
        # tasks, and a scrape iterating while a job writes would otherwise be a
        # dictionary-changed-size error rather than a slightly stale number.
        self._lock = threading.Lock()

    def _key(self, values: Sequence[str]) -> tuple[str, ...]:
        return tuple(str(v) for v in values)

    def _guard(self, store: dict, key: tuple[str, ...]) -> tuple[str, ...]:
        """Return the key to use, collapsing to `overflow` past the ceiling."""
        if key in store or len(store) < MAX_SERIES:
            return key
        if not self._overflowed:
            self._overflowed = True
            logger.warning(
                "metric_cardinality_capped",
                metric=self.name,
                series=len(store),
                dropped_example=key,
            )
        return tuple(_OVERFLOW for _ in self.labels)

    def _label_string(self, key: Sequence[str]) -> str:
        if not self.labels:
            return ""
        pairs = ",".join(
            f'{name}="{_escape(value)}"' for name, value in zip(self.labels, key, strict=False)
        )
        return "{" + pairs + "}"


class Counter(_Metric):
    """A number that only goes up."""

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        try:
            with self._lock:
                key = self._guard(self._values, self._key(label_values))
                self._values[key] = self._values.get(key, 0.0) + amount
        except Exception:  # noqa: BLE001 - a metric is never worth an exception
            logger.debug("metric_increment_failed", metric=self.name)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            items = list(self._values.items())
        for key, value in items:
            yield f"{self.name}{self._label_string(key)} {_number(value)}"


class Gauge(_Metric):
    """A number that goes both ways - a queue depth, a pool size."""

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, *label_values: str) -> None:
        try:
            with self._lock:
                key = self._guard(self._values, self._key(label_values))
                self._values[key] = value
        except Exception:  # noqa: BLE001
            logger.debug("metric_set_failed", metric=self.name)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} gauge"
        with self._lock:
            items = list(self._values.items())
        for key, value in items:
            yield f"{self.name}{self._label_string(key)} {_number(value)}"


class Histogram(_Metric):
    """Cumulative buckets, a sum and a count - enough for a quantile estimate.

    Not a summary. A summary computes quantiles in the process, which cannot be
    aggregated across instances afterwards: the median of two medians is not a
    median. Buckets add up, so a p95 across a fleet is still a p95.
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labels)
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}

    def observe(self, value: float, *label_values: str) -> None:
        try:
            with self._lock:
                key = self._guard(self._counts, self._key(label_values))
                counts = self._counts.get(key)
                if counts is None:
                    # One slot per bucket plus one for +Inf.
                    counts = [0] * (len(self.buckets) + 1)
                    self._counts[key] = counts
                    self._sums[key] = 0.0
                self._sums[key] += value
                # Linear scan: eleven buckets, so bisect would cost more in
                # indirection than it saves in comparisons.
                for index, edge in enumerate(self.buckets):
                    if value <= edge:
                        counts[index] += 1
                        break
                else:
                    counts[len(self.buckets)] += 1
        except Exception:  # noqa: BLE001
            logger.debug("metric_observe_failed", metric=self.name)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            items = [(key, list(counts), self._sums[key]) for key, counts in self._counts.items()]

        for key, counts, total in items:
            running = 0
            for index, edge in enumerate(self.buckets):
                running += counts[index]
                yield (
                    f"{self.name}_bucket{self._label_string_with(key, 'le', _number(edge))} "
                    f"{running}"
                )
            running += counts[len(self.buckets)]
            yield f'{self.name}_bucket{self._label_string_with(key, "le", "+Inf")} {running}'
            yield f"{self.name}_sum{self._label_string(key)} {_number(total)}"
            yield f"{self.name}_count{self._label_string(key)} {running}"

    def _label_string_with(self, key: Sequence[str], extra: str, value: str) -> str:
        pairs = [
            f'{name}="{_escape(v)}"' for name, v in zip(self.labels, key, strict=False)
        ]
        pairs.append(f'{extra}="{value}"')
        return "{" + ",".join(pairs) + "}"


def _number(value: float) -> str:
    """Format for exposition, keeping integers integral.

    `1.0` and `1` both parse, but a counter rendered as `1.0` reads as a
    measurement rather than a count.
    """
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


# --------------------------------------------------------------- the registry

_registry: list[_Metric] = []


def _register(metric: _Metric):
    _registry.append(metric)
    return metric


# HTTP. `route` is the *template* - `/api/v1/experiences/{experience_id}` - and
# never the resolved path, because one series per experience id is precisely the
# unbounded-label problem this module guards against. `status` is the class
# (2xx, 4xx, 5xx) for the same reason: the exact code is in the logs, and what a
# dashboard asks is "how many are failing".
http_requests = _register(
    Counter(
        "mado_http_requests_total",
        "HTTP requests handled, by route template and status class.",
        ("method", "route", "status"),
    )
)
http_duration = _register(
    Histogram(
        "mado_http_request_duration_seconds",
        "Wall time from first byte in to last byte of the response body handed off.",
        ("method", "route"),
    )
)

# Outbound calls to anything not ours - the model gateway, geocoding, routing,
# search, a subscriber's webhook endpoint. `outcome` rather than a status code:
# what matters operationally is whether it worked.
dependency_calls = _register(
    Counter(
        "mado_dependency_calls_total",
        "Calls to an external dependency, by name and outcome.",
        ("dependency", "outcome"),
    )
)
dependency_duration = _register(
    Histogram(
        "mado_dependency_duration_seconds",
        "Time spent waiting on an external dependency.",
        ("dependency",),
        # Longer tail than HTTP: a model call routinely takes seconds, and the
        # HTTP buckets would put every one of them in the last bucket.
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0),
    )
)

# AI spend, which is the one operational number that is also a bill (§9).
ai_tokens = _register(
    Counter(
        "mado_ai_tokens_total",
        "Tokens billed, by model and direction.",
        ("model", "direction"),
    )
)

# Rate limiting. A rising count here is either abuse or a limit set too tight,
# and the scope label is what tells them apart.
rate_limited = _register(
    Counter(
        "mado_rate_limited_total",
        "Requests refused by a rate limit, by scope.",
        ("scope",),
    )
)

# Background work (§4, "Background services"). A job that stops running is
# invisible in request metrics, which is exactly why it needs its own.
job_runs = _register(
    Counter(
        "mado_job_runs_total",
        "Scheduled job executions, by name and outcome.",
        ("job", "outcome"),
    )
)
job_duration = _register(
    Histogram(
        "mado_job_duration_seconds",
        "How long a scheduled job took.",
        ("job",),
        buckets=(0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0),
    )
)

# Webhook delivery (spec DEV-003). Separate from `dependency_calls` because the
# far end belongs to a publisher rather than to us: a rise here is somebody
# else's outage, and reading it as ours would send the wrong team looking.
webhook_deliveries = _register(
    Counter(
        "mado_webhook_deliveries_total",
        "Webhook delivery attempts, by outcome.",
        ("outcome",),
    )
)

# Readiness, as a number. A dashboard should not have to poll and parse a JSON
# health endpoint to draw a red square.
dependency_up = _register(
    Gauge(
        "mado_dependency_up",
        "1 when a dependency answered its last check, 0 when it did not.",
        ("dependency",),
    )
)


def render() -> str:
    """The whole registry in the Prometheus text exposition format.

    Ends with a newline: a scrape of a body without one is a parse error in some
    clients, and it is the sort of bug that only appears in production.
    """
    lines: list[str] = []
    for metric in _registry:
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


def reset() -> None:
    """Forget everything. For tests, which would otherwise see each other's counts."""
    for metric in _registry:
        for attribute in ("_values", "_counts", "_sums"):
            store = getattr(metric, attribute, None)
            if store is not None:
                store.clear()
        metric._overflowed = False
