from __future__ import annotations

import re
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from careerops.observability.career_loop_trace import bind_trace_id

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        trace_id = supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else uuid4().hex
        request.state.trace_id = trace_id
        with bind_trace_id(trace_id):
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = trace_id
            return response
