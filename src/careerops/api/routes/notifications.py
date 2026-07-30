"""Notification API routes (Phase 9.1-9.2).

Provides:
- ``GET /api/v1/notifications/stream`` — SSE endpoint (cookie auth, no CSRF).
- ``GET /api/v1/notifications`` — recovery endpoint for missed events.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from careerops.api.auth_dependency import require_candidate_id

router = APIRouter(tags=["notifications"])
_log = logging.getLogger("careerops.api.notifications")

_HEARTBEAT_INTERVAL = 15  # seconds


@router.get("/api/v1/notifications/stream")
async def stream_notifications(
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> StreamingResponse:
    """SSE endpoint for real-time notifications.

    Uses cookie-based auth (``require_web_auth`` via ``require_candidate_id``).
    EventSource in the browser sends cookies automatically; no CSRF header
    is needed for this GET-only endpoint.
    """
    notification_service = getattr(request.app.state, "notification_service", None)
    if notification_service is None:
        return StreamingResponse(
            iter([]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    user_id = str(candidate_id)

    async def event_generator():
        sse_channel = notification_service._channel
        async for chunk in sse_channel.subscribe(user_id):
            yield chunk

    async def stream_with_heartbeat():
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        sse_channel = notification_service._channel

        # Register subscriber
        subscribers = sse_channel._subscribers.setdefault(user_id, [])
        subscribers.append(queue)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_INTERVAL
                    )
                    if item is None:
                        break
                    yield item
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in subscribers:
                subscribers.remove(queue)
            if not subscribers:
                sse_channel._subscribers.pop(user_id, None)

    return StreamingResponse(
        stream_with_heartbeat(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/v1/notifications")
async def list_pending_notifications(
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> dict[str, object]:
    """Recovery endpoint: return undelivered notifications.

    Clients call this after reconnecting to catch up on missed events.
    """
    notification_service = getattr(request.app.state, "notification_service", None)
    if notification_service is None:
        return {"items": [], "count": 0}

    user_id = str(candidate_id)
    pending = notification_service.get_pending(user_id)
    items = [
        {
            "id": str(e.id),
            "type": e.event_type,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
            "delivered": e.delivered,
        }
        for e in pending
    ]
    return {"items": items, "count": len(items)}
