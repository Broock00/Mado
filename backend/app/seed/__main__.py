"""Seed CLI.

python -m app.seed              seed the pilot city and rebuild the search index
python -m app.seed --no-index   seed only
python -m app.seed --reindex    rebuild the search index only
"""

from __future__ import annotations

import argparse

from app.core.asyncio_compat import run as run_async
from app.core.database import SessionFactory
from app.core.logging import configure_logging, get_logger
from app.domains.discovery.indexer import reindex_all
from app.seed.addis_ababa import seed

logger = get_logger("mado.seed.cli")


async def _run(*, do_seed: bool, do_index: bool) -> None:
    async with SessionFactory() as session:
        if do_seed:
            counts = await seed(session)
            await session.commit()
            print("Seeded Addis Ababa:")
            for name, value in counts.items():
                print(f"  {name:<15} {value}")

        if do_index:
            indexed = await reindex_all(session)
            print(f"Indexed {indexed} experiences into the search cluster.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Mado platform")
    parser.add_argument("--no-index", action="store_true", help="Skip search indexing.")
    parser.add_argument("--reindex", action="store_true", help="Only rebuild the index.")
    args = parser.parse_args()

    configure_logging()
    run_async(
        _run(
            do_seed=not args.reindex,
            do_index=not args.no_index,
        )
    )


if __name__ == "__main__":
    main()
