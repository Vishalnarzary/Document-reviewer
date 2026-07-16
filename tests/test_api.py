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


def test_checklists_can_be_listed_added_and_removed(tmp_path, monkeypatch):
    import app.checklists as checklist_module

    seed = """category: existing\ndisplay_name: Existing\naliases: [existing]\ncriteria:\n  - id: published\n    label: Information is published\n    scope: public_web\n"""
    (tmp_path / "existing.yaml").write_text(seed, encoding="utf-8")
    with monkeypatch.context() as scoped:
        scoped.setattr(checklist_module, "CHECKLIST_DIR", tmp_path)
        checklist_module.load_checklists.cache_clear()

        listed = client.get("/api/checklists")
        assert listed.status_code == 200
        assert [item["category"] for item in listed.json()] == ["existing"]

        created = client.post(
            "/api/checklists",
            json={
                "category": "Art Supplies",
                "display_name": "Art Supplies",
                "aliases": ["creative materials"],
                "criteria": [
                    {
                        "label": "The item and price are published",
                        "scope": "public_web",
                        "evidence_terms": ["price", "materials"],
                        "rule": "price_match",
                    },
                    {"label": "Budget approval is recorded", "scope": "internal"},
                ],
            },
        )
        assert created.status_code == 201
        assert created.json()["category"] == "art_supplies"
        assert (tmp_path / "art_supplies.yaml").exists()

        removed = client.delete("/api/checklists/art_supplies")
        assert removed.status_code == 204
        assert not (tmp_path / "art_supplies.yaml").exists()
    checklist_module.load_checklists.cache_clear()
