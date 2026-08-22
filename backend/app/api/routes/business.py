"""Business accounts and their teams.

**An account is a person or a business, never both.** Registration is unchanged;
shortly afterwards the account answers which it is. Choosing business asks for
the details and converts the account - from then on it *is* the business: the
profile page, the name on everything it posts, and what other explorers see.
There is no personal profile alongside it and no second login, because the same
credentials sign in to the same account.

That is why there is no "create a business" here and no list of businesses you
own. There is one account, and `/me/account-type` says what it is.

Conversion is one way. Turning back would leave published listings, reviews of
them and any tickets sold attributed to a business that no longer exists.

Every route below goes through `assert_can_manage`, which is the one gate. Routes
do not decide authorization for themselves, because the failure mode of a
scattered check is not a visible bug - it is the one endpoint that forgot.

The explorer-facing half is at the bottom: a public profile by slug, deliberately
a different payload from what the team sees. An owner needs verification state
and contact details; somebody deciding where to have dinner needs the pictures
and what is on.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, Request, UploadFile, status

from app.api.deps import CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import NotFoundError, ValidationError
from app.domains.catalog.schemas import ExperienceSummary
from app.domains.catalog.serializers import to_summary
from app.domains.publisher import business as business_vocab
from app.domains.publisher import permissions
from app.domains.publisher.models import (
    GALLERY_IMAGE,
    GALLERY_VIDEO,
    GalleryItem,
    Publisher,
    PublisherMember,
)
from app.domains.publisher.schemas import (
    AccountTypeOut,
    AddGalleryItemRequest,
    BusinessOut,
    ChangeRoleRequest,
    CreateBusinessRequest,
    GalleryItemOut,
    InvitationOut,
    InviteMemberRequest,
    MemberOut,
    PublishingIdentityOut,
    RoleOut,
    UpdateBusinessRequest,
)
from app.domains.publisher.service import PublishingService
from app.integrations import media_storage

router = APIRouter(tags=["business"])


def _view(publisher: Publisher) -> BusinessOut:
    return BusinessOut(
        id=publisher.id,
        name=publisher.name,
        slug=publisher.slug,
        type=publisher.type,
        business_type=publisher.business_type,
        business_type_label=(
            business_vocab.label(publisher.business_type) if publisher.business_type else None
        ),
        description=publisher.description,
        industry=publisher.industry,
        website=publisher.website,
        contact=publisher.contact or {},
        social=publisher.social or {},
        logo_url=publisher.logo_url,
        cover_url=publisher.cover_url,
        verification_status=publisher.verification_status,
        trust_level=publisher.trust_level,
        created_at=publisher.created_at,
    )


def _gallery_view(item: GalleryItem) -> GalleryItemOut:
    return GalleryItemOut(
        id=item.id,
        kind=item.kind,
        url=item.url,
        caption=item.caption,
        sort_order=item.sort_order,
        width=item.width,
        height=item.height,
        content_type=item.content_type,
        created_at=item.created_at,
    )


def _member_view(member: PublisherMember, *, display_name: str | None = None) -> MemberOut:
    return MemberOut(
        id=member.id,
        role=member.role,
        role_label=permissions.label(member.role),
        status=member.status,
        user_id=member.user_id,
        display_name=display_name,
        invited_email=member.invited_email,
        invited_at=member.invited_at,
        expires_at=member.expires_at,
        permissions=sorted(permissions.granted_to(member.role)),
    )


# ------------------------------------------------------------------ businesses


@router.get(
    "/businesses/roles",
    response_model=CollectionEnvelope[RoleOut],
    summary="Roles that can be assigned to a team member",
    description=(
        "Owner is absent on purpose: ownership is a property of the business, not "
        "a role, and cannot be granted by inviting somebody."
    ),
)
async def list_roles() -> CollectionEnvelope[RoleOut]:
    return CollectionEnvelope(
        data=[
            RoleOut(
                value=role,
                label=permissions.label(role),
                permissions=sorted(permissions.granted_to(role)),
            )
            for role in permissions.ASSIGNABLE_ROLES
        ]
    )


@router.get(
    "/me/account-type",
    response_model=Envelope[AccountTypeOut],
    summary="Whether this account is a person or a business",
    description=(
        "`chosen` is false when the account has never answered - it predates the "
        "question, or has just registered. The interface asks once on that basis; "
        "the type itself defaults to individual so nothing is ever in limbo."
    ),
)
async def my_account_type(
    user: CurrentUser, session: SessionDep
) -> Envelope[AccountTypeOut]:
    business = await PublishingService(session).business_of(user)
    return Envelope(
        data=AccountTypeOut(
            account_type=user.account_type,
            chosen=user.account_type_chosen,
            business=_view(business) if business else None,
        )
    )


@router.post(
    "/me/account-type/individual",
    response_model=Envelope[AccountTypeOut],
    summary="Say this account is a person",
    description="Records the answer so you are not asked again. Nothing else changes.",
)
async def choose_individual(
    user: CurrentUser, session: SessionDep
) -> Envelope[AccountTypeOut]:
    await PublishingService(session).choose_individual(user)
    view = AccountTypeOut(
        account_type=user.account_type, chosen=True, business=None
    )
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/me/account-type/business",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AccountTypeOut],
    summary="Turn this account into a business",
    description=(
        "One way, and once. From here the account **is** the business: its profile, "
        "the name on everything it posts, and what other explorers see. There is no "
        "personal profile left alongside it, and no second login - the same "
        "credentials sign in to the same account.\n\n"
        "Anything posted as a person before converting stays attributed to that "
        "person. Rewriting it to claim the business wrote it would misstate who was "
        "accountable at the time."
    ),
)
async def become_business(
    payload: CreateBusinessRequest, user: CurrentUser, session: SessionDep
) -> Envelope[AccountTypeOut]:
    publisher = await PublishingService(session).become_business(
        user,
        name=payload.name,
        business_type=payload.business_type,
        description=payload.description,
        website=payload.website,
        contact=payload.contact,
        social=payload.social,
        logo_url=payload.logo_url,
        cover_url=payload.cover_url,
    )
    view = AccountTypeOut(
        account_type=user.account_type, chosen=True, business=_view(publisher)
    )
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/me/publishing-identities",
    response_model=CollectionEnvelope[PublishingIdentityOut],
    summary="What you may post as",
    description=(
        "Usually one entry, and the composer then offers no choice - an individual "
        "posts as themselves, a business as itself. A second appears only for "
        "somebody invited to another business, who genuinely does have two."
    ),
)
async def publishing_identities(
    user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[PublishingIdentityOut]:
    found = await PublishingService(session).publishing_identities(user)
    return CollectionEnvelope(
        data=[
            PublishingIdentityOut(
                id=publisher.id,
                name=publisher.name,
                type=publisher.type,
                logo_url=publisher.logo_url,
                is_default=is_default,
            )
            for publisher, is_default in found
        ]
    )


@router.get(
    "/businesses/{business_id}",
    response_model=Envelope[BusinessOut],
    summary="Read a business you manage",
)
async def get_business(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[BusinessOut]:
    service = PublishingService(session)
    publisher = await service.assert_can_manage(
        user, business_id, permission=permissions.PROFILE_VIEW
    )
    return Envelope(data=_view(publisher))


@router.patch(
    "/businesses/{business_id}",
    response_model=Envelope[BusinessOut],
    summary="Edit a business",
)
async def update_business(
    business_id: uuid.UUID,
    payload: UpdateBusinessRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[BusinessOut]:
    changes = payload.model_dump(exclude_unset=True)
    publisher = await PublishingService(session).update_business(user, business_id, changes)
    view = _view(publisher)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/businesses/{business_id}/permissions",
    response_model=Envelope[list[str]],
    summary="What you may do to this business",
    description=(
        "So the interface can hide what the caller cannot do. This is a "
        "convenience, never the enforcement - every action is checked again "
        "server-side when it is attempted."
    ),
)
async def my_permissions(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[list[str]]:
    held = await PublishingService(session).permissions_for(user, business_id)
    if not held:
        raise NotFoundError("Business not found.", code="PUBLISHER_NOT_FOUND")
    return Envelope(data=sorted(held))


# ------------------------------------------------------------------------ team


@router.get(
    "/businesses/{business_id}/members",
    response_model=CollectionEnvelope[MemberOut],
    summary="List team members and pending invitations",
)
async def list_members(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[MemberOut]:
    service = PublishingService(session)
    members = await service.list_members(user, business_id)
    names = await service.display_names_for(
        [member.user_id for member in members if member.user_id]
    )
    return CollectionEnvelope(
        data=[
            _member_view(member, display_name=names.get(member.user_id))
            for member in members
        ]
    )


@router.post(
    "/businesses/{business_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[MemberOut],
    summary="Invite somebody to help manage this business",
)
async def invite_member(
    business_id: uuid.UUID,
    payload: InviteMemberRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[MemberOut]:
    member = await PublishingService(session).invite_member(
        user, business_id, email=payload.email, role=payload.role
    )
    view = _member_view(member)
    await session.commit()
    return Envelope(data=view)


@router.patch(
    "/businesses/{business_id}/members/{member_id}",
    response_model=Envelope[MemberOut],
    summary="Change a team member's role",
)
async def change_role(
    business_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: ChangeRoleRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[MemberOut]:
    member = await PublishingService(session).change_member_role(
        user, business_id, member_id, role=payload.role
    )
    view = _member_view(member)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/businesses/{business_id}/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member, or revoke their invitation",
)
async def remove_member(
    business_id: uuid.UUID,
    member_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await PublishingService(session).remove_member(user, business_id, member_id)
    await session.commit()


# --------------------------------------------------------------------- gallery


@router.get(
    "/businesses/{business_id}/gallery",
    response_model=CollectionEnvelope[GalleryItemOut],
    summary="Photos and videos on this business's profile",
)
async def list_gallery(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[GalleryItemOut]:
    items = await PublishingService(session).list_gallery(user, business_id)
    return CollectionEnvelope(data=[_gallery_view(item) for item in items])


@router.post(
    "/businesses/{business_id}/gallery/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[GalleryItemOut],
    summary="Upload a photo or a video to this business's profile",
    description=(
        "One endpoint for both, because the uploader is one file picker and "
        "making the client decide which endpoint to call would mean it deciding "
        "what the file is - which is the thing this route does not trust it "
        "about.\n\n"
        "An image is decoded and re-encoded, which proves it is an image, strips "
        "EXIF including any GPS coordinates, and resizes it for serving. A video "
        "is identified from its container's own magic bytes; MP4 and WebM are "
        "accepted, and the stored extension comes from what was recognised "
        "rather than from the uploaded filename."
    ),
)
async def upload_gallery_item(
    business_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
) -> Envelope[GalleryItemOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.UPLOAD_LIMIT)

    service = PublishingService(session)
    # Before a byte is read. An upload route that processes the file first does
    # the expensive work for anybody who asks, whether or not they may store it.
    await service.assert_can_manage(user, business_id, permission=permissions.PROFILE_EDIT)

    # The file's own opening bytes choose the verifier, not its Content-Type or
    # its name - both are written by the client, and `photo.jpg` renamed to
    # `.mp4` would otherwise pick the path that cannot catch it. The declared
    # type is consulted only when the bytes are unrecognisable, and then purely
    # so the refusal talks about what the uploader thought they were sending.
    head = await file.read(media_storage.SNIFF_BYTES)
    declared = (file.content_type or "").lower()

    if media_storage.looks_like_video(head) or declared.startswith("video/"):
        with media_storage.VideoUpload() as upload:
            upload.feed(head)
            # A megabyte at a time from here. Reading a hundred-megabyte video
            # whole before the size check makes the size check decorative, and
            # doing it per concurrent request is how the API runs out of memory.
            while chunk := await file.read(1024 * 1024):
                upload.feed(chunk)
            stored_video = upload.finish(owner_id=user.id)

        item = await service.add_gallery_item(
            user,
            business_id,
            kind=GALLERY_VIDEO,
            url=stored_video.url,
            caption=caption,
            content_type=stored_video.content_type,
        )
    else:
        # `head` was already taken off the stream, so the rest is read on top of
        # it rather than instead of it - the cap still bounds the whole file.
        data = head + await file.read(media_storage.MAX_UPLOAD_BYTES + 1 - len(head))
        if len(data) > media_storage.MAX_UPLOAD_BYTES:
            raise ValidationError(
                f"Images must be under {media_storage.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                code="UPLOAD_TOO_LARGE",
            )
        stored = media_storage.store(data, owner_id=user.id)
        item = await service.add_gallery_item(
            user,
            business_id,
            kind=GALLERY_IMAGE,
            url=stored.url,
            caption=caption,
            width=stored.width,
            height=stored.height,
            content_type="image/webp",
        )

    view = _gallery_view(item)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/businesses/{business_id}/gallery",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[GalleryItemOut],
    summary="Add media already hosted somewhere else",
    description=(
        "For a business whose pictures already live on their own site, and for "
        "the seed. Uploading is the path in the interface."
    ),
)
async def add_gallery_item(
    business_id: uuid.UUID,
    payload: AddGalleryItemRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[GalleryItemOut]:
    item = await PublishingService(session).add_gallery_item(
        user,
        business_id,
        kind=payload.kind,
        url=payload.url,
        caption=payload.caption,
    )
    view = _gallery_view(item)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/businesses/{business_id}/gallery/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take a photo or video off the profile",
)
async def remove_gallery_item(
    business_id: uuid.UUID,
    item_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await PublishingService(session).remove_gallery_item(user, business_id, item_id)
    await session.commit()


# ----------------------------------------------------------------- invitations


@router.get(
    "/me/business-invitations",
    response_model=CollectionEnvelope[InvitationOut],
    summary="Invitations waiting for you",
)
async def my_invitations(
    user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[InvitationOut]:
    pending = await PublishingService(session).pending_invitations(user)
    return CollectionEnvelope(
        data=[
            InvitationOut(
                id=member.id,
                role=member.role,
                role_label=permissions.label(member.role),
                business_id=member.publisher.id,
                business_name=member.publisher.name,
                business_slug=member.publisher.slug,
                invited_at=member.invited_at,
                expires_at=member.expires_at,
            )
            for member in pending
        ]
    )


@router.post(
    "/me/business-invitations/{member_id}/accept",
    response_model=Envelope[MemberOut],
    summary="Accept an invitation",
)
async def accept_invitation(
    member_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[MemberOut]:
    member = await PublishingService(session).accept_invitation(user, member_id)
    view = _member_view(member)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/me/business-invitations/{member_id}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Decline an invitation",
)
async def decline_invitation(
    member_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await PublishingService(session).decline_invitation(user, member_id)
    await session.commit()


# --------------------------------------------------------------- public profile


class PublicBusinessOut(BusinessOut):
    """What an explorer sees.

    A different payload from the team's on purpose. Contact details and social
    links are things a business chose to publish, so they stay; verification
    state stays because it is exactly what an explorer wants to know about a
    stranger's claim. What is dropped is everything only the owner has a reason
    to see.
    """

    listings: list[ExperienceSummary] = []
    # Sent with the profile rather than fetched when the tab is opened. It is one
    # ordered list of rows already keyed by this publisher, and a second round
    # trip to get it would make the tab blank for as long as that request takes -
    # for a gallery, which is the part of the page people came to look at.
    gallery: list[GalleryItemOut] = []


@router.get(
    "/businesses/by-slug/{slug}",
    response_model=Envelope[PublicBusinessOut],
    summary="A business profile, as an explorer sees it",
    description=(
        "Public. Returns the business and what it currently has published - "
        "nothing in draft, nothing withheld by moderation, because this is the "
        "same catalogue read every other discovery surface uses."
    ),
)
async def public_business(
    slug: str, session: SessionDep, user: OptionalUser
) -> Envelope[PublicBusinessOut]:
    service = PublishingService(session)
    publisher = await service.business_by_slug(slug)
    if publisher is None:
        raise NotFoundError("Business not found.", code="PUBLISHER_NOT_FOUND")

    listings = await service.published_listings_of(publisher.id)
    gallery = await service.gallery_of(publisher.id)
    view = PublicBusinessOut(
        **_view(publisher).model_dump(by_alias=False),
        listings=[to_summary(experience) for experience in listings],
        gallery=[_gallery_view(item) for item in gallery],
    )
    return Envelope(data=view)
