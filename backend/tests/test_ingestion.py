"""External feed ingestion tests.

The failure that matters is the same concert appearing three times because three
sources described it. The tests below are mostly about reconciliation and about
refusing nonsense at the door, because those are where an unattended nightly job
does damage quietly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domains.catalog.feeds import FeedSource, from_ics
from app.domains.catalog.ingestion import (
    MAX_FUTURE,
    SAME_EVENT_WINDOW,
    TITLE_SIMILARITY,
    FeedItem,
    IngestionReport,
    is_plausible,
    provenance,
    title_similarity,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def item(**overrides) -> FeedItem:
    base = {
        "external_id": "evt-1",
        "title": "Azmari Night at Fendika",
        "description": "Improvised sung poetry and masinko.",
        "starts_at": NOW + timedelta(days=1),
    }
    base.update(overrides)
    return FeedItem(**base)


# --- title matching ----------------------------------------------------------


class TestTitleMatching:
    """Deliberately permissive - the venue and time gate does the discriminating.

    Tested here in isolation only to pin the shape of the measure. What actually
    prevents a bad merge is that this is never consulted until two listings
    already share a venue and start within an hour of each other.
    """

    def test_the_same_event_named_differently_matches(self):
        score = title_similarity("Azmari Night at Fendika", "Fendika: Azmari Music Evening")
        assert score >= TITLE_SIMILARITY

    def test_a_shorter_restatement_matches(self):
        score = title_similarity("Friday Jazz on the Terrace", "Jazz on the Terrace")
        assert score >= TITLE_SIMILARITY

    def test_unrelated_events_do_not_match(self):
        score = title_similarity("Azmari Night at Fendika", "Sunday Morning Run Club")
        assert score < TITLE_SIMILARITY

    def test_a_verbose_title_is_not_penalised(self):
        """Jaccard scored this pair 0.5 and refused to merge the same concert."""
        assert title_similarity("Jazz Night", "Jazz Night with the Addis Quartet Live") == 1.0

    def test_empty_titles_do_not_raise(self):
        assert title_similarity("", "Something") == 0.0

    def test_shared_filler_words_alone_do_not_match(self):
        """"Night", "live" and "event" appear in half the catalogue."""
        assert title_similarity("Live Music Night", "Live Comedy Night") < TITLE_SIMILARITY


class TestSameEventWindow:
    """The gate that makes permissive title matching safe."""

    def test_recurring_events_on_different_days_fall_outside_it(self):
        tuesday = NOW
        wednesday = NOW + timedelta(days=1)
        # A weekly series has a title overlap high enough to merge on titles
        # alone; only the time gate keeps the instances distinct.
        assert (
            title_similarity("Tuesday Jazz Session", "Wednesday Jazz Session")
            >= TITLE_SIMILARITY
        )
        assert abs(wednesday - tuesday) > SAME_EVENT_WINDOW

    def test_doors_and_showtime_fall_inside_it(self):
        """One feed lists doors, another the performance. Same event."""
        assert abs(timedelta(minutes=30)) <= SAME_EVENT_WINDOW

    def test_a_matinee_and_an_evening_show_stay_separate(self):
        assert timedelta(hours=6) > SAME_EVENT_WINDOW


# --- rejecting nonsense ------------------------------------------------------


class TestPlausibility:
    def test_a_normal_listing_passes(self):
        ok, reason = is_plausible(item(), now=NOW)
        assert ok and reason is None

    def test_an_untitled_item_is_refused(self):
        ok, reason = is_plausible(item(title="   "), now=NOW)
        assert not ok and "title" in reason

    def test_an_item_with_no_external_id_is_refused(self):
        """Without one there is no way to recognise the same record next run."""
        ok, reason = is_plausible(item(external_id=""), now=NOW)
        assert not ok

    def test_an_absurdly_distant_date_is_refused(self):
        """A year parsed into a day field produces exactly this."""
        ok, reason = is_plausible(item(starts_at=NOW + MAX_FUTURE + timedelta(days=1)), now=NOW)
        assert not ok and "days from now" in reason

    def test_an_event_ending_before_it_starts_is_refused(self):
        ok, reason = is_plausible(
            item(starts_at=NOW + timedelta(days=1), ends_at=NOW), now=NOW
        )
        assert not ok and "before" in reason

    def test_a_naive_timestamp_does_not_raise(self):
        ok, _ = is_plausible(item(starts_at=datetime(2026, 8, 8, 20, 0)), now=NOW)
        assert ok

    def test_an_undated_place_is_fine(self):
        """Not everything in a feed is an event."""
        ok, _ = is_plausible(item(starts_at=None), now=NOW)
        assert ok


# --- provenance --------------------------------------------------------------


class TestProvenance:
    def test_the_source_is_recorded(self):
        attributes = provenance(item(), source="fendika-calendar", source_trust=0.9)
        assert attributes["source"] == "fendika-calendar"
        assert attributes["externalId"] == "evt-1"

    def test_imported_content_is_marked_as_such(self):
        """The interface has to attribute it honestly rather than as a local post."""
        assert provenance(item(), source="x", source_trust=0.5)["origin"] == "external_feed"

    def test_trust_travels_with_the_record(self):
        """So a moderator can act on everything from one source at once."""
        assert provenance(item(), source="x", source_trust=0.4)["sourceTrust"] == 0.4


class TestFeedSource:
    def test_a_low_trust_source_may_not_overwrite(self):
        """An aggregator repeating a scrape must not overwrite a venue's own data."""
        assert not FeedSource("agg", "Aggregator", 0.5, "pub", "addis-ababa").may_overwrite

    def test_a_venues_own_calendar_may_overwrite(self):
        assert FeedSource("fendika", "Fendika", 0.9, "pub", "addis-ababa").may_overwrite


# --- iCalendar adapter -------------------------------------------------------

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:azmari-2026-08-08
SUMMARY:Azmari Night
DTSTART:20260808T200000Z
DTEND:20260808T230000Z
LOCATION:Fendika Cultural Centre
DESCRIPTION:Improvised sung poetry\\, masinko fiddle and eskista dancing that
  runs late into the night.
URL:https://example.org/azmari
END:VEVENT
BEGIN:VEVENT
UID:no-summary
DTSTART:20260809T200000Z
END:VEVENT
END:VCALENDAR
"""


class TestIcsAdapter:
    def test_reads_an_event(self):
        items = from_ics(ICS)
        assert len(items) == 1  # the one without a summary is dropped
        assert items[0].title == "Azmari Night"
        assert items[0].venue_name == "Fendika Cultural Centre"

    def test_parses_times_as_utc_aware(self):
        starts = from_ics(ICS)[0].starts_at
        assert starts is not None and starts.tzinfo is not None
        assert starts.hour == 20

    def test_unfolds_wrapped_lines(self):
        """ICS wraps long values onto continuation lines; parsing line-by-line
        would truncate every description at 75 characters."""
        assert "runs late into the night" in from_ics(ICS)[0].description

    def test_unescapes_ics_escaping(self):
        assert "poetry, masinko" in from_ics(ICS)[0].description

    def test_ignores_parameters_on_property_names(self):
        """DTSTART;TZID=Africa/Addis_Ababa is still DTSTART."""
        text = ICS.replace("DTSTART:", "DTSTART;TZID=Africa/Addis_Ababa:")
        assert from_ics(text)[0].starts_at is not None

    def test_prefixes_external_ids(self):
        """Two sources can both use "event-1"; the prefix keeps them distinct."""
        assert from_ics(ICS, external_id_prefix="fendika:")[0].external_id.startswith("fendika:")

    def test_an_empty_feed_is_not_an_error(self):
        assert from_ics("BEGIN:VCALENDAR\nEND:VCALENDAR\n") == []

    def test_junk_input_does_not_raise(self):
        assert from_ics("this is not a calendar") == []


class TestReport:
    def test_totals_add_up(self):
        report = IngestionReport(source="x", created=2, updated=3, merged=1, skipped=4)
        assert report.total == 10
