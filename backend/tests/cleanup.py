"""Remove what the integration tests create.

The suite drives a real API against a real database, so every run leaves
accounts, publishers, venues and listings behind. Nothing removed them, and over
a week that reached 3,038 accounts and 1,626 listings against 30 real ones - at
which point the seeded catalogue was 2% of itself, the concierge was recommending
"An Evening of Spoken Word at Test Venue 0bf7" to the person developing it, and
"the catalogue is thin" was a reasonable and completely wrong conclusion to draw.

The criterion is the suite's own marker. Every account it creates has an
address at :data:`TEST_EMAIL_DOMAIN`, so anything owned by one is test data and
nothing else is. Seed and demo publishers have no owner account at all, so they
cannot be caught by it - which is what makes deleting by this rule safe rather
than merely convenient.

Runnable on its own for a database that already has years of it:

    python -m tests.cleanup
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.database import SessionFactory

TEST_EMAIL_DOMAIN = "mado-qa.example.org"
MARKER = f"%@{TEST_EMAIL_DOMAIN}"

# The accounts, and everything hanging off them, in an order that never leaves a
# row pointing at something gone. Cascades would cover much of this; naming each
# step means a missing one is a zero in the output rather than a foreign-key
# error halfway through a teardown nobody is watching.
_OWNED_EXPERIENCES = """
    select e.id from catalog.experiences e
    join publisher.publishers p on p.id = e.publisher_id
    join identity.user_profiles up on up.user_id = p.owner_user_id
    where up.email like :m
"""
_ACCOUNTS = "select user_id from identity.user_profiles where email like :m"

STATEMENTS: list[tuple[str, str]] = [
    ("tickets", f"delete from commerce.tickets where user_id in ({_ACCOUNTS})"),
    (
        "payment events",
        f"delete from commerce.payment_events where order_id in "
        f"(select id from commerce.orders where user_id in ({_ACCOUNTS}))",
    ),
    (
        "order lines",
        f"delete from commerce.order_lines where order_id in "
        f"(select id from commerce.orders where user_id in ({_ACCOUNTS}))",
    ),
    ("orders", f"delete from commerce.orders where user_id in ({_ACCOUNTS})"),
    (
        "ticket types",
        f"delete from commerce.ticket_types where experience_id in ({_OWNED_EXPERIENCES})",
    ),
    (
        "event instances",
        f"delete from catalog.event_instances where experience_id in ({_OWNED_EXPERIENCES})",
    ),
    (
        "experience tags",
        f"delete from catalog.experience_tags where experience_id in ({_OWNED_EXPERIENCES})",
    ),
    ("media", f"delete from catalog.media where experience_id in ({_OWNED_EXPERIENCES})"),
    (
        "experiences",
        "delete from catalog.experiences where publisher_id in "
        "(select p.id from publisher.publishers p "
        "join identity.user_profiles up on up.user_id = p.owner_user_id where up.email like :m)",
    ),
    (
        "venues",
        "delete from catalog.venues where publisher_id in "
        "(select p.id from publisher.publishers p "
        "join identity.user_profiles up on up.user_id = p.owner_user_id where up.email like :m)",
    ),
    ("api keys", f"delete from publisher.api_keys where owner_user_id in ({_ACCOUNTS})"),
    ("publishers", f"delete from publisher.publishers where owner_user_id in ({_ACCOUNTS})"),
    ("accounts", f"delete from identity.users where id in ({_ACCOUNTS})"),
]


async def purge() -> dict[str, int]:
    """Delete it all. Returns what went, per kind."""
    removed: dict[str, int] = {}
    async with SessionFactory() as session:
        for label, sql in STATEMENTS:
            result = await session.execute(text(sql), {"m": MARKER})
            if result.rowcount:
                removed[label] = result.rowcount
        await session.commit()
    return removed


def purge_blocking() -> dict[str, int]:
    """For a synchronous teardown.

    Its own event loop, and a selector one: psycopg cannot drive Windows'
    default proactor loop, and the suite's loop has already closed by the time
    a session-scoped teardown runs.
    """
    return asyncio.run(purge(), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    for name, count in (purge_blocking() or {"nothing": 0}).items():
        print(f"  {name:18} {count}")
