import importlib.util
from pathlib import Path
import httpx
from starlette.testclient import TestClient

spec = importlib.util.spec_from_file_location('devflow_frontend_server', Path(__file__).parents[1]/'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def test_static_scope_and_security_headers():
    with TestClient(server.app) as client:
        root = client.get('/')
        assert root.status_code == 200 and 'DevFlow' in root.text
        assert root.headers['X-Frame-Options'] == 'DENY'
        assert "connect-src 'self'" in root.headers['Content-Security-Policy']
        assert client.get('/src/app.js').status_code == 200
        for path in ('/.env','/server.py','/requirements.lock','/internal/attempts/test','/src/../server.py'):
            assert client.get(path).status_code == 404
        async def unavailable(request): raise httpx.ConnectError('offline')
        original = server.app.state.client
        server.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
        assert client.get('/api/v1/projects').status_code == 502
        server.app.state.client = original


def test_proxy_forwards_auth_idempotency_and_stream_without_credentials_in_urls():
    calls=[]
    async def upstream(request):
        calls.append(request)
        if request.url.path.endswith('/events'):
            return httpx.Response(200,headers={'Content-Type':'text/event-stream'},content='id: 3\ndata: {"sequence":3}\n\n')
        return httpx.Response(409,json={'detail':'conflict'})
    with TestClient(server.app) as client:
        real = server.app.state.client
        server.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        result = client.post('/api/v1/tasks',headers={'Authorization':'Bearer ui-test-token','Idempotency-Key':'same'},json={'goal':'fix'})
        assert result.status_code == 409 and result.json()['detail'] == 'conflict'
        assert calls[0].headers['Authorization'] == 'Bearer ui-test-token'
        assert calls[0].headers['Idempotency-Key'] == 'same'
        assert 'ui-test-token' not in str(calls[0].url)
        stream = client.get('/api/v1/events?project_id=p&stream=true&after=2')
        assert stream.status_code == 200 and '"sequence":3' in stream.text
        assert calls[-1].url.params['after'] == '2'
        server.app.state.client = real
