from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .config import CHECKLIST_DIR
from .models import Criterion


@lru_cache(maxsize=1)
def load_checklists() -> dict[str, dict]:
    checklists: dict[str, dict] = {}
    for path in sorted(CHECKLIST_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["criteria"] = [Criterion.model_validate(item) for item in data.get("criteria", [])]
        checklists[data["category"]] = data
    return checklists


def get_checklist(category: str | None) -> dict | None:
    if not category:
        return None
    normalized = category.strip().lower().replace("-", "_").replace(" ", "_")
    checklists = load_checklists()
    if normalized in checklists:
        return checklists[normalized]
    for checklist in checklists.values():
        aliases = [value.lower() for value in checklist.get("aliases", [])]
        if category.lower() in aliases:
            return checklist
    return None


def supported_categories() -> list[dict[str, str]]:
    return [
        {"id": key, "name": data["display_name"]}
        for key, data in load_checklists().items()
    ]

