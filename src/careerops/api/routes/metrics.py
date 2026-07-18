from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST

from careerops.observability import Metrics

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    application_metrics = cast("Metrics", request.app.state.metrics)
    return Response(
        content=application_metrics.render(),
        headers={
            "Cache-Control": "no-store",
            "Content-Type": CONTENT_TYPE_LATEST,
        },
    )
