import pytest
from app.config import settings
import app.documents as documents

@pytest.fixture(autouse=True)
def configure_test_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_username", "admin")
    monkeypatch.setattr(settings, "admin_password", "test-password")
    monkeypatch.setattr(settings, "log_transcripts", False)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "supertonic_enabled", False)
    monkeypatch.setattr(documents, "DB_PATH", tmp_path / "knowledge.sqlite3")
    monkeypatch.setattr(documents, "DATA_DIR", tmp_path / "documents")
    documents.DATA_DIR.mkdir(parents=True, exist_ok=True)
    documents.knowledge_base._init_db()
