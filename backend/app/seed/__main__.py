"""Seed CLI.

python -m app.seed              seed the pilot city and rebuild the search index
python -m app.seed --no-index   seed only
python -m app.seed --reindex    rebuild the search index only
python -m app.seed --recompute  rebuild popularity and trend from interaction events
python -m app.seed --ingest URL  import an iCalendar feed from a trusted source
python -m app.seed --demo-cities    add invented listings in New York, London and
                                    Nairobi so the platform can be exercised
                                    outside its pilot city
python -m app.seed --remove-demo    delete every one of them again
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.core.asyncio_compat import run as run_async
from app.core.database import SessionFactory
from app.core.logging import configure_logging, get_logger
from app.domains.discovery.embedding_service import backfill_embeddings
from app.domains.discovery.indexer import reindex_all
from app.domains.explorer.learning import recompute_engagement_scores
from app.seed.addis_ababa import seed

logger = get_logger("mado.seed.cli")


async def _grant_moderator(email: str) -> None:
    from app.domains.identity.models import User, UserProfile

    async with SessionFactory() as session:
        result = await session.execute(
            select(User)
            .join(UserProfile, UserProfile.user_id == User.id)
            .where(UserProfile.email == email.lower().strip())
        )
        user = result.scalar_one_or_none()
        if user is None:
            print(f"No account found for {email}")
            return
        user.is_moderator = True
        await session.commit()
        print(f"{email} is now a moderator.")


async def _ingest(url: str, source_slug: str) -> None:
    """Import an iCalendar feed.

    A manual entry point for the same code a scheduled job runs. Sources are
    configured rather than passed on the command line in a deployed environment;
    this exists to try a feed before trusting it.
    """
    import httpx

    from app.domains.catalog.feeds import FeedSource, from_ics, ingest

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        items = from_ics(response.text, external_id_prefix=f"{source_slug}:")

    print(f"Parsed {len(items)} items from {url}")
    if not items:
        return

    async with SessionFactory() as session:
        source = FeedSource(
            slug=source_slug,
            name=source_slug.replace("-", " ").title(),
            # Manual runs are treated as untrusted: an operator trying a feed for
            # the first time should not be able to overwrite existing data with
            # it by accident. A configured source carries its real trust level.
            trust=0.5,
            publisher_slug="addis-culture-bureau",
            city_slug="addis-ababa",
        )
        report = await ingest(session, source, items)
        await session.commit()

        print(f"  created  {report.created}")
        print(f"  updated  {report.updated}")
        print(f"  merged   {report.merged}  (recognised as something already held)")
        print(f"  skipped  {report.skipped}")
        print(f"  withheld {report.withheld}  (screening sent these to moderation)")
        for problem in report.problems[:10]:
            print(f"    - {problem}")


async def _demo_cities(*, remove: bool) -> None:
    """Invented listings in real places.

    Separate from the main seed, and never part of it: this data is not real,
    and the one thing that must not happen is it becoming indistinguishable from
    the catalogue. It refuses to run in production, marks everything it writes,
    and can delete exactly what it made.
    """
    from app.seed.demo_cities import remove_demo_cities, seed_demo_cities

    async with SessionFactory() as session:
        if remove:
            counts = await remove_demo_cities(session)
            await session.commit()
            print("Removed the demo cities:")
        else:
            counts = await seed_demo_cities(session)
            await session.commit()
            print("Seeded demo cities (invented data - never run this in production):")
        for name, value in counts.items():
            print(f"  {name:<14} {value}")

        if not remove:
            indexed = await reindex_all(session)
            written = await backfill_embeddings(session, only_missing=True)
            await session.commit()
            print(f"  {'indexed':<14} {indexed}")
            print(f"  {'embedded':<14} {written}")


async def _recompute() -> None:
    """Rebuild engagement aggregates from the interaction stream.

    Separate from seeding because it is a recurring maintenance pass, not a
    one-off: in a deployed environment this runs on a schedule, and running it
    here is the same code path.
    """
    async with SessionFactory() as session:
        result = await recompute_engagement_scores(session)
        await session.commit()
        print(
            f"Recomputed engagement: {result['updated']} experiences updated, "
            f"{result['with_activity']} had activity in the window."
        )
        if result["with_activity"] == 0:
            print("  No interaction events in the window - scores left at zero.")


async def _run(
    *, do_seed: bool, do_index: bool, do_embed: bool, only_missing_embeddings: bool = True
) -> None:
    async with SessionFactory() as session:
        if do_seed:
            counts = await seed(session)
            # Derive popularity and trend from the interaction history the seed
            # just wrote, rather than letting the seed assert them. Same code path
            # the scheduled job uses.
            engagement = await recompute_engagement_scores(session)
            await session.commit()
            print("Seeded Addis Ababa:")
            for name, value in counts.items():
                print(f"  {name:<15} {value}")
            print(f"  {'scored':<15} {engagement['updated']} experiences ranked from behaviour")

        if do_index:
            indexed = await reindex_all(session)
            print(f"Indexed {indexed} experiences into the search cluster.")

        if do_embed:
            from app.integrations.embeddings import get_embedding_provider

            provider = get_embedding_provider()
            written = await backfill_embeddings(session, only_missing=only_missing_embeddings)
            await session.commit()
            kind = "semantic" if provider.is_semantic else "lexical only, not semantic"
            print(f"Embedded {written} experiences using {provider.name} ({kind}).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Mado platform")
    parser.add_argument("--no-index", action="store_true", help="Skip search indexing.")
    parser.add_argument("--reindex", action="store_true", help="Only rebuild the index.")
    parser.add_argument(
        "--make-moderator", metavar="EMAIL", help="Grant moderator rights to an account."
    )
    parser.add_argument(
        "--ingest", metavar="URL", help="Import an iCalendar feed from a trusted source."
    )
    parser.add_argument(
        "--source", default="manual-import", help="Source slug for --ingest."
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="Rebuild popularity and trend scores from interaction events.",
    )
    parser.add_argument(
        "--demo-cities",
        action="store_true",
        help="Add invented listings in New York, London and Nairobi (never in production).",
    )
    parser.add_argument(
        "--remove-demo", action="store_true", help="Delete the demo cities again."
    )
    parser.add_argument("--no-embed", action="store_true", help="Skip embedding generation.")
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Only generate embeddings for experiences that lack them.",
    )
    parser.add_argument(
        "--re-embed",
        action="store_true",
        help="Regenerate every embedding (use after changing embedding provider).",
    )
    args = parser.parse_args()

    configure_logging()

    if args.make_moderator:
        run_async(_grant_moderator(args.make_moderator))
        return
    if args.ingest:
        run_async(_ingest(args.ingest, args.source))
        return
    if args.demo_cities or args.remove_demo:
        run_async(_demo_cities(remove=args.remove_demo))
        return
    if args.recompute:
        run_async(_recompute())
        return
    embed_only = args.embed or args.re_embed
    run_async(
        _run(
            do_seed=not (args.reindex or embed_only),
            do_index=not (args.no_index or embed_only),
            do_embed=not args.no_embed,
            only_missing_embeddings=not args.re_embed,
        )
    )


if __name__ == "__main__":
    main()
