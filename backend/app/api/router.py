"""API v1 router.

Route order matters: ``catalog`` is mounted after ``discovery`` so that literal
paths like ``/search`` are matched before any parameterised sibling can shadow them.
"""

from fastapi import APIRouter

from app.api.routes import (
    admin,
    analytics,
    auth,
    catalog,
    collections,
    concierge,
    discovery,
    me,
    notifications,
    planning,
    publishing,
    reservations,
    reviews,
    trust,
)

api_router = APIRouter()
api_router.include_router(admin.router)
api_router.include_router(analytics.router)
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(discovery.router)
api_router.include_router(catalog.router)
api_router.include_router(collections.router)
api_router.include_router(concierge.router)
api_router.include_router(notifications.router)
api_router.include_router(planning.router)
api_router.include_router(publishing.router)
api_router.include_router(reservations.router)
api_router.include_router(reviews.router)
api_router.include_router(trust.router)
