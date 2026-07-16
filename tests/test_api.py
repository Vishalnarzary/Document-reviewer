import json

from fastapi.testclient import TestClient

from app.main import app
from app.models import ApplicationData, ReviewState


client = TestClient(app)


def test_home_and_health():
    response = client.get("/")
    assert response.status_code == 200
    assert "Evidence Desk" in response.text
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_non_pdf_upload_is_rejected():
    response = client.post("/api/reviews", files={"file": ("notes.txt", b"not a pdf", "text/plain")})
    assert response.status_code == 400


def test_pdf_upload_streams_real_progress_events(monkeypatch):
    async def fake_create(upload_path, filename, progress=None):
        progress(5, "Saving the uploaded document")
        progress(55, "Analyzing all website text with Groq")
        progress(100, "Review complete")
        return ReviewState(
            id="progress-test-review",
            application_filename=filename,
            application_sha256="test-sha256",
            application=ApplicationData(requested_item="Test class"),
        )

    monkeypatch.setattr("app.main.workflow.create", fake_create)
    response = client.post(
        "/api/reviews",
        files={"file": ("sample.pdf", b"%PDF-1.4 test", "application/pdf")},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["progress"] for event in events if event["type"] == "progress"] == [5, 55, 100]
    assert events[-1]["type"] == "complete"
    assert events[-1]["review_id"] == "progress-test-review"
