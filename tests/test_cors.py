"""Hosted operator truth editing must pass the browser's CORS preflight."""
import httpx


async def test_truth_editor_put_is_allowed_from_configured_origin():
    from app.main import app
    from app.config import settings

    origin = settings.allowed_origins[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.options('/api/operator/reports/example/batches/b0/truth', headers={
            'Origin': origin, 'Access-Control-Request-Method': 'PUT',
            'Access-Control-Request-Headers': 'Authorization,Content-Type',
        })
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == origin
