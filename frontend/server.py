"""Static workbench + streaming, same-origin API proxy. Holds no platform credentials."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
UPSTREAM = os.environ.get('DEVFLOW_BACKEND_URL', 'http://127.0.0.1:8000').rstrip('/')
SECURITY = {
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'same-origin',
    'Cache-Control': 'no-store',
}

@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10)) as client:
        app.state.client = client
        yield

class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY)
        return response

async def index(request):
    return FileResponse(ROOT / 'index.html')

async def proxy(request):
    size = 0
    body = bytearray()
    async for part in request.stream():
        size += len(part)
        if size > 4 * 1024 * 1024:
            return JSONResponse({'detail': 'request too large'}, status_code=413)
        body.extend(part)
    headers = {key:value for key,value in request.headers.items() if key.lower() in {
        'authorization','content-type','idempotency-key','last-event-id','accept',
    }}
    url = UPSTREAM + request.url.path + ('?' + request.url.query if request.url.query else '')
    try:
        response = await request.app.state.client.send(
            request.app.state.client.build_request(request.method, url, headers=headers, content=bytes(body)), stream=True)
    except httpx.HTTPError:
        return JSONResponse({'detail':'backend unavailable'}, status_code=502)
    return StreamingResponse(response.aiter_bytes(), status_code=response.status_code,
        headers={k:v for k,v in response.headers.items() if k.lower() in {'content-type','cache-control','x-accel-buffering'}},
        background=BackgroundTask(response.aclose))

app = Starlette(routes=[Route('/', index), Route('/index.html', index),
    Route('/api/v1/{path:path}', proxy, methods=['GET','POST','PATCH','HEAD']),
    Mount('/src', app=StaticFiles(directory=ROOT / 'src'))],
    lifespan=lifespan, middleware=[Middleware(SecurityHeaders)])

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('DEVFLOW_FRONTEND_PORT','3000')))
