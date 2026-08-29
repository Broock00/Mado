"""What a business account has bought, as a controlled vocabulary.

BUSINESS-09 sizes publisher subscriptions at 30% of the long-term revenue mix -
the largest single stream - and BUSINESS-90.01 §5 puts them first, before
sponsorship and before the API. This is that.

**The catalogue lives here, not in a table.** Two reasons, and the second is the
one that decides it:

- A plan row nobody wrote is a plan nobody enforces. Limits that live in the
  database can be edited in production by somebody who has not read what depends
  on them, and the code that checks a limit would have to cope with a plan it has
  never heard of.
- The upgrade page has to render the tiers before it has asked the server
  anything. That is the same argument that put the suitability vocabulary in a
  module and duplicated it in `frontend/web/src/lib/suitability.ts`, and it is
  duplicated the same way here, with a test asserting the two agree.

**Prices are per currency, because BUSINESS-90.01 §18 asks for region-aware
pricing** and a business in Addis and one in London are not being sold the same
thing at the same number. There is no exchange-rate conversion anywhere near
this: a price list is a decision, and a rate that moved overnight would reprice
every account without anybody choosing to.

**Absence is the free plan.** An account with no subscription row is on `free`,
not in an error state and not unconfigured. Almost every account will never buy
anything, and a model where the overwhelming majority need a row written for
them is a migration waiting to be forgotten.

**A limit of None is "no limit", not zero.** Spelled explicitly, because a
missing number read as zero would lock an enterprise account out of the thing it
paid the most for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Plans -------------------------------------------------------------------

FREE = "free"
PROFESSIONAL = "professional"
BUSINESS = "business"
ENTERPRISE = "enterprise"


@dataclass(frozen=True, slots=True)
class Entitlements:
    """What an account on this plan may have.

    Deliberately small. Every field here is checked somewhere - a limit nothing
    enforces is the same lie as a permission nothing checks, which
    `permissions.py` already refuses to tell.
    """

    # How many posts may be live at once. Drafts are not counted: a limit that
    # stopped somebody writing would push the work somewhere else rather than
    # sell them anything, and an unpublished draft costs the platform nothing.
    max_live_listings: int | None
    # People who can be given access, not counting the owner. The owner is a
    # column on the publisher rather than a membership row, so counting them
    # would make a "two seats" plan mean one.
    max_team_members: int | None
    # How far back the analytics screen may look. Capped at what
    # `publisher/analytics.py` is willing to produce - it holds that beyond a
    # season the numbers describe a listing that has since changed, and a tier
    # advertising a year against that would be selling a fiction. So this
    # separates free from paid and then stops, and the plans above differ on
    # seats, the assistant and API access instead.
    analytics_window_days: int
    # The AI publishing assistant that drafts and tightens copy.
    ai_assistant: bool
    # Whether this account may mint API keys at all.
    api_access: bool


@dataclass(frozen=True, slots=True)
class Plan:
    key: str
    name: str
    tagline: str
    entitlements: Entitlements
    # Monthly price in minor units, per currency. Empty for `free`, which is not
    # the same as zero-priced: there is nothing to buy, so there is no checkout.
    prices_minor: dict[str, int] = field(default_factory=dict)

    @property
    def is_free(self) -> bool:
        return not self.prices_minor


PLANS: dict[str, Plan] = {
    FREE: Plan(
        key=FREE,
        name="Free",
        tagline="Put the business on Mado and start posting.",
        entitlements=Entitlements(
            # Enough to be genuinely useful, which BUSINESS-06 asks for
            # explicitly: "free users experience meaningful value". A free tier
            # that cannot hold a season's programme is a demo, and a demo does
            # not bring the supply the whole platform depends on.
            max_live_listings=5,
            max_team_members=1,
            analytics_window_days=30,
            ai_assistant=False,
            api_access=False,
        ),
    ),
    PROFESSIONAL: Plan(
        key=PROFESSIONAL,
        name="Professional",
        tagline="Publish without counting, with a team and the writing assistant.",
        entitlements=Entitlements(
            max_live_listings=None,
            max_team_members=5,
            analytics_window_days=90,
            ai_assistant=True,
            api_access=False,
        ),
        prices_minor={"ETB": 150_000, "USD": 2_900, "EUR": 2_900, "GBP": 2_500, "KES": 380_000},
    ),
    BUSINESS: Plan(
        key=BUSINESS,
        name="Business",
        tagline="For somewhere with several venues and a programme to run.",
        entitlements=Entitlements(
            max_live_listings=None,
            max_team_members=25,
            analytics_window_days=90,
            ai_assistant=True,
            api_access=True,
        ),
        prices_minor={"ETB": 500_000, "USD": 9_900, "EUR": 9_900, "GBP": 8_500, "KES": 1_290_000},
    ),
    ENTERPRISE: Plan(
        key=ENTERPRISE,
        name="Enterprise",
        tagline="Tourism boards, hotel groups and universities.",
        entitlements=Entitlements(
            max_live_listings=None,
            max_team_members=None,
            analytics_window_days=90,
            ai_assistant=True,
            api_access=True,
        ),
        # Deliberately no price list. An enterprise agreement is negotiated -
        # BUSINESS-06 says so - and a number here would be one the sales
        # conversation immediately contradicts. It is granted, not bought.
    ),
}

# The order they are shown in, which is not dictionary order by accident: it is
# cheapest first, because that is how somebody reads a price list.
PLAN_ORDER = (FREE, PROFESSIONAL, BUSINESS, ENTERPRISE)

# Plans somebody may buy for themselves. Free needs no transaction and
# enterprise needs a conversation.
PURCHASABLE = (PROFESSIONAL, BUSINESS)


# --- Subscription states -----------------------------------------------------
#
# `past_due` exists between a period ending and a renewal being paid. It is not
# `cancelled`: the account has not left, and treating a late renewal as a
# departure would strip a paying customer of their team on the day their card
# was declined.

STATUS_ACTIVE = "active"
STATUS_PAST_DUE = "past_due"
STATUS_CANCELLED = "cancelled"
STATUS_PENDING = "pending"

# The states that can still grant what was bought - subject to the period, which
# `entitlements.plan_of` checks separately and which is what actually ends it.
#
# `pending` is absent: a subscription started and not paid for must give
# nothing, or the checkout is optional.
#
# `cancelled` is present, and has to be. Somebody who bought a month and
# cancelled on day three has paid for the month; dropping them to free that
# afternoon would keep their money and withdraw the thing it bought. Cancelling
# means "does not continue", and since nothing renews, that is entirely a
# statement about the end of the period. `SubscriptionService.cancel` guarantees
# there is one, so this can never grant indefinitely.
#
# `past_due` is absent: it exists for a period that ended without a renewal, and
# there is nothing left to grant.
STATUS_GRANTING = frozenset({STATUS_ACTIVE, STATUS_CANCELLED})


def get(key: str | None) -> Plan:
    """The plan for a key, falling back to free.

    An unknown key returns free rather than raising, for the same reason
    `permissions.granted_to` returns nothing for an unknown role: a row holding a
    plan this build does not recognise - a rollback, a half-finished migration -
    must fail closed. A caller that has to catch an exception to discover that
    will eventually forget to.
    """
    return PLANS.get(key or FREE, PLANS[FREE])


def price_for(plan_key: str, currency: str) -> int | None:
    """What this plan costs in this currency, or None if it is not sold in it.

    None rather than a converted figure. Quoting a price Mado has not decided to
    charge, arrived at through an exchange rate that moved this morning, is a
    number nobody chose.
    """
    return get(plan_key).prices_minor.get(currency.upper())


def is_purchasable(plan_key: str) -> bool:
    return plan_key in PURCHASABLE


def sold_in() -> list[str]:
    """Every currency any plan is priced in.

    Derived from the price lists rather than kept beside them, so adding a
    country to one plan cannot leave this saying otherwise.
    """
    found: set[str] = set()
    for plan in PLANS.values():
        found.update(plan.prices_minor)
    return sorted(found)


__all__ = [
    "BUSINESS",
    "ENTERPRISE",
    "FREE",
    "PLANS",
    "PLAN_ORDER",
    "PROFESSIONAL",
    "PURCHASABLE",
    "STATUS_ACTIVE",
    "STATUS_CANCELLED",
    "STATUS_GRANTING",
    "STATUS_PAST_DUE",
    "STATUS_PENDING",
    "Entitlements",
    "Plan",
    "get",
    "is_purchasable",
    "price_for",
    "sold_in",
]
