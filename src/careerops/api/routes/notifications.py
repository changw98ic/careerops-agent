"""Notification API routes (Phase 9.1-9.2).

Provides per-candidate notification endpoints under
``/api/v1/candidates/{candidate_id}/notifications``:
- ``GET .../notifications/stream`` — SSE endpoint.
- ``GET .../notifications`` — recovery endpoint for missed events.

Both are public: the session/CSRF auth layer was removed (auth-rm Task 9).
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(
    prefix="/api/v1/candidates/{candidate_id}/notifications",
    tags=["notifications"],
)
_log = logging.getLogger("careerops.api.notifications")

_HEARTBEAT_INTERVAL = 15  # seconds


@router.get("/stream")
async def stream_notifications(
    request: Request,
    candidate_id: UUID,
) -> StreamingResponse:
    """SSE endpoint for real-time notifications.

    Public endpoint (the session/CSRF auth layer was removed — auth-rm
    Task 9). ``candidate_id`` comes from the request path.
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


@router.get("")
async def list_pending_notifications(
    request: Request,
    candidate_id: UUID,
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
