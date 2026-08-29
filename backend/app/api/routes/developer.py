"""The developer platform (spec DEV-001, DEV-003).

Everything here belongs to the caller. There is no administrator view of other
people's keys or endpoints, and there is deliberately no endpoint that returns a
key or a secret after the moment it was created.

One route authenticates with a key rather than a session: `/developer/whoami`,
so an integration can check its credential works before it starts writing
things. The catalogue and publishing routes accept keys through the same
dependency.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Request, Response, status
from pydantic import Field

from app.api.deps import ApiKeyUser, CurrentUser, SessionDep
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import NotFoundError
from app.domains.catalog.schemas import CamelModel
from app.domains.developer import keys as key_module
from app.domains.developer import sdk as sdk_module
from app.domains.developer import usage
from app.domains.developer import webhooks as webhook_module
from app.domains.developer.keys import ApiKeyService
from app.domains.developer.webhooks import WebhookService

router = APIRouter(tags=["developer"])


# ------------------------------------------------------------------- shapes


class SdkOut(CamelModel):
    language: str
    label: str
    version: str
    filename: str
    files: list[str]


class ScopeOut(CamelModel):
    key: str
    description: str


class EventTypeOut(CamelModel):
    type: str
    description: str


class ApiKeyOut(CamelModel):
    id: uuid.UUID
    name: str
    preview: str
    scopes: list[str]
    state: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class NewApiKeyOut(CamelModel):
    """The only response that ever carries the key itself."""

    key: ApiKeyOut
    secret: str


class CreateKeyRequest(CamelModel):
    name: str = Field(max_length=key_module.MAX_NAME)
    scopes: list[str]
    expires_in_days: int | None = Field(default=None, ge=1, le=key_module.MAX_TTL_DAYS)


class EndpointOut(CamelModel):
    id: uuid.UUID
    url: str
    events: list[str]
    status: str
    secret_preview: str
    consecutive_failures: int
    created_at: datetime
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None


class NewEndpointOut(CamelModel):
    endpoint: EndpointOut
    secret: str


class CreateEndpointRequest(CamelModel):
    url: str = Field(max_length=webhook_module.MAX_URL)
    events: list[str]


class UpdateEndpointRequest(CamelModel):
    url: str | None = Field(default=None, max_length=webhook_module.MAX_URL)
    events: list[str] | None = None
    status: str | None = None


class DeliveryOut(CamelModel):
    id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    is_test: bool
    created_at: datetime
    next_attempt_at: datetime
    delivered_at: datetime | None = None
    response_status: int | None = None
    error: str | None = None
    duration_ms: int | None = None


class WhoAmIOut(CamelModel):
    key_id: uuid.UUID
    key_name: str
    scopes: list[str]
    owner_id: uuid.UUID


# ------------------------------------------------------------------ metadata


@router.get(
    "/developer/scopes",
    response_model=CollectionEnvelope[ScopeOut],
    summary="What a key can be allowed to do",
    description=(
        "Public, and intentionally short. A scope list that grows with every "
        "endpoint stops being something anybody reads before ticking boxes."
    ),
)
async def scopes() -> CollectionEnvelope[ScopeOut]:
    return CollectionEnvelope(
        data=[ScopeOut(key=k, description=v) for k, v in key_module.SCOPES.items()]
    )


@router.get(
    "/developer/event-types",
    response_model=CollectionEnvelope[EventTypeOut],
    summary="What a webhook can be told about",
)
async def event_types() -> CollectionEnvelope[EventTypeOut]:
    return CollectionEnvelope(
        data=[EventTypeOut(type=k, description=v) for k, v in webhook_module.EVENT_TYPES.items()]
    )


# ---------------------------------------------------------------- api keys


@router.get(
    "/developer/keys",
    response_model=CollectionEnvelope[ApiKeyOut],
    summary="Your API keys",
    description=(
        "Revoked and expired keys stay in the list. A credential that vanishes "
        "leaves no answer to what it was doing before it was withdrawn."
    ),
)
async def list_keys(session: SessionDep, user: CurrentUser) -> CollectionEnvelope[ApiKeyOut]:
    rows = await ApiKeyService(session).mine(user)
    return CollectionEnvelope(data=[_key_out(row) for row in rows])


@router.post(
    "/developer/keys",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[NewApiKeyOut],
    summary="Mint a key",
    description=(
        "The key is in this response and nowhere else. Only its digest is "
        "stored, so there is no endpoint that can show it again - if it is "
        "lost, revoke it and make another."
    ),
)
async def create_key(
    payload: CreateKeyRequest, session: SessionDep, user: CurrentUser
) -> Envelope[NewApiKeyOut]:
    key, plaintext = await ApiKeyService(session).issue(
        user,
        name=payload.name,
        scopes=payload.scopes,
        expires_in_days=payload.expires_in_days,
    )
    view = NewApiKeyOut(key=_key_out(key), secret=plaintext)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/developer/keys/{key_id}",
    response_model=Envelope[ApiKeyOut],
    summary="Revoke a key",
    description="Immediate and permanent. A revoked key cannot be brought back.",
)
async def revoke_key(
    key_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> Envelope[ApiKeyOut]:
    key = await ApiKeyService(session).revoke(user, key_id)
    view = _key_out(key)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/developer/whoami",
    response_model=Envelope[WhoAmIOut],
    summary="Check a key works",
    description=(
        "Authenticates with `X-Mado-Api-Key` rather than a session, so an "
        "integration can verify its credential before it starts writing."
    ),
)
async def whoami(request: Request, user: ApiKeyUser) -> Envelope[WhoAmIOut]:
    key = request.state.api_key
    return Envelope(
        data=WhoAmIOut(
            key_id=key.id, key_name=key.name, scopes=key.scopes, owner_id=user.id
        )
    )


# ------------------------------------------------------------------- usage


class UsageDayOut(CamelModel):
    day: date
    calls: int


class UsageOut(CamelModel):
    plan: str
    plan_name: str
    calls_this_month: int
    # Null is unmetered, which is what an enterprise agreement buys. Zero would
    # read as "no calls allowed", which is the opposite.
    monthly_allowance: int | None = None
    # The day the current allowance resets, so a developer who has run out knows
    # whether to wait or to buy.
    resets_on: date
    daily: list[UsageDayOut]


@router.get(
    "/developer/usage",
    response_model=Envelope[UsageOut],
    summary="How many API calls this account has made",
    description=(
        "Counted per key per UTC day and summed here. A quota is not a rate "
        "limit: running out returns `QUOTA_EXCEEDED` and waiting will not help "
        "until the first of the month, whereas `RATE_LIMIT_EXCEEDED` means slow "
        "down and try again shortly."
    ),
)
async def api_usage(session: SessionDep, user: CurrentUser) -> Envelope[UsageOut]:
    account = await usage.account_for(session, user.id)
    plan = usage.plan_for(account.plan if account else None)
    return Envelope(
        data=UsageOut(
            plan=plan.key,
            plan_name=plan.name,
            calls_this_month=await usage.calls_this_month(session, user.id),
            monthly_allowance=await usage.allowance_for(session, user.id),
            resets_on=usage.next_month_start(),
            daily=[
                UsageDayOut(day=day, calls=calls)
                for day, calls in await usage.daily_breakdown(session, user.id)
            ],
        )
    )


# ---------------------------------------------------------------- webhooks


@router.get(
    "/webhooks",
    response_model=CollectionEnvelope[EndpointOut],
    summary="Your webhook endpoints",
)
async def list_endpoints(
    session: SessionDep, user: CurrentUser
) -> CollectionEnvelope[EndpointOut]:
    rows = await WebhookService(session).mine(user)
    return CollectionEnvelope(data=[_endpoint_out(row) for row in rows])


@router.post(
    "/webhooks",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[NewEndpointOut],
    summary="Subscribe an endpoint",
    description=(
        "The URL is checked before it is accepted: https, no credentials in "
        "the URL, and it must resolve to an address on the public internet. The "
        "same check runs again before every delivery, because DNS is the "
        "receiver's to change.\n\n"
        "The signing secret is in this response. It is shown again only when "
        "rotated."
    ),
)
async def subscribe(
    payload: CreateEndpointRequest, session: SessionDep, user: CurrentUser
) -> Envelope[NewEndpointOut]:
    endpoint, secret = await WebhookService(session).subscribe(
        user, url=payload.url, events=payload.events
    )
    view = NewEndpointOut(endpoint=_endpoint_out(endpoint), secret=secret)
    await session.commit()
    return Envelope(data=view)


@router.patch(
    "/webhooks/{endpoint_id}",
    response_model=Envelope[EndpointOut],
    summary="Change an endpoint",
    description=(
        "Setting the status back to active also clears the failure count, so an "
        "endpoint that was suspended and then fixed gets a full run of chances."
    ),
)
async def update_endpoint(
    endpoint_id: uuid.UUID,
    payload: UpdateEndpointRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[EndpointOut]:
    endpoint = await WebhookService(session).update(
        user, endpoint_id, url=payload.url, events=payload.events, status=payload.status
    )
    view = _endpoint_out(endpoint)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/webhooks/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unsubscribe",
    description="Queued deliveries go with it - nothing is posted to a URL its owner withdrew.",
)
async def unsubscribe(endpoint_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> None:
    await WebhookService(session).delete(user, endpoint_id)
    await session.commit()


@router.post(
    "/webhooks/{endpoint_id}/rotate-secret",
    response_model=Envelope[NewEndpointOut],
    summary="Replace the signing secret",
    description="The old secret stops working immediately. There is no overlap period.",
)
async def rotate_secret(
    endpoint_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> Envelope[NewEndpointOut]:
    endpoint, secret = await WebhookService(session).rotate_secret(user, endpoint_id)
    view = NewEndpointOut(endpoint=_endpoint_out(endpoint), secret=secret)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/webhooks/{endpoint_id}/deliveries",
    response_model=CollectionEnvelope[DeliveryOut],
    summary="What has been sent",
)
async def deliveries(
    endpoint_id: uuid.UUID, session: SessionDep, user: CurrentUser, limit: int = 50
) -> CollectionEnvelope[DeliveryOut]:
    rows = await WebhookService(session).deliveries(user, endpoint_id, limit=limit)
    return CollectionEnvelope(data=[DeliveryOut.model_validate(row) for row in rows])


@router.post(
    "/webhooks/{endpoint_id}/deliveries/{delivery_id}/retry",
    response_model=Envelope[DeliveryOut],
    summary="Send it again",
    description=(
        "Keeps the original event id, so a receiver that did process it and "
        "merely failed to say so discards the duplicate rather than acting twice."
    ),
)
async def retry_delivery(
    endpoint_id: uuid.UUID,
    delivery_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[DeliveryOut]:
    delivery = await WebhookService(session).retry(user, endpoint_id, delivery_id)
    view = DeliveryOut.model_validate(delivery)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/webhooks/{endpoint_id}/test",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[DeliveryOut],
    summary="Send a test event",
    description=(
        "Queued like any other delivery, and marked as a test in the payload and "
        "in the history so a receiver can tell a drill from the real thing."
    ),
)
async def send_test(
    endpoint_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> Envelope[DeliveryOut]:
    delivery = await WebhookService(session).send_test(user, endpoint_id)
    view = DeliveryOut.model_validate(delivery)
    await session.commit()
    return Envelope(data=view)


# ---------------------------------------------------------------- sdks


@router.get(
    "/developer/sdks",
    response_model=CollectionEnvelope[SdkOut],
    summary="Client libraries available to download",
    description=(
        "Generated from this API's own description, so what they can do and "
        "what the API can do cannot drift apart. The version is the API "
        "version plus a digest of the endpoint surface: if it changes, "
        "something a caller can see changed.\n\n"
        "Public, like the scope list and the OpenAPI document it is built "
        "from. A generated client contains nothing that is not already in a "
        "published description, and somebody deciding whether to integrate "
        "should be able to read one before they have an account."
    ),
)
async def sdks(request: Request) -> CollectionEnvelope[SdkOut]:
    return CollectionEnvelope(
        data=[SdkOut(**entry) for entry in sdk_module.catalogue(request.app)]
    )


@router.get(
    "/developer/sdks/{language}",
    summary="Download a client library",
    response_class=Response,
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "A zip holding the client, a README and a worked example.",
        }
    },
)
async def download_sdk(language: str, request: Request) -> Response:
    """Build the SDK now rather than serving a stored copy.

    A file built at release time is a file that is stale the first time a route
    changes, and nothing about the download says so. Generating on request costs
    a few milliseconds and cannot be out of date.
    """
    try:
        bundle = sdk_module.build(request.app, language.lower())
    except KeyError:
        raise NotFoundError(
            f"No SDK for '{language}'. Available: "
            f"{', '.join(sorted(sdk_module.LANGUAGES))}.",
            code="SDK_LANGUAGE_UNKNOWN",
        ) from None

    return Response(
        content=bundle.archive(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            # The generated client is only as current as the API it was built
            # from, so it must not sit in a proxy after a deployment.
            "Cache-Control": "no-store",
        },
    )


# ------------------------------------------------------------------ mapping


def _key_out(key) -> ApiKeyOut:
    # `state` is a property rather than a column, so this cannot be
    # `model_validate` without teaching pydantic about it.
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        preview=key.preview,
        scopes=key.scopes,
        state=key.state,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
        revoked_at=key.revoked_at,
    )


def _endpoint_out(endpoint) -> EndpointOut:
    return EndpointOut(
        id=endpoint.id,
        url=endpoint.url,
        events=endpoint.events,
        status=endpoint.status,
        secret_preview=endpoint.secret_preview,
        consecutive_failures=endpoint.consecutive_failures,
        created_at=endpoint.created_at,
        last_success_at=endpoint.last_success_at,
        last_failure_at=endpoint.last_failure_at,
        last_error=endpoint.last_error,
    )
