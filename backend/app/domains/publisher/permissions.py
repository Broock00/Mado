"""Who may do what to a business.

Mado already had an authorization vocabulary and this follows it rather than
inventing a second one. `developer/keys.py` names permissions `resource:action`
and states the rule this file is bound by:

> A scope nobody enforces is worse than no scope at all: it tells the person
> ticking the box that they have restricted something when they have not.

So the set below is deliberately smaller than the feature specification's list of
permission categories. The spec names Offers, Reviews-respond and Media as
separate areas; Mado has no offers concept and no mechanism for a business to
reply to a review, and media is edited through the post that owns it. Inventing
`offers:manage` would put a checkbox in front of an owner that governs nothing.
Those appear as known limitations instead, and get a permission on the day they
get a feature.

**Roles are named sets, not a second model.** A member holds one role; the role
maps to permissions here. That keeps the check one dictionary lookup, keeps the
database honest about what was granted, and means adding a permission to a role
is a code change somebody reviews rather than a row somebody edits in production.

Ownership is not a role. The owner is `publishers.owner_user_id`, which already
existed and is what every authorization check keyed on before this file. Owners
implicitly hold everything, including the things no role may ever grant.
"""

from __future__ import annotations

# --- Permissions -------------------------------------------------------------

PROFILE_VIEW = "profile:view"
PROFILE_EDIT = "profile:edit"

CONTENT_CREATE = "content:create"
CONTENT_EDIT = "content:edit"
CONTENT_PUBLISH = "content:publish"
CONTENT_DELETE = "content:delete"

EVENTS_MANAGE = "events:manage"

ANALYTICS_VIEW = "analytics:view"

TEAM_MANAGE = "team:manage"

# Only ever held by the owner. Kept as a named permission so the check reads the
# same as every other one rather than being a special case at the call site.
OWNERSHIP_TRANSFER = "ownership:transfer"
BUSINESS_DELETE = "business:delete"

PERMISSIONS: dict[str, str] = {
    PROFILE_VIEW: "See the business dashboard and its profile.",
    PROFILE_EDIT: "Change the business name, description, contact details and images.",
    CONTENT_CREATE: "Create posts for the business.",
    CONTENT_EDIT: "Edit the business's posts, including their media and prices.",
    CONTENT_PUBLISH: "Publish and withdraw posts.",
    CONTENT_DELETE: "Delete the business's posts.",
    EVENTS_MANAGE: "Add, reschedule and cancel dates on the business's posts.",
    ANALYTICS_VIEW: "See how the business's posts are performing.",
    TEAM_MANAGE: "Invite people, change their roles and remove them.",
    OWNERSHIP_TRANSFER: "Hand the business to somebody else.",
    BUSINESS_DELETE: "Delete the business.",
}

# --- Roles -------------------------------------------------------------------

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_EVENT_MANAGER = "event_manager"
ROLE_ANALYST = "analyst"

# Never granted through membership, and never assignable. The owner is a column
# on the publisher, and a row claiming to be one would be a second answer to
# "who owns this" that could disagree with the first.
UNASSIGNABLE_ROLES = frozenset({ROLE_OWNER})

# What a role may never carry, whoever writes the mapping. Belt and braces
# against a future edit that quietly hands ownership transfer to an admin: the
# check in `granted_to` filters against this rather than trusting the table.
OWNER_ONLY = frozenset({OWNERSHIP_TRANSFER, BUSINESS_DELETE})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset(PERMISSIONS),
    # Everything operational. Not ownership: an administrator who can hand the
    # business away can take it, and that is a different decision from being
    # trusted to run it.
    ROLE_ADMIN: frozenset({
        PROFILE_VIEW,
        PROFILE_EDIT,
        CONTENT_CREATE,
        CONTENT_EDIT,
        CONTENT_PUBLISH,
        CONTENT_DELETE,
        EVENTS_MANAGE,
        ANALYTICS_VIEW,
        TEAM_MANAGE,
    }),
    # Writes and publishes, but cannot delete the record of what was published
    # or change who else has access.
    #
    # `events:manage` is included, and has to be. An event's dates *are* its
    # content: without them an editor could write a listing and publish it, and
    # publishing would refuse because a date is required to go live. That was
    # not a theoretical gap - it broke the first end-to-end run of this feature,
    # with an editor holding `content:publish` and no way to reach it.
    #
    # `event_manager` below is still a distinct role, for somebody who manages
    # the programme without being able to create listings at all.
    ROLE_EDITOR: frozenset({
        PROFILE_VIEW,
        CONTENT_CREATE,
        CONTENT_EDIT,
        CONTENT_PUBLISH,
        EVENTS_MANAGE,
        ANALYTICS_VIEW,
    }),
    # The person who runs the programme: dates, cancellations, and editing the
    # posts those dates belong to.
    ROLE_EVENT_MANAGER: frozenset({
        PROFILE_VIEW,
        CONTENT_EDIT,
        EVENTS_MANAGE,
        ANALYTICS_VIEW,
    }),
    # Reads, and nothing else. Exists because "let the marketing agency see the
    # numbers" should not require handing them the ability to publish.
    ROLE_ANALYST: frozenset({PROFILE_VIEW, ANALYTICS_VIEW}),
}

ROLE_LABELS: dict[str, str] = {
    ROLE_OWNER: "Owner",
    ROLE_ADMIN: "Administrator",
    ROLE_EDITOR: "Editor",
    ROLE_EVENT_MANAGER: "Event manager",
    ROLE_ANALYST: "Analyst",
}

ASSIGNABLE_ROLES = tuple(role for role in ROLE_LABELS if role not in UNASSIGNABLE_ROLES)


def granted_to(role: str) -> frozenset[str]:
    """What a role actually carries.

    An unknown role grants nothing rather than raising. A membership row holding
    a role this build does not recognise - a rollback, a half-finished
    migration - must fail closed, and a caller that has to catch an exception to
    discover that will eventually forget to.
    """
    if role == ROLE_OWNER:
        return frozenset(PERMISSIONS)
    permissions = ROLE_PERMISSIONS.get(role, frozenset())
    # Owner-only permissions are stripped whatever the table says.
    return permissions - OWNER_ONLY


def role_allows(role: str, permission: str) -> bool:
    return permission in granted_to(role)


def is_assignable(role: str) -> bool:
    return role in ROLE_PERMISSIONS and role not in UNASSIGNABLE_ROLES


def label(role: str) -> str:
    return ROLE_LABELS.get(role, role.replace("_", " ").capitalize())


__all__ = [
    "ANALYTICS_VIEW",
    "ASSIGNABLE_ROLES",
    "BUSINESS_DELETE",
    "CONTENT_CREATE",
    "CONTENT_DELETE",
    "CONTENT_EDIT",
    "CONTENT_PUBLISH",
    "EVENTS_MANAGE",
    "OWNERSHIP_TRANSFER",
    "OWNER_ONLY",
    "PERMISSIONS",
    "PROFILE_EDIT",
    "PROFILE_VIEW",
    "ROLE_ADMIN",
    "ROLE_ANALYST",
    "ROLE_EDITOR",
    "ROLE_EVENT_MANAGER",
    "ROLE_LABELS",
    "ROLE_OWNER",
    "ROLE_PERMISSIONS",
    "TEAM_MANAGE",
    "granted_to",
    "is_assignable",
    "label",
    "role_allows",
]
