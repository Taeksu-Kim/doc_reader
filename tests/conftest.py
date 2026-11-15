"""Shared helpers: the one doc_reader app, seen through an area prefix (/read, /chunk, /library).
API paths in the area tests stay as the screens use them ("/api/..."); A2A and the card stay at the root."""

from types import SimpleNamespace

from fastapi.testclient import TestClient


class Area:
    """A TestClient that prefixes area paths: "/api/x" -> "/read/api/x", "/" -> "/read/"."""

    def __init__(self, client: TestClient, prefix: str, state):
        self.c, self.prefix = client, prefix
        self.app = SimpleNamespace(state=state)

    def _p(self, path: str) -> str:
        if path.startswith(("/a2a", "/.well-known")):
            return path
        return self.prefix + ("/" if path == "/" else path)

    def get(self, path, **kw):
        return self.c.get(self._p(path), **kw)

    def post(self, path, **kw):
        return self.c.post(self._p(path), **kw)

    def delete(self, path, **kw):
        return self.c.delete(self._p(path), **kw)
