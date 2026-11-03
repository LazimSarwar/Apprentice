"""Bound the body before multipart parsing can spool unbounded input."""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in ("/api/check", "/api/procedures"): limit = self.max_bytes
        elif path == "/api/watch" or path.endswith("/followup") or (path.startswith('/api/pilots/') and '/cases/' in path and not path.endswith('/timing')): limit = min(self.max_bytes, 88*1024*1024)
        elif path.endswith("/chunk"): limit = min(self.max_bytes, 2*1024*1024)
        else: limit = min(self.max_bytes, 1024*1024)
        length = next((v for k, v in scope.get("headers", []) if k == b"content-length"), b"0")
        try: oversized = int(length) > limit
        except ValueError: oversized = True
        if oversized:
            return await JSONResponse({"detail": "Request exceeds its size limit"}, 413)(scope, receive, send)
        received = 0
        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "Upload request exceeds its limit")
            return message
        await self.app(scope, bounded_receive, send)
