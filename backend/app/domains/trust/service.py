"""Trust and safety (spec BUSINESS-07, 56.05).

Open publishing makes this load-bearing rather than optional. The governance model
the specs describe has four layers, and this module implements the first three:

1. **Publisher responsibility** - every post has an accountable owner
2. **Automated screening** - cheap heuristics at submission, scoring not blocking
3. **Community reporting** - readers flag what screening missed
4. **Human oversight** - moderators decide (the queue below feeds them)

The governing constraint is spec BUSINESS-07: *automated systems detect, humans
decide*. Nothing here deletes content or suspends an account. The strongest
automatic action is withholding an item from discovery pending review, which is
reversible and leaves the author's own copy intact.

The heuristics are deliberately simple and transparent. They are a first filter
that raises attention, not a classifier pretending to judgement - and every one of
them is written so a moderator can see exactly why something was flagged.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog.models import (
    MODERATION_APPROVED,
    MODERATION_FLAGGED,
    MODERATION_PENDING,
    MODERATION_REJECTED,
    Experience,
    Venue,
)
from app.domains.explorer.models import ContentReport
from app.domains.identity.models import User
from app.domains.trust.semantic_screening import screen_semantically

logger = get_logger("mado.trust")

VALID_REPORT_REASONS = {
    "spam",
    "inaccurate",
    "inappropriate",
    "duplicate",
    "scam",
    "other",
}

# Reports needed before an item is withheld pending review. Set above one so a
# single disgruntled reader cannot take a competitor's listing down, and low
# enough that genuine problems surface quickly.
REPORT_THRESHOLD_FOR_REVIEW = 3

# A scam report is the one category where waiting for a threshold is the wrong
# trade: the harm lands before the third report arrives.
URGENT_REASONS = {"scam"}

# Risk at or above this routes a new post to review instead of straight to live.
RISK_THRESHOLD_FOR_REVIEW = 0.7

_URL_PATTERN = re.compile(r"https?://|www\.", re.I)
_CONTACT_PATTERN = re.compile(r"\b(whatsapp|telegram|dm me|call now|\+?\d{9,})\b", re.I)
_MONEY_BAIT_PATTERN = re.compile(
    r"\b(?:guaranteed|100% (?:free|profit)|earn \$?\d+|investment opportunity|crypto"
    r"|forex|make money|work from home|click here|limited offer|act now)\b",
    re.I,
)
_SHOUTING_PATTERN = re.compile(r"[A-Z]{6,}")


class ScreeningResult:
    """The outcome of automated screening, with its reasoning attached."""

    def __init__(self, score: float, signals: list[str]) -> None:
        self.score = round(min(1.0, max(0.0, score)), 3)
        self.signals = signals

    @property
    def needs_review(self) -> bool:
        return self.score >= RISK_THRESHOLD_FOR_REVIEW

    def as_note(self) -> str | None:
        if not self.signals:
            return None
        return "Automated screening: " + "; ".join(self.signals)


def screen_text(title: str, description: str, summary: str | None = None) -> ScreeningResult:
    """Score submitted text for spam and low-quality signals.

    Returns a score with the specific signals that produced it, so a moderator
    reviewing the queue sees the reasoning rather than an opaque number.
    """
    body = " ".join(filter(None, [title, summary or "", description]))
    score = 0.0
    signals: list[str] = []

    urls = len(_URL_PATTERN.findall(body))
    if urls >= 3:
        score += 0.35
        signals.append(f"{urls} links in the text")
    elif urls == 2:
        score += 0.1

    if _CONTACT_PATTERN.search(body):
        score += 0.3
        signals.append("off-platform contact details")

    # finditer + group(0), not findall: the pattern contains groups, and findall
    # would return tuples of those groups rather than the matched phrases.
    bait = {match.group(0).lower() for match in _MONEY_BAIT_PATTERN.finditer(body)}
    if bait:
        score += min(0.4, 0.2 * len(bait))
        signals.append(f"promotional language ({', '.join(sorted(bait))})")

    shouting = _SHOUTING_PATTERN.findall(title)
    if shouting:
        score += 0.15
        signals.append("shouting in the title")

    if len(description.strip()) < 60:
        score += 0.15
        signals.append("very short description")

    # A wall of repeated words is a classic filler pattern.
    words = [w.lower() for w in re.findall(r"\w+", body) if len(w) > 3]
    if len(words) >= 20:
        variety = len(set(words)) / len(words)
        if variety < 0.35:
            score += 0.25
            signals.append("highly repetitive text")

    return ScreeningResult(score, signals)


def _combine(rules: ScreeningResult, verdict) -> ScreeningResult:
    """Merge the two screeners, taking the worse view.

    Deliberately not an average. Averaging lets a confident clearance from one
    reader dilute a genuine concern from the other, which is the wrong direction to
    fail in when the input is written by someone who may want a particular verdict.
    The model can therefore raise a score but never lower one.
    """
    score = max(rules.score, verdict.risk)
    signals = list(rules.signals)

    if verdict.categories:
        signals.append("content review: " + ", ".join(verdict.categories))
    if verdict.rationale:
        signals.append(verdict.rationale.strip().rstrip("."))

    # Some categories are serious enough that a hedged score should not keep them
    # out of the queue. A model saying "possibly fraud, 0.45" still said fraud.
    if verdict.demands_review:
        score = max(score, RISK_THRESHOLD_FOR_REVIEW)

    return ScreeningResult(score=score, signals=signals)


class TrustService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --------------------------------------------------------------- screening

    async def screen_on_publish(self, experience: Experience) -> ScreeningResult:
        """Screen an experience as it goes live.

        A high score withholds it pending review rather than rejecting it: spec
        BUSINESS-07 keeps enforcement with humans, and a false positive that
        silently deletes someone's work is far worse than one that delays it.
        """
        result = screen_text(experience.title, experience.description, experience.summary)

        # A human ruling outranks any later automated re-check - and there is no
        # point spending a model call to re-litigate a decision that cannot change
        # the outcome.
        if experience.moderation_locked:
            experience.risk_score = result.score
            return result

        # Second, independent reading. Combined by taking the worse of the two:
        # the pattern screener catches surface markers and cannot be argued with,
        # the model catches intent expressed in ordinary words. Neither is allowed
        # to clear what the other flagged, so a submission that talks its way past
        # the model still meets the deterministic floor.
        verdict = await screen_semantically(
            experience.title, experience.description, experience.summary
        )
        if verdict.available:
            result = _combine(result, verdict)

        experience.risk_score = result.score

        if result.needs_review:
            # FLAGGED, not PENDING. PENDING is in DISCOVERABLE_MODERATION_STATUSES,
            # so routing here withheld nothing: a post scoring 0.85 for scam signals
            # stayed live in the feed, in search and inside generated itineraries
            # until a moderator happened to look. The docstring above already said
            # "withholds it pending review"; only the status was wrong.
            #
            # This is still "automated systems detect, humans decide" (spec
            # BUSINESS-07). Nothing is deleted or rejected, the author keeps and can
            # edit their post, and a moderator makes the actual ruling. What the
            # screener decides is only whether to promote it to strangers while that
            # ruling is outstanding.
            experience.moderation_status = MODERATION_FLAGGED
            experience.moderation_notes = result.as_note()
            logger.info(
                "experience_withheld_for_review",
                experience_id=str(experience.id),
                risk=result.score,
                signals=result.signals,
            )
        else:
            experience.moderation_status = MODERATION_APPROVED
            experience.moderation_notes = result.as_note()

        return result

    # ---------------------------------------------------------------- reports

    async def report(
        self,
        *,
        experience_id: uuid.UUID,
        reporter: User | None,
        reason: str,
        detail: str | None = None,
    ) -> ContentReport:
        if reason not in VALID_REPORT_REASONS:
            raise ValidationError(
                f"'{reason}' is not a valid reason.",
                code="INVALID_REPORT_REASON",
                details={"validReasons": sorted(VALID_REPORT_REASONS)},
            )

        experience = await self.session.get(Experience, experience_id)
        if experience is None or experience.deleted_at is not None:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")

        if reporter is not None:
            existing = await self.session.execute(
                select(ContentReport).where(
                    ContentReport.experience_id == experience_id,
                    ContentReport.reporter_user_id == reporter.id,
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise ConflictError("You have already reported this.", code="ALREADY_REPORTED")

        report = ContentReport(
            experience_id=experience_id,
            reporter_user_id=reporter.id if reporter else None,
            reason=reason,
            detail=(detail or "").strip() or None,
        )
        self.session.add(report)

        experience.report_count = (experience.report_count or 0) + 1

        # Withhold pending review once reports accumulate, or immediately for a
        # scam report where the damage is done before a threshold is reached.
        should_withhold = (
            experience.report_count >= REPORT_THRESHOLD_FOR_REVIEW or reason in URGENT_REASONS
        )
        if should_withhold and not experience.moderation_locked:
            experience.moderation_status = MODERATION_FLAGGED
            logger.info(
                "experience_flagged_by_reports",
                experience_id=str(experience_id),
                reports=experience.report_count,
                reason=reason,
            )

        await self.session.flush()
        return report

    # -------------------------------------------------------- moderation queue

    async def queue(self, *, limit: int = 50) -> list[Experience]:
        """Items awaiting a human decision, most-reported first."""
        result = await self.session.execute(
            select(Experience)
            .where(
                Experience.deleted_at.is_(None),
                Experience.moderation_status.in_([MODERATION_PENDING, MODERATION_FLAGGED]),
            )
            .options(
                selectinload(Experience.publisher),
                selectinload(Experience.media),
                selectinload(Experience.category),
                selectinload(Experience.city),
                selectinload(Experience.tags),
                selectinload(Experience.venue),
                selectinload(Experience.events),
            )
            .order_by(Experience.report_count.desc(), Experience.risk_score.desc())
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def decide(
        self,
        *,
        experience_id: uuid.UUID,
        moderator: User,
        approve: bool,
        note: str | None = None,
    ) -> Experience:
        """Record a human moderation decision.

        Sets ``moderation_locked`` so automated screening cannot later overturn a
        person's judgement - spec BUSINESS-07's "human oversight" layer only means
        something if it is durable.
        """
        # Eagerly loaded: the caller serializes the result and may re-index it,
        # both of which read publisher, city, venue, tags and media. A bare get()
        # would leave those to lazy-load, which cannot work under an async session.
        result = await self.session.execute(
            select(Experience)
            .where(Experience.id == experience_id)
            .options(
                selectinload(Experience.publisher),
                selectinload(Experience.city),
                selectinload(Experience.category),
                selectinload(Experience.tags),
                selectinload(Experience.media),
                selectinload(Experience.events),
                selectinload(Experience.venue).selectinload(Venue.neighborhood),
            )
        )
        experience = result.scalar_one_or_none()
        if experience is None or experience.deleted_at is not None:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")

        experience.moderation_status = MODERATION_APPROVED if approve else MODERATION_REJECTED
        experience.moderation_notes = note
        experience.moderation_locked = True

        reports = await self.session.execute(
            select(ContentReport).where(
                ContentReport.experience_id == experience_id, ContentReport.status == "open"
            )
        )
        now = datetime.now(UTC)
        for report in reports.scalars().all():
            report.status = "dismissed" if approve else "upheld"
            report.resolved_at = now
            report.resolved_by_user_id = moderator.id
            report.resolution_note = note

        await self.session.flush()
        logger.info(
            "moderation_decision",
            experience_id=str(experience_id),
            approved=approve,
            moderator_id=str(moderator.id),
        )
        return experience

    # ------------------------------------------------------------ rate limiting

    async def recent_publish_count(self, publisher_id: uuid.UUID, *, hours: int = 24) -> int:
        """How many experiences this publisher has posted recently.

        Volume is the cheapest spam signal there is; the API uses this to cap how
        fast a brand-new account can flood the catalog.
        """
        since = datetime.now(UTC) - timedelta(hours=hours)
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(Experience)
                .where(
                    Experience.publisher_id == publisher_id,
                    Experience.created_at >= since,
                )
            )
            or 0
        )
