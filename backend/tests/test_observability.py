"""Metrics, tracing and health tests (spec OPERATIONS-42).

Monitoring code has an unusual failure mode: when it is wrong, everything looks
fine. A miscounted metric does not raise, a mislabelled series does not fail a
test, and a health check that always says ok passes every probe. So these tests
are mostly about the things that would be silently wrong.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import health, metrics, tracing
from app.core.metrics import (
    MAX_SERIES,
    Counter,
    Gauge,
    Histogram,
    _escape,
    _number,
)
from app.core.middleware import route_template, status_class

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clean():
    metrics.reset()
    yield
    metrics.reset()


def series_of(metric, text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(metric) and "#" not in line]


def _request(path: str, template: str, params: dict | None = None):
    """A stand-in for the two things `route_template` reads."""
    route = type("R", (), {"path_format": template})()
    return type(
        "Q",
        (),
        {
            "scope": {"route": route},
            "path_params": params or {},
            "url": type("U", (), {"path": path})(),
        },
    )()


# ------------------------------------------------------------ cardinality


class TestLabelsCannotRunAway:
    def test_a_metric_stops_growing_at_the_ceiling(self):
        """The classic incident: a label whose values are unbounded turns one
        metric into a million series and takes the scrape, then the time-series
        database, then the dashboards with it."""
        counter = Counter("test_unbounded", "help", ("id",))
        for index in range(MAX_SERIES * 3):
            counter.inc(f"id-{index}")

        rendered = list(counter.render())
        assert len(series_of("test_unbounded", "\n".join(rendered))) <= MAX_SERIES + 1

    def test_the_overflow_is_visible_rather_than_dropped(self):
        """Silently discarding the excess would make a runaway label look like a
        metric that simply stopped moving."""
        counter = Counter("test_overflow", "help", ("id",))
        for index in range(MAX_SERIES + 50):
            counter.inc(f"id-{index}")
        assert any("overflow" in line for line in counter.render())

    def test_existing_series_keep_counting_after_the_cap(self):
        """The point is to stop *new* series, not to freeze the metric. A
        counter that stopped moving during an incident would be worse than one
        that lost some detail."""
        counter = Counter("test_still_counts", "help", ("id",))
        counter.inc("first")
        for index in range(MAX_SERIES * 2):
            counter.inc(f"id-{index}")
        counter.inc("first")

        line = next(x for x in counter.render() if 'id="first"' in x)
        assert line.endswith(" 2")

    def test_the_http_route_label_is_a_template(self):
        """`/api/v1/experiences/{experience_id}`, never the resolved path - one
        series per experience is the same problem wearing a reasonable disguise."""
        label = route_template(
            _request(
                "/api/v1/experiences/9f1c8d4e",
                "/experiences/{experience_id}",
                {"experience_id": "9f1c8d4e"},
            )
        )
        assert label == "/api/v1/experiences/{experience_id}"

    def test_the_mount_prefix_is_restored(self):
        """An included router reports its path relative to the prefix, so two
        routers sharing a sub-path would otherwise share one series and the
        graph would be the sum of two unrelated endpoints."""
        assert route_template(_request("/api/v1/search", "/search")) == "/api/v1/search"

    def test_a_top_level_route_is_left_alone(self):
        assert route_template(_request("/health", "/health")) == "/health"

    def test_an_unmatched_path_does_not_become_a_label(self):
        """A 404 sweep from a vulnerability scanner would otherwise create one
        series per URL it guessed."""
        request = type("Q", (), {"scope": {}, "path_params": {}, "url": None})()
        assert route_template(request) == "unmatched"

    def test_status_is_a_class_not_a_code(self):
        assert status_class(201) == "2xx"
        assert status_class(404) == "4xx"
        assert status_class(503) == "5xx"


# ------------------------------------------------------- exposition format


class TestTheTextFormat:
    def test_a_counter_renders_with_help_and_type(self):
        counter = Counter("test_counter", "How many.", ())
        counter.inc()
        text = "\n".join(counter.render())
        assert "# HELP test_counter How many." in text
        assert "# TYPE test_counter counter" in text
        assert "test_counter 1" in text

    def test_counts_render_as_integers(self):
        """`1.0` parses, but a counter rendered that way reads as a measurement
        rather than a count."""
        assert _number(1.0) == "1"
        assert _number(0.5) == "0.5"

    def test_a_histogram_is_cumulative(self):
        """Prometheus buckets are 'less than or equal', so each contains the one
        below it. Non-cumulative buckets parse fine and give wrong quantiles."""
        histogram = Histogram("test_hist", "help", (), buckets=(1.0, 2.0, 3.0))
        for value in (0.5, 1.5, 2.5):
            histogram.observe(value)

        text = "\n".join(histogram.render())
        assert 'test_hist_bucket{le="1"} 1' in text
        assert 'test_hist_bucket{le="2"} 2' in text
        assert 'test_hist_bucket{le="3"} 3' in text
        assert 'test_hist_bucket{le="+Inf"} 3' in text
        assert "test_hist_count 3" in text

    def test_a_value_past_the_last_bucket_still_counts(self):
        histogram = Histogram("test_tail", "help", (), buckets=(1.0,))
        histogram.observe(99.0)
        text = "\n".join(histogram.render())
        assert 'test_tail_bucket{le="1"} 0' in text
        assert 'test_tail_bucket{le="+Inf"} 1' in text
        assert "test_tail_sum 99" in text

    def test_the_sum_is_of_observations_not_buckets(self):
        histogram = Histogram("test_sum", "help", ())
        histogram.observe(0.25)
        histogram.observe(2.0)
        text = "\n".join(histogram.render())
        assert "test_sum_sum 2.25" in text
        assert "test_sum_count 2" in text

    def test_an_unobserved_histogram_exposes_no_samples(self):
        """Only HELP and TYPE. Emitting zeroed buckets for something that has
        never happened invents data - and a rate over it would read as a real
        measurement of nothing."""
        histogram = Histogram("test_untouched", "help", ())
        assert not [line for line in histogram.render() if not line.startswith("#")]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [('say "hi"', 'say \\"hi\\"'), ("a\\b", "a\\\\b"), ("one\ntwo", "one\\ntwo")],
    )
    def test_label_values_are_escaped(self, raw, expected):
        """An unescaped quote in a label value produces a body the scraper
        cannot parse - and the value that contains one is always an error
        message, which is to say the moment you most need the metric."""
        assert _escape(raw) == expected

    def test_a_backslash_is_escaped_before_the_quotes_it_introduces(self):
        """Escaping in the other order double-escapes."""
        assert _escape('\\"') == '\\\\\\"'

    def test_the_body_ends_with_a_newline(self):
        """A scrape of a body without one is a parse error in some clients, and
        the sort of bug that only turns up in production."""
        assert metrics.render().endswith("\n")

    def test_a_gauge_can_go_down(self):
        gauge = Gauge("test_gauge", "help", ("name",))
        gauge.set(1, "db")
        gauge.set(0, "db")
        assert 'test_gauge{name="db"} 0' in "\n".join(gauge.render())


class TestHistogramsRatherThanSummaries:
    def test_buckets_are_exposed_so_they_can_be_aggregated(self):
        """A summary computes quantiles in the process, and those cannot be
        combined afterwards: the median of two medians is not a median. Buckets
        add up, so a p95 across a fleet is still a p95."""
        metrics.http_duration.observe(0.05, "GET", "/api/v1/experiences")
        text = metrics.render()
        assert "mado_http_request_duration_seconds_bucket" in text
        assert "quantile=" not in text


# --------------------------------------------------------------- tracing


class TestTraceContext:
    def test_an_inbound_trace_is_continued(self):
        """Otherwise the browser's trace and the API's are two unrelated ids for
        one thing that happened."""
        incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        trace_id, span_id = tracing.start_request(incoming)
        assert trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert span_id != "00f067aa0ba902b7"  # a new span, in the same trace

    def test_a_request_with_no_header_starts_its_own(self):
        trace_id, _ = tracing.start_request(None)
        assert len(trace_id) == 32
        assert int(trace_id, 16) != 0

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "garbage",
            "00-tooshort-00f067aa0ba902b7-01",
            # All-zero ids are explicitly invalid in the specification.
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
        ],
    )
    def test_a_broken_header_is_ignored_rather_than_fatal(self, header):
        """A misconfigured proxy must not be able to fail requests."""
        assert tracing.parse_traceparent(header) is None
        trace_id, _ = tracing.start_request(header)
        assert len(trace_id) == 32

    def test_an_outbound_header_carries_this_trace(self):
        tracing.start_request(None)
        outbound = tracing.traceparent_for_outbound()
        assert outbound.startswith("00-")
        assert tracing.current_trace_id() in outbound


class TestTheBreakdown:
    def test_repeated_spans_of_one_name_add_up(self):
        """Forty database calls should be one `db` total. Forty entries is a
        profiler's output; six numbers is what an operator acts on."""
        tracing.start_request(None)
        for _ in range(3):
            with tracing.span("db"):
                pass
        assert list(tracing.breakdown()) == ["db"]

    def test_the_slowest_span_is_first(self):
        """A breakdown you have to read all of is a breakdown nobody reads."""
        tracing.start_request(None)
        tracing.add_span("db", 0.01)
        tracing.add_span("ai", 1.5)
        tracing.add_span("search", 0.2)
        assert list(tracing.breakdown()) == ["ai", "search", "db"]

    def test_span_names_cannot_run_away_either(self):
        """A span name generated from data is the cardinality problem again,
        one request at a time."""
        tracing.start_request(None)
        for index in range(tracing.MAX_SPANS * 2):
            tracing.add_span(f"span-{index}", 0.001)
        assert len(tracing.breakdown()) <= tracing.MAX_SPANS

    def test_timing_outside_a_request_is_harmless(self):
        """Scheduled jobs and tests call the same instrumented code. There is
        nowhere to hang the span, and that must not be an error."""
        tracing._spans.set(None)
        with tracing.span("db"):
            pass
        assert tracing.breakdown() == {}

    def test_a_raising_body_still_records_its_time(self):
        """The slow call that failed is the one worth timing."""
        tracing.start_request(None)
        with pytest.raises(ValueError), tracing.span("ai"):
            raise ValueError("boom")
        assert "ai" in tracing.breakdown()


class TestServerTiming:
    def test_it_is_a_valid_header(self):
        tracing.start_request(None)
        tracing.add_span("db", 0.012)
        header = tracing.server_timing_header()
        assert "db;dur=12.0" in header

    def test_a_name_that_is_not_a_token_is_made_into_one(self):
        """An invalid header is dropped silently by the browser, which is the
        worst kind of broken."""
        tracing.start_request(None)
        tracing.add_span("ai gateway;x", 0.01)
        header = tracing.server_timing_header()
        assert ";" in header
        assert "ai_gateway_x;dur=" in header

    def test_nothing_timed_means_no_header(self):
        """An empty `Server-Timing` is not a valid one."""
        tracing.start_request(None)
        assert tracing.server_timing_header() is None


class TestDependencyHelper:
    async def test_a_failure_is_counted_as_one(self):
        """The failure mode of hand-written instrumentation is a `try` that
        increments the success counter and an `except` that forgets the other -
        so the dashboard says the dependency is healthy while it is down."""
        with pytest.raises(RuntimeError), tracing.dependency("ai"):
            raise RuntimeError("provider down")

        text = metrics.render()
        assert 'mado_dependency_calls_total{dependency="ai",outcome="error"} 1' in text

    async def test_a_success_is_counted_and_timed(self):
        with tracing.dependency("search"):
            pass
        text = metrics.render()
        assert 'mado_dependency_calls_total{dependency="search",outcome="ok"} 1' in text
        assert 'mado_dependency_duration_seconds_count{dependency="search"} 1' in text


# ---------------------------------------------------------------- health


class TestLivenessAndReadinessAreDifferentQuestions:
    async def test_liveness_touches_nothing_external(self):
        """A liveness probe that fails when the database is down makes an
        orchestrator restart every container during a database incident, which
        turns one outage into two."""
        import inspect

        from app.api.routes import platform

        source = inspect.getsource(platform.liveness)
        for forbidden in ("SessionFactory", "redis", "search", "readiness("):
            assert forbidden not in source

    async def test_an_optional_dependency_down_is_degraded_not_unhealthy(self, monkeypatch):
        """Search falls back to the database, so reporting not-ready would pull
        every healthy instance out of the load balancer over an accelerator."""

        async def fail():
            raise RuntimeError("meilisearch unreachable")

        monkeypatch.setattr(
            health,
            "CHECKS",
            (health.Check("database", True, _ok), health.Check("search", False, fail)),
        )
        overall, results = await health.readiness()
        assert overall == "degraded"
        assert {r.name: r.status for r in results}["search"] == "down"

    async def test_a_required_dependency_down_is_unhealthy(self):
        async def fail():
            raise RuntimeError("no database")

        checks = (health.Check("database", True, fail),)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(health, "CHECKS", checks)
            overall, _ = await health.readiness()
        assert overall == "unhealthy"

    async def test_a_hung_dependency_does_not_hang_the_probe(self, monkeypatch):
        """A probe held open past the orchestrator's own timeout is read as a
        failure of the probe rather than of the thing it was checking."""

        async def hang():
            await asyncio.sleep(60)

        monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(health, "CHECKS", (health.Check("database", True, hang),))

        overall, results = await asyncio.wait_for(health.readiness(), timeout=5)
        assert overall == "unhealthy"
        assert "No answer" in results[0].detail

    async def test_a_switched_off_dependency_is_not_a_failure(self, monkeypatch):
        """An operator who turned something off does not want a red light
        about it."""

        async def disabled():
            raise health._Disabled("No API key configured.")

        monkeypatch.setattr(health, "CHECKS", (health.Check("ai", False, disabled),))
        overall, results = await health.readiness()
        assert overall == "ok"
        assert results[0].status == health.STATUS_DISABLED

    async def test_a_dependency_nobody_asked_is_not_reported_as_up(self, monkeypatch):
        """The model gateway is not probed - a readiness check that spends money
        every few seconds is a bill. Reporting it `up` anyway would be a health
        check that always says ok, which passes every probe and means nothing."""

        async def unprobed():
            raise health._Unprobed("Configured but not contacted.")

        monkeypatch.setattr(health, "CHECKS", (health.Check("ai", False, unprobed),))
        overall, results = await health.readiness()
        assert results[0].status == health.STATUS_UNKNOWN
        assert overall == "ok"  # not knowing is not a failure
        # And no gauge either way: a guess dressed as a measurement is worse
        # than a gap.
        assert 'mado_dependency_up{dependency="ai"}' not in metrics.render()

    async def test_a_disabled_dependency_is_not_gauged_as_down(self, monkeypatch):
        """`mado_dependency_up{dependency="ai"} 0` would page somebody about a
        setting."""

        async def disabled():
            raise health._Disabled("stub provider")

        monkeypatch.setattr(health, "CHECKS", (health.Check("ai", False, disabled),))
        await health.readiness()
        assert 'mado_dependency_up{dependency="ai"}' not in metrics.render()

    async def test_checks_run_concurrently(self, monkeypatch):
        """Otherwise a readiness probe costs the sum of its checks, and adding a
        fifth dependency slowly breaks the probe's own timeout budget."""

        async def slow():
            await asyncio.sleep(0.2)

        monkeypatch.setattr(
            health,
            "CHECKS",
            tuple(health.Check(f"d{i}", False, slow) for i in range(5)),
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        await health.readiness()
        assert loop.time() - started < 0.6  # not 1.0


class TestTheGaugeIsKeptAlive:
    def test_health_is_refreshed_on_a_timer_not_only_when_polled(self):
        """Otherwise `mado_dependency_up` only exists if something happens to
        call `/health/ready`, and a scraper reading `/metrics` sees the series
        appear, vanish or never exist. An alert that only fires when somebody is
        already looking is not an alert."""
        from app.core import scheduler

        assert any(job.name == "health" for job in scheduler.JOBS)

    def test_it_runs_in_every_process_rather_than_one(self):
        """Shared work is elected so it is not done twice. This is the opposite:
        it observes *this* process, so electing a leader would leave every other
        instance reporting nothing about itself."""
        from app.core import scheduler

        health_job = next(job for job in scheduler.JOBS if job.name == "health")
        assert health_job.local

    def test_shared_jobs_are_still_elected(self):
        """The lock is what stops two workers sending the same webhook twice."""
        from app.core import scheduler

        assert not any(job.local for job in scheduler.JOBS if job.name != "health")

    async def test_a_local_job_runs_immediately_rather_than_after_a_delay(self):
        """An instance that says nothing about itself for the first half minute
        is silent during exactly the window a deploy goes wrong in."""
        import asyncio

        from app.core import scheduler

        ran = asyncio.Event()

        async def record():
            ran.set()

        task = asyncio.create_task(
            scheduler._loop(scheduler.Job("probe", 3600, record, local=True))
        )
        try:
            await asyncio.wait_for(ran.wait(), timeout=2)
        finally:
            task.cancel()

    async def test_a_shared_job_waits_its_interval_first(self):
        """Starting every job at boot makes a deploy the busiest moment on the
        database."""
        import asyncio

        from app.core import scheduler

        ran = asyncio.Event()

        async def record():
            ran.set()

        task = asyncio.create_task(
            scheduler._loop(scheduler.Job("probe", 3600, record, local=False))
        )
        try:
            await asyncio.sleep(0.2)
            assert not ran.is_set()
        finally:
            task.cancel()


class TestWhatIsNotExposed:
    def test_telemetry_is_closed_by_default_in_production(self, monkeypatch):
        """Metrics and readiness together describe every dependency, its
        latency, the traffic shape and where the errors are. A deployment that
        forgot to set a token should get a refusal, not an open door."""
        from app.api.routes import platform
        from app.core import config

        production = config.get_settings().model_copy(
            update={"environment": "production", "telemetry_token": ""}
        )
        monkeypatch.setattr(platform, "get_settings", lambda: production)
        assert not platform._authorised(None)
        assert not platform._authorised("guess")

    def test_but_open_in_development_so_nothing_needs_setup(self, monkeypatch):
        from app.api.routes import platform
        from app.core import config

        development = config.get_settings().model_copy(
            update={"environment": "development", "telemetry_token": ""}
        )
        monkeypatch.setattr(platform, "get_settings", lambda: development)
        assert platform._authorised(None)

    def test_a_configured_token_is_required_even_in_development(self, monkeypatch):
        from app.api.routes import platform
        from app.core import config

        settings = config.get_settings().model_copy(
            update={"environment": "development", "telemetry_token": "s3cret"}
        )
        monkeypatch.setattr(platform, "get_settings", lambda: settings)
        assert platform._authorised("s3cret")
        assert not platform._authorised("wrong")
        assert not platform._authorised(None)

    def test_the_token_is_compared_in_constant_time(self):
        """This is a scrape target: it tolerates being hit thousands of times,
        which is exactly the budget a timing attack needs."""
        import inspect

        from app.api.routes import platform

        assert "compare_digest" in inspect.getsource(platform._authorised)

    def test_the_operational_endpoints_are_not_in_the_public_schema(self):
        """They are for an orchestrator and a scrape job, not for anybody
        reading the API documentation."""
        from app.api.routes import platform

        assert platform.router.routes
        assert all(not route.include_in_schema for route in platform.router.routes)


async def _ok() -> None:
    return None
