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
from app.domains.identity.models import AuthIdentity, User, UserProfile, UserSession
from app.domains.identity.tokens import AccountToken
from app.domains.publisher.models import Publisher

__all__ = [
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
    "Publisher",
    "Review",
    "SavedItem",
    "Tag",
    "User",
    "UserMemory",
    "UserProfile",
    "UserSession",
    "Venue",
    "experience_tags",
]
