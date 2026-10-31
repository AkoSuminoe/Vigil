import pytest
from fastapi.testclient import TestClient

import app.main as main

SECRET = "test-secret-key-do-not-leak"


async def fake_analyze(text, source):
    return {"status": "ok", "error_kind": None, "source": source, "lines": len(text.splitlines())}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", SECRET)
    monkeypatch.setattr(main, "analyze", fake_analyze)
    monkeypatch.setattr(main, "limiter", main.RateLimiter(5, 600))
    monkeypatch.setattr(main, "TRUST_PROXY", False)
    return TestClient(main.app)


def upload(client, name, content, **kwargs):
    return client.post("/api/analyze", files={"file": (name, content, "text/plain")}, **kwargs)


def test_index_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Vigil only reads and suggests" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'href="https://berktan.dev"' in response.text
    assert "don't upload real or sensitive logs" in response.text


def test_fonts_are_self_hosted(client):
    html = client.get("/").text
    assert "fonts.googleapis" not in html and "https://cdn" not in html
    for name in ("inter-latin-wght-normal.woff2", "geist-mono-latin-wght-normal.woff2"):
        assert f"/static/fonts/{name}" in html
        response = client.get(f"/static/fonts/{name}")
        assert response.status_code == 200
        assert response.content[:4] == b"wOF2"
        assert "default-src 'self'" in response.headers["content-security-policy"]
    assert client.get("/static/index.html").status_code == 404


def test_samples_lists_the_sample_logs(client):
    names = {s["name"] for s in client.get("/api/samples").json()}
    assert {"payments_incident.log", "auth_service.log", "batch_worker.log"} <= names


def test_analyze_sample_and_upload(client):
    response = client.post("/api/analyze", data={"sample": "payments_incident.log"})
    assert response.status_code == 200
    assert response.json()["source"] == "payments_incident.log"

    response = upload(client, "app.txt", b"2026-10-31 10:00:00 ERROR [api] boom\n")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "error_kind": None, "source": "app.txt", "lines": 1}


def test_rejects_bad_input(client):
    assert upload(client, "app.exe", b"hello").status_code == 400
    assert upload(client, "app.log", b"\x00\x01binary").status_code == 400
    assert upload(client, "app.log", b"   \n").status_code == 400
    assert client.post("/api/analyze", data={"sample": "../.env"}).status_code == 400
    assert client.post("/api/analyze", data={}).status_code == 400


def test_rejects_files_over_2mb(client):
    just_over = b"x" * (2 * 1024 * 1024 + 1)
    assert upload(client, "big.log", just_over).status_code == 413
    way_over = b"x" * (3 * 1024 * 1024)
    response = upload(client, "huge.log", way_over)
    assert response.status_code == 413
    assert "2 MB" in response.json()["error"]


def test_rate_limit_per_ip(client):
    codes = [client.post("/api/analyze", data={"sample": "auth_service.log"}).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    blocked = client.post("/api/analyze", data={"sample": "auth_service.log"})
    assert int(blocked.headers["retry-after"]) > 0


def test_proxy_headers_ignored_unless_trusted(client):
    for i in range(5):
        client.post("/api/analyze", data={"sample": "auth_service.log"}, headers={"CF-Connecting-IP": f"198.51.100.{i}"})
    # Without TRUST_PROXY all of those counted against the same socket address.
    response = client.post("/api/analyze", data={"sample": "auth_service.log"}, headers={"CF-Connecting-IP": "198.51.100.99"})
    assert response.status_code == 429


def test_trusted_proxy_uses_cf_connecting_ip_then_x_forwarded_for(client, monkeypatch):
    monkeypatch.setattr(main, "TRUST_PROXY", True)
    for _ in range(5):
        client.post("/api/analyze", data={"sample": "auth_service.log"}, headers={"CF-Connecting-IP": "198.51.100.1"})
    assert client.post("/api/analyze", data={"sample": "auth_service.log"}, headers={"CF-Connecting-IP": "198.51.100.1"}).status_code == 429
    other = client.post("/api/analyze", data={"sample": "auth_service.log"}, headers={"CF-Connecting-IP": "198.51.100.2"})
    assert other.status_code == 200

    def ip_for(headers):
        return main.client_ip(type("R", (), {"headers": headers, "client": None})())

    assert ip_for({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}) == "203.0.113.7"
    assert ip_for({"cf-connecting-ip": "203.0.113.8", "x-forwarded-for": "203.0.113.7"}) == "203.0.113.8"


def test_rate_limiter_window_slides():
    limiter = main.RateLimiter(2, 10)
    assert limiter.check("a", now=0) is None
    assert limiter.check("a", now=1) is None
    assert limiter.check("a", now=2) == pytest.approx(8)
    assert limiter.check("a", now=10.5) is None  # first hit expired


def test_pipeline_errors_map_to_status_codes(client, monkeypatch):
    async def failing(text, source):
        return {"status": "error", "error_kind": "timeout", "error": "Analysis timed out after 120s."}

    monkeypatch.setattr(main, "analyze", failing)
    response = client.post("/api/analyze", data={"sample": "auth_service.log"})
    assert response.status_code == 504
    assert "timed out" in response.json()["error"]


def test_unrecognised_format_returns_422(client, monkeypatch):
    from vigil.pipeline import analyze as real_analyze

    monkeypatch.setattr(main, "analyze", real_analyze)
    response = upload(client, "access.log", b'203.0.113.9 - - [31/Oct/2026:10:00:00 +0000] "GET / HTTP/1.1" 200 512\n')
    assert response.status_code == 422
    assert "supported log format" in response.json()["error"]


def test_api_key_never_reaches_the_browser(client):
    bodies = [
        client.get("/").text,
        client.get("/api/samples").text,
        client.post("/api/analyze", data={"sample": "payments_incident.log"}).text,
    ]
    assert all(SECRET not in body for body in bodies)
