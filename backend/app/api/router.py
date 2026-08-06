"""API v1 router.

Route order matters: ``catalog`` is mounted after ``discovery`` so that literal
paths like ``/search`` are matched before any parameterised sibling can shadow them.
"""

from fastapi import APIRouter

from app.api.routes import auth, catalog, concierge, discovery, me

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(discovery.router)
api_router.include_router(catalog.router)
api_router.include_router(concierge.router)
