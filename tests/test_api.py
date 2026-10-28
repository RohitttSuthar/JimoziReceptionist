from fastapi.testclient import TestClient
from app.main import app
from app.config import settings


def test_health_and_document_chat_fallback(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    with TestClient(app, headers={"Authorization": "Basic YWRtaW46dGVzdC1wYXNzd29yZA=="}) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        response = client.post("/api/chat", json={"text": "What is the menu?", "product_id": "jimozi"})
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"]
        assert body["answer"]
        assert body["product_name"]
        assert "answer_mode" not in body
        assert "sources" not in body
        assert "latency_ms" not in body


def test_document_upload_list_delete():
    with TestClient(app, headers={"Authorization": "Basic YWRtaW46dGVzdC1wYXNzd29yZA=="}) as client:
        uploaded = client.post("/api/products/jimozi/documents", data={"audience": "consumer"},
            files={"file": ("test-menu.txt", b"Our lunch menu includes dal, rice and roti. Delivery hours are 10 AM to 8 PM.", "text/plain")})
        assert uploaded.status_code == 200, uploaded.text
        doc = uploaded.json()["document"]
        assert doc["filename"] == "test-menu.txt"
        assert doc["chunks"] >= 1
        listed = client.get("/api/products/jimozi/documents")
        assert listed.status_code == 200
        assert any(x["id"] == doc["id"] for x in listed.json()["documents"])
        answer = client.post("/api/chat", json={"text": "What are the delivery hours?", "product_id": "jimozi", "mode": "consumer"})
        assert answer.status_code == 200
        assert answer.json()["answer"]
        assert "matched" not in answer.json()
        assert "sources" not in answer.json()
        deleted = client.delete(f"/api/products/jimozi/documents/{doc['id']}")
        assert deleted.status_code == 200


def test_mode_filter_and_private_logging(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(settings, "openai_api_key", "")
    with caplog.at_level(logging.INFO, logger="voice_receptionist"):
        with TestClient(app, headers={"Authorization": "Basic YWRtaW46dGVzdC1wYXNzd29yZA=="}) as client:
            response = client.post("/api/chat", json={
                "text": "Does the menu include pasta?", "product_id": "jimozi",
                "mode": "chef", "session_id": "doc-log-test",
            })
    assert response.status_code == 200
    assert "CHAT_RESPONSE" in caplog.text
    assert "Does the menu include pasta?" not in caplog.text
    assert response.json()["answer"] not in caplog.text


def test_admin_routes_require_authentication():
    with TestClient(app) as client:
        response = client.get("/api/products/jimozi/documents")
        assert response.status_code == 401
        page = client.get("/admin")
        assert page.status_code == 401

def test_admin_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "admin_username", "")
    monkeypatch.setattr(settings, "admin_password", "")
    with TestClient(app) as client:
        response = client.get("/api/products/jimozi/documents", headers={"Authorization": "Basic YWRtaW46dGVzdC1wYXNzd29yZA=="})
        assert response.status_code == 503
