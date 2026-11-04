from datetime import UTC, datetime
from uuid import UUID

import simplejson
from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute


def encode_extra(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.isoformat()
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


class MoneyJSONResponse(Response):
    media_type = "application/json"

    def render(self, content):
        return simplejson.dumps(
            content, use_decimal=True, allow_nan=False, default=encode_extra, separators=(",", ":")
        ).encode()


class DecimalRequest(Request):
    async def body(self):
        if not hasattr(self, "_body"):
            chunks = []
            size = 0
            maximum = self.app.state.settings.max_body_bytes
            async for chunk in self.stream():
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(413, "Request body exceeds allowed size")
                chunks.append(chunk)
            self._body = b"".join(chunks)
        return self._body

    async def json(self):
        if not hasattr(self, "_json"):
            # Parse JSON decimal tokens directly: no intermediate binary floating point.
            self._json = simplejson.loads(await self.body(), use_decimal=True, allow_nan=False)
        return self._json


class DecimalRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            return await original(DecimalRequest(request.scope, request.receive))

        return handler
