"""Multipart descriptors close even when a console handler rejects or raises."""

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from hub.admin import refuse_cross_origin


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_guard_closes_uploaded_files_after_handler(fail):
    uploads = []

    async def handler(request):
        form = await request.form()
        uploads.append(form["field"])
        if fail:
            raise ValueError("injected handler failure")
        return Response(status_code=400)

    app = Starlette(routes=[Route("/", refuse_cross_origin(handler), methods=["POST"])])
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post("/", files={"field": ("field.txt", b"untrusted")})
        assert response.status_code == (500 if fail else 400)
        assert uploads and uploads[0].file.closed
    finally:
        for upload in uploads:
            await upload.close()
