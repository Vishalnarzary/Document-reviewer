from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class FindingStatus(StrEnum):
    FOUND = "Found"
    NOT_FOUND = "Not Found"
    NEEDS_REVIEW = "Needs Review"
    INTERNAL = "Internal"


class ReviewPhase(StrEnum):
    EXTRACTED = "extracted"
    CLARIFICATION = "clarification_required"
    RESEARCHING = "researching"
    COMPLETE = "complete"
    FAILED = "failed"


class ApplicationData(BaseModel):
    participant_name: str | None = None
    participant_age: int | None = None
    fi_coordinator: str | None = None
    broker_name: str | None = None
    category: str | None = None
    requested_item: str | None = None
    provider_name: str | None = None
    website_url: str | None = None
    requested_price_text: str | None = None
    requested_price: float | None = None
    billing_period: str | None = None
    duration: str | None = None
    subject_area: str | None = None
    safety_features: str | None = None
    denial_reason: str | None = None
    appeal_justification: str | None = None
    valued_outcome: str | None = None
    life_plan_date: str | None = None
    source_pages: int = 0
    extraction_warnings: list[str] = Field(default_factory=list)


class Criterion(BaseModel):
    id: str
    label: str
    scope: str
    description: str = ""
    evidence_terms: list[str] = Field(default_factory=list)
    absence_status: FindingStatus = FindingStatus.NEEDS_REVIEW
    rule: str | None = None


class ChecklistCriterionInput(BaseModel):
    id: str | None = None
    label: str = Field(min_length=2, max_length=160)
    scope: Literal["public_web", "document", "internal"] = "public_web"
    description: str = Field(default="", max_length=1000)
    evidence_terms: list[str] = Field(default_factory=list, max_length=30)
    absence_status: FindingStatus = FindingStatus.NEEDS_REVIEW
    rule: str | None = Field(default=None, max_length=60)


class ChecklistInput(BaseModel):
    category: str = Field(min_length=2, max_length=60)
    display_name: str = Field(min_length=2, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    criteria: list[ChecklistCriterionInput] = Field(min_length=1, max_length=40)


class EvidenceRecord(BaseModel):
    id: str
    criterion_id: str | None = None
    kind: str
    url: str
    captured_at: str
    raw_path: str
    stamped_path: str
    quote: str | None = None
    sha256: str | None = None


class Finding(BaseModel):
    criterion_id: str
    label: str
    status: FindingStatus
    note: str
    url: str | None = None
    quote: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    source: str = "system"
    confidence: float | None = None
    visual_capture_id: str | None = None


class CrawledPage(BaseModel):
    url: str
    title: str = ""
    markdown: str = ""
    text: str = ""
    score: float = 0


class VisionCapture(BaseModel):
    id: str
    url: str
    title: str = ""
    path: str
    blocked: bool = False


class AuditEvent(BaseModel):
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    actor: str
    action: str
    details: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: str
    content: str
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ReviewState(BaseModel):
    id: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase: ReviewPhase = ReviewPhase.EXTRACTED
    application_filename: str
    application_sha256: str
    application: ApplicationData
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    crawled_pages: list[CrawledPage] = Field(default_factory=list)
    reviewer_notes: list[str] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    audit_log: list[AuditEvent] = Field(default_factory=list)
    report_html: str | None = None
    report_pdf: str | None = None
    package_zip: str | None = None
    error: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
