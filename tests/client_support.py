"""A TestClient that talks to the API the way a real local client does.

It uses a loopback Host and, once the app has started, sends the Bearer token
the lifespan loaded (WP-05A).
"""

from fastapi.testclient import TestClient

LOCAL_BASE_URL = "http://127.0.0.1"


class LocalClient(TestClient):
    def __init__(self, app, **kwargs):
        kwargs.setdefault("base_url", LOCAL_BASE_URL)
        super().__init__(app, **kwargs)
        self._asgi_app = app

    def __enter__(self):
        client = super().__enter__()
        token = getattr(self._asgi_app.state, "auth_token", None)
        if isinstance(token, str):
            client.headers["Authorization"] = f"Bearer {token}"
        return client
