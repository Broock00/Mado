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
    # Before the orders, and by hand rather than by cascade. A ledger entry is a
    # financial record and its foreign key is deliberately RESTRICT: in
    # production nothing deletes an order, and an entry that vanished with one
    # would take a publisher's earnings with it silently. Here the order is
    # invented and so is the debt, so both go - in the order the constraint
    # demands.
    (
        "ledger entries",
        f"delete from commerce.ledger_entries where order_id in "
        f"(select id from commerce.orders where user_id in ({_ACCOUNTS}))",
    ),
    ("orders", f"delete from commerce.orders where user_id in ({_ACCOUNTS})"),
    # Payouts point at a publisher rather than an order, so they survive the
    # above and have to be named. Emptied after the entries that referenced them.
    (
        "payouts",
        "delete from commerce.payouts where publisher_id in "
        "(select p.id from publisher.publishers p "
        "join identity.user_profiles up on up.user_id = p.owner_user_id where up.email like :m)",
    ),
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
    # Counted per key, so this has to go before the keys do.
    ("api usage", f"delete from publisher.api_usage where owner_user_id in ({_ACCOUNTS})"),
    ("api keys", f"delete from publisher.api_keys where owner_user_id in ({_ACCOUNTS})"),
    (
        "developer accounts",
        f"delete from publisher.developer_accounts where user_id in ({_ACCOUNTS})",
    ),
    (
        "subscription invoices",
        "delete from publisher.subscription_invoices where publisher_id in "
        "(select p.id from publisher.publishers p "
        "join identity.user_profiles up on up.user_id = p.owner_user_id where up.email like :m)",
    ),
    (
        "subscriptions",
        "delete from publisher.subscriptions where publisher_id in "
        "(select p.id from publisher.publishers p "
        "join identity.user_profiles up on up.user_id = p.owner_user_id where up.email like :m)",
    ),
    ("publishers", f"delete from publisher.publishers where owner_user_id in ({_ACCOUNTS})"),
    ("accounts", f"delete from identity.users where id in ({_ACCOUNTS})"),
    # Cities materialised by a test posting somewhere the platform had never seen.
    #
    # These cannot be caught by the ownership rule above - a city has no owner,
    # which is the whole point of it being derived from coordinates rather than
    # entered by somebody. So the rule here is emptiness instead: a city that is
    # not live, and that nothing is left pointing at, is a city no test and no
    # person has a use for.
    #
    # Deliberately conservative in two directions. `is_live` false spares every
    # seeded city outright, and they are all live. Requiring no venues and no
    # experiences spares a real city somebody genuinely posted in, because it
    # will not be empty. What is left is exactly the rows a test run created and
    # then had emptied out by the statements above.
    (
        "materialised cities",
        "delete from catalog.cities c where c.is_live = false "
        "and not exists (select 1 from catalog.venues v where v.city_id = c.id) "
        "and not exists (select 1 from catalog.experiences e where e.city_id = c.id) "
        "and not exists (select 1 from catalog.neighborhoods n where n.city_id = c.id)",
    ),
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
