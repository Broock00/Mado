"""Feature flag and audit log tests (spec ADM-003, ADM-004).

Two features that fail in opposite ways. A flag fails by being on when it should
be off, or by flickering; an audit log fails by being incomplete or editable,
and you find out during the investigation that needed it.
"""

from __future__ import annotations

import pytest

import app.models  # noqa: F401  (audit entries are built here, so mappers must resolve)
from app.domains.trust import audit
from app.domains.trust.audit import AuditEntry, AuditLog
from app.domains.trust.flags import (
    KEY_PATTERN,
    FeatureFlag,
    FlagState,
    bucket_of,
    is_enabled_for,
)

pytestmark = pytest.mark.anyio


def flag(**overrides) -> FlagState:
    return FlagState(
        **{
            "key": "concierge.voice",
            "description": "Speak instead of typing.",
            "enabled": True,
            "rollout_percentage": 100,
            **overrides,
        }
    )


class TestFlagDefaults:
    def test_a_flag_that_does_not_exist_is_off(self):
        """Flags turn new things on, so absence means the behaviour that
        existed before - the state that has actually been tested. Defaulting on
        means a typo in a flag name ships an unfinished feature."""
        assert not is_enabled_for(None, "user-1")

    def test_a_disabled_flag_is_off_whatever_the_rollout(self):
        """Switching off is one action, not "off and also set 0%"."""
        assert not is_enabled_for(flag(enabled=False, rollout_percentage=100), "user-1")

    def test_a_fully_rolled_out_flag_is_on_for_everybody(self):
        assert all(is_enabled_for(flag(), f"user-{i}") for i in range(50))

    def test_a_zero_percent_rollout_is_on_for_nobody(self):
        assert not any(
            is_enabled_for(flag(rollout_percentage=0), f"user-{i}") for i in range(50)
        )

    def test_a_new_flag_is_created_switched_off(self):
        """A flag that arrives on has shipped the feature by existing."""
        assert FeatureFlag.__table__.c.enabled.default.arg is False


class TestRolloutIsSticky:
    def test_the_same_explorer_always_gets_the_same_answer(self):
        """A rollout that re-rolled per request would show somebody a feature on
        one page and not the next, which is worse than either state."""
        partial = flag(rollout_percentage=50)
        first = is_enabled_for(partial, "user-42")
        assert all(is_enabled_for(partial, "user-42") == first for _ in range(20))

    def test_a_percentage_covers_roughly_that_share(self):
        people = [f"user-{i}" for i in range(2000)]
        inside = sum(1 for p in people if is_enabled_for(flag(rollout_percentage=10), p))
        assert 0.07 <= inside / len(people) <= 0.13

    def test_different_flags_do_not_pick_the_same_people(self):
        """Without the flag key in the hash, the same unlucky tenth would be
        last for every gradual rollout the platform ever does."""
        people = [f"user-{i}" for i in range(2000)]
        one = {p for p in people if is_enabled_for(flag(key="a.one", rollout_percentage=10), p)}
        two = {p for p in people if is_enabled_for(flag(key="b.two", rollout_percentage=10), p)}

        # Independent 10% samples overlap on about 1% of the population.
        overlap = len(one & two) / len(people)
        assert overlap < 0.04
        assert one != two

    def test_buckets_stay_inside_the_range(self):
        assert all(0 <= bucket_of("some.flag", f"user-{i}") < 100 for i in range(200))

    def test_an_anonymous_visitor_is_outside_a_partial_rollout(self):
        """Otherwise the feature appears and disappears as they sign in."""
        assert not is_enabled_for(flag(rollout_percentage=50), None)

    def test_but_a_full_rollout_still_reaches_them(self):
        assert is_enabled_for(flag(rollout_percentage=100), None)


class TestFlagKeys:
    @pytest.mark.parametrize("key", ["concierge.voice", "search.visual", "map.route.live"])
    def test_a_surface_and_a_capability(self, key):
        assert KEY_PATTERN.match(key)

    @pytest.mark.parametrize("key", ["Voice", "concierge voice", "concierge", "UPPER.CASE", ""])
    def test_anything_else_is_refused(self, key):
        """The list has to stay readable once there are thirty of them."""
        assert not KEY_PATTERN.match(key)


class TestTheAuditLogIsAppendOnly:
    def test_there_is_no_way_to_change_an_entry(self):
        """A record somebody with power can edit is not evidence."""
        methods = {m for m in dir(AuditLog) if not m.startswith("_")}
        assert not methods & {"update", "edit", "delete", "remove", "purge", "amend"}
        assert {"record", "recent", "for_subject"} <= methods

    def test_an_entry_has_no_updated_at(self):
        """A column implying a row can be modified is one somebody will modify."""
        assert "updated_at" not in AuditEntry.__table__.c

    def test_the_actor_survives_their_account_being_deleted(self):
        """A trail that empties itself when somebody leaves is not a trail."""
        assert AuditEntry.__table__.c.actor_label.nullable is False
        actor_fk = next(iter(AuditEntry.__table__.c.actor_user_id.foreign_keys))
        assert actor_fk.ondelete == "SET NULL"

    def test_the_subject_has_no_foreign_key(self):
        """Subjects live in several domains, and the record must outlive the
        thing it describes."""
        assert not AuditEntry.__table__.c.subject_id.foreign_keys


class TestWhatIsRecorded:
    def test_every_action_is_an_exercise_of_authority(self):
        """This logs power, not people. A log of what administrators do is
        accountability; a log of what everybody does is surveillance."""
        for action in audit.KNOWN_ACTIONS:
            subject = action.split(".")[0]
            assert subject in {"account", "moderator", "verification", "content", "flag"}

    def test_nothing_about_browsing_is_in_the_vocabulary(self):
        forbidden = {"search", "view", "save", "plan", "recommend", "click", "session"}
        assert not {a.split(".")[0] for a in audit.KNOWN_ACTIONS} & forbidden

    def test_suspension_and_restoration_are_separate_actions(self):
        """"Status changed" would need the context read to know which."""
        assert audit.ACCOUNT_SUSPENDED != audit.ACCOUNT_RESTORED
        assert audit.MODERATOR_GRANTED != audit.MODERATOR_REVOKED


class TestRecordingBehaviour:
    def collector(self):
        added: list = []
        return added, type("S", (), {"add": lambda _self, row: added.append(row)})()

    def test_an_entry_joins_the_callers_transaction(self):
        """Not fire-and-forget: if the record cannot be written the action does
        not happen either. The opposite of the choice made for analytics and
        email, where a failed write must never block the user."""
        added, session = self.collector()
        log = AuditLog(session)

        entry = log.record(actor=None, action=audit.FLAG_CHANGED, subject_type="feature_flag")

        assert added == [entry]
        # No flush, no commit - the caller owns the transaction.
        assert not hasattr(session, "flush")

    def test_a_system_action_is_recorded_rather_than_dropped(self):
        added, session = self.collector()
        entry = AuditLog(session).record(
            actor=None, action=audit.CONTENT_REJECTED, subject_type="experience"
        )
        assert entry.actor_user_id is None
        assert entry.actor_label == "system"

    def test_an_unfamiliar_action_is_still_written(self):
        """Refusing to record an action because its name is unfamiliar would
        lose exactly the unusual event most worth having."""
        added, session = self.collector()
        entry = AuditLog(session).record(
            actor=None, action="something.new", subject_type="thing"
        )
        assert entry.action == "something.new"
        assert added == [entry]

    def test_a_long_reason_is_truncated_rather_than_refused(self):
        added, session = self.collector()
        entry = AuditLog(session).record(
            actor=None,
            action=audit.ACCOUNT_SUSPENDED,
            subject_type="user",
            reason="x" * 5000,
        )
        assert len(entry.reason) == audit.MAX_REASON


class TestEveryFlagGatesSomething:
    """A flag nobody reads is a switch wired to nothing.

    The same argument as the API key scopes: a lever in an admin console that
    changes no behaviour is worse than an empty console, because somebody will
    flip it during an incident and conclude the platform is broken when nothing
    happens.
    """

    def test_each_gated_flag_is_actually_checked_in_the_code(self):
        import pathlib

        from app.domains.trust import flags as flags_module

        root = pathlib.Path(flags_module.__file__).parents[2]
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in root.rglob("*.py")
            if path.name != "flags.py"  # the catalogue names all of them
        )
        unread = [key for key in flags_module.GATED if f'"{key}"' not in sources]
        assert not unread, f"registered but never consulted: {unread}"

    def test_a_gated_flag_is_seeded_so_the_feature_does_not_vanish(self):
        """An absent flag resolves to off. Shipping a gate without registering
        the flag would switch a working feature off everywhere, which is the
        opposite of what a flag is for."""
        import inspect

        from app.seed import addis_ababa

        assert "GATED" in inspect.getsource(addis_ababa._register_flags)

    def test_the_seed_only_inserts(self):
        """An operator who switched one off keeps that decision when the seed is
        re-run. A seed that resets flags is a seed that undoes an incident
        response."""
        import inspect

        source = inspect.getsource(addis_ababa_register())
        assert "if key in existing" in source
        assert "enabled=True" in source


def addis_ababa_register():
    from app.seed import addis_ababa

    return addis_ababa._register_flags


class TestFlagsAreNotSettings:
    def test_infrastructure_toggles_stay_in_configuration(self):
        """The moment infrastructure lives in a database, somebody switches off
        rate limiting from a web page."""
        from app.core.config import Settings

        operator_only = {
            "rate_limit_enabled",
            "scheduler_enabled",
            "require_verified_email_to_publish",
        }
        assert operator_only <= set(Settings.model_fields)
