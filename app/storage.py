from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import REVIEWS_DIR
from .models import ReviewState
from .utils import atomic_json_write


class ReviewStore:
    def review_dir(self, review_id: str) -> Path:
        return REVIEWS_DIR / review_id

    def save(self, state: ReviewState) -> None:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        directory = self.review_dir(state.id)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json_write(directory / "state.json", state.model_dump(mode="json"))

    def load(self, review_id: str) -> ReviewState:
        path = self.review_dir(review_id) / "state.json"
        if not path.exists():
            raise FileNotFoundError(review_id)
        return ReviewState.model_validate_json(path.read_text(encoding="utf-8"))

    def recent(self, limit: int = 20) -> list[ReviewState]:
        states: list[ReviewState] = []
        for path in sorted(REVIEWS_DIR.glob("*/state.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                states.append(ReviewState.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if len(states) >= limit:
                break
        return states


store = ReviewStore()

