from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re

import yaml

from .config import CHECKLIST_DIR
from .models import ChecklistInput, Criterion


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


def checklist_definitions() -> list[dict]:
    return [
        {
            "category": key,
            "display_name": data["display_name"],
            "aliases": list(data.get("aliases", [])),
            "criteria": [criterion.model_dump(mode="json") for criterion in data["criteria"]],
        }
        for key, data in load_checklists().items()
    ]


def _identifier(value: str, fallback: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return (clean[:60] or fallback).strip("_")


def save_checklist(payload: ChecklistInput) -> dict:
    category = _identifier(payload.category, "checklist")
    existing = load_checklists()
    if category in existing:
        raise FileExistsError(f'A checklist named "{category}" already exists.')

    criteria: list[dict] = []
    used_ids: set[str] = set()
    for index, item in enumerate(payload.criteria, 1):
        criterion_id = _identifier(item.id or item.label, f"criterion_{index}")
        if criterion_id in used_ids:
            raise ValueError("Each checklist criterion must have a unique name.")
        used_ids.add(criterion_id)
        criterion = Criterion(
            id=criterion_id,
            label=item.label.strip(),
            scope=item.scope,
            description=item.description.strip(),
            evidence_terms=[term.strip() for term in item.evidence_terms if term.strip()],
            absence_status=item.absence_status,
            rule=item.rule,
        )
        criteria.append(criterion.model_dump(mode="json", exclude_defaults=True))

    aliases = list(
        dict.fromkeys(
            value.strip().lower()
            for value in [payload.display_name, category.replace("_", " "), *payload.aliases]
            if value.strip()
        )
    )
    data = {
        "category": category,
        "display_name": payload.display_name.strip(),
        "aliases": aliases,
        "criteria": criteria,
    }
    target = CHECKLIST_DIR / f"{category}.yaml"
    temporary = target.with_suffix(".yaml.tmp")
    temporary.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(target)
    load_checklists.cache_clear()
    return next(item for item in checklist_definitions() if item["category"] == category)


def remove_checklist(category: str) -> None:
    normalized = _identifier(category, "")
    checklists = load_checklists()
    if normalized not in checklists:
        raise FileNotFoundError("Checklist not found.")
    if len(checklists) <= 1:
        raise ValueError("At least one checklist must remain available.")
    target = CHECKLIST_DIR / f"{normalized}.yaml"
    if not target.exists():
        raise FileNotFoundError("Checklist file not found.")
    target.unlink()
    load_checklists.cache_clear()
