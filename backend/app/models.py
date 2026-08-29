"""Model registry.

Importing this module registers every domain's tables on ``Base.metadata``, which
Alembic autogenerate and the test fixtures both depend on. Without a single import
point, a domain that no route happens to import would silently vanish from
migrations.
"""

from app.core.database import Base
from app.domains.ai.models import Conversation, Message, UserMemory
from app.domains.catalog.models import (
    Category,
    City,
    EventInstance,
    Experience,
    Media,
    Neighborhood,
    Tag,
    Venue,
    experience_tags,
)
from app.domains.commerce.models import (
    LedgerEntry,
    Order,
    OrderLine,
    PaymentEvent,
    Payout,
    Ticket,
    TicketType,
)
from app.domains.developer.keys import ApiKey
from app.domains.developer.usage import ApiUsage, DeveloperAccount
from app.domains.developer.webhooks import WebhookDelivery, WebhookEndpoint
from app.domains.explorer.models import (
    Collection,
    CollectionItem,
    ContentReport,
    InteractionEvent,
    Itinerary,
    ItineraryStop,
    Review,
    SavedItem,
)
from app.domains.explorer.notifications import Notification
from app.domains.explorer.reservations import Reservation
from app.domains.identity.models import AuthIdentity, User, UserProfile, UserSession
from app.domains.identity.tokens import AccountToken
from app.domains.promotion.models import Promotion, PromotionDay
from app.domains.publisher.models import (
    GalleryItem,
    Publisher,
    PublisherMember,
    Subscription,
    SubscriptionInvoice,
)
from app.domains.trust.audit import AuditEntry
from app.domains.trust.flags import FeatureFlag

__all__ = [
    "ApiKey",
    "ApiUsage",
    "AuditEntry",
    "DeveloperAccount",
    "FeatureFlag",
    "GalleryItem",
    "WebhookDelivery",
    "WebhookEndpoint",
    "Reservation",
    "LedgerEntry",
    "Order",
    "OrderLine",
    "PaymentEvent",
    "Payout",
    "Ticket",
    "TicketType",
    "Collection",
    "CollectionItem",
    "AccountToken",
    "Base",
    "AuthIdentity",
    "Category",
    "ContentReport",
    "City",
    "Conversation",
    "EventInstance",
    "Experience",
    "InteractionEvent",
    "Notification",
    "Itinerary",
    "ItineraryStop",
    "Media",
    "Message",
    "Neighborhood",
    "Promotion",
    "PromotionDay",
    "Publisher",
    "PublisherMember",
    "Review",
    "SavedItem",
    "Subscription",
    "SubscriptionInvoice",
    "Tag",
    "User",
    "UserMemory",
    "UserProfile",
    "UserSession",
    "Venue",
    "experience_tags",
]
