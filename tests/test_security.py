from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from vesper_cashflow.security import LocalSecurityMiddleware


def make_client():
    app = FastAPI()
    app.add_middleware(LocalSecurityMiddleware, port=8765, testing=True)

    @app.get("/")
    def home():
        return {"ok": True}

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        return {"csrf": request.state.vesper_csrf}

    @app.post("/api/change")
    def change():
        return {"changed": True}

    return TestClient(app)


def test_session_origin_host_and_csrf():
    client = make_client()
    assert client.get("/api/bootstrap").status_code == 403
    home = client.get("/")
    cookie = home.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    csrf = client.get("/api/bootstrap").json()["csrf"]
    assert client.post("/api/change").status_code == 403
    assert (
        client.post("/api/change", headers={"Origin": "http://testserver"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/change",
            headers={"Origin": "https://evil.test", "X-Vesper-CSRF": csrf},
        ).status_code
        == 403
    )
    assert (
        client.get("/api/bootstrap", headers={"Host": "attacker.test"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/change",
            headers={"Origin": "http://testserver", "X-Vesper-CSRF": csrf},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/bootstrap", headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    other = make_client()
    other.cookies.update(client.cookies)
    assert other.get("/api/bootstrap").status_code == 403


def test_production_rejects_dns_aliases():
    app = FastAPI()
    app.add_middleware(LocalSecurityMiddleware, port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 404
        assert client.get("/", headers={"Host": "localhost:8765"}).status_code == 403
        assert client.get("/", headers={"Host": "127.0.0.1:9999"}).status_code == 403
