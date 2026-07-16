from __future__ import annotations

import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from .checklists import get_checklist
from .evidence import capture_evidence, enforce_evidence_gate, materialize_recovered_text_evidence
from .extraction import extract_application
from .groq_client import GroqAdapter
from .models import AuditEvent, ChatMessage, CrawledPage, Criterion, Finding, FindingStatus, ReviewPhase, ReviewState
from .reporting import generate_report_package
from .research import crawl_site, heuristic_evaluate
from .storage import store
from .utils import safe_public_url, sha256_file
from .vision import (
    capture_vision_candidates,
    materialize_vision_evidence,
    select_vision_fallback_criteria,
)


ProgressCallback = Callable[[int, str], None]


def _update_progress(callback: ProgressCallback | None, value: int, stage: str) -> None:
    if callback:
        callback(max(0, min(100, value)), stage)


def _is_price_criterion(criterion: Criterion) -> bool:
    text = f"{criterion.id} {criterion.label} {criterion.rule or ''}".lower()
    return any(term in text for term in ("price", "fee", "cost", "cap"))


def _quote_is_scaled_or_contextual_funding(quote: str) -> bool:
    """Safety net for model output; semantic Groq analysis remains authoritative."""
    text = re.sub(r"\s+", " ", quote).strip().lower()
    amounts = list(
        re.finditer(
            r"\$\s*[0-9][0-9,]*(?:\.\d+)?\s*(million|billion|thousand|[kmb](?=\b))?",
            text,
            re.IGNORECASE,
        )
    )
    if not amounts:
        return False
    only_scaled_amounts = all(match.group(1) for match in amounts)
    funding_terms = (
        "scholarship",
        "grant",
        "fundraising",
        "fund raised",
        "donation",
        "endowment",
        "award students",
        "budget",
    )
    direct_fee_terms = ("fee", "tuition", "price", "cost", "registration", "per class", "per session")
    contextual_funding_only = any(term in text for term in funding_terms) and not any(
        term in text for term in direct_fee_terms
    )
    return only_scaled_amounts or contextual_funding_only


def _money_values(value: str) -> list[float]:
    scales = {
        "thousand": 1_000,
        "k": 1_000,
        "million": 1_000_000,
        "m": 1_000_000,
        "billion": 1_000_000_000,
        "b": 1_000_000_000,
    }
    values: list[float] = []
    for match in re.finditer(
        r"\$\s*([0-9][0-9,]*(?:\.\d+)?)\s*(thousand|million|billion|[kmb](?=\b))?",
        value,
        re.IGNORECASE,
    ):
        amount = float(match.group(1).replace(",", ""))
        values.append(amount * scales.get((match.group(2) or "").lower(), 1))
    return values


def _derive_exact_price_matches(
    application,
    criteria_by_id: dict[str, Criterion],
    findings_by_id: dict[str, Finding],
) -> None:
    """Reuse direct Groq-validated price evidence for deterministic equality checks."""
    requested = application.requested_price
    if requested is None:
        return
    sources = [
        finding
        for finding in findings_by_id.values()
        if finding.status == FindingStatus.FOUND
        and finding.url
        and finding.quote
        and not _quote_is_scaled_or_contextual_funding(finding.quote)
        and any(abs(value - requested) < 0.01 for value in _money_values(finding.quote))
    ]
    if not sources:
        return
    source = max(sources, key=lambda finding: finding.confidence or 0)
    for criterion in criteria_by_id.values():
        if criterion.rule != "price_match":
            continue
        current = findings_by_id.get(criterion.id)
        if current is not None and current.status == FindingStatus.FOUND:
            continue
        findings_by_id[criterion.id] = Finding(
            criterion_id=criterion.id,
            label=criterion.label,
            status=FindingStatus.FOUND,
            note=(
                f"The Groq-validated public fee of ${requested:,.2f} matches the amount stated "
                "on the application."
            ),
            url=source.url,
            quote=source.quote,
            source="groq-derived",
            confidence=min(0.98, source.confidence or 0.9),
        )


def _derive_explicit_requested_price(
    application,
    criteria_by_id: dict[str, Criterion],
    pages: list[CrawledPage],
    findings_by_id: dict[str, Finding],
) -> None:
    """Prefer an exact price that is contextually tied to the requested offering.

    A provider can publish several similarly named products on neighboring pages.
    Groq's first conclusive price is therefore not enough: a candidate must also
    contain a distinctive requested-offering term in the surrounding product block
    (or in the page identity). This lets ``Individual ... $80`` outrank an
    unrelated ``University Membership ... $7,500`` result.
    """
    requested = application.requested_price
    if requested is None:
        return
    stopwords = {
        "activity",
        "annual",
        "class",
        "classes",
        "course",
        "fee",
        "fees",
        "lesson",
        "lessons",
        "membership",
        "minute",
        "minutes",
        "program",
        "requested",
        "service",
        "services",
        "session",
        "sessions",
        "the",
    }
    offering_tokens = {
        token
        for token in re.findall(
            r"[a-z0-9]+",
            f"{application.requested_item or ''} {application.subject_area or ''}".lower(),
        )
        if len(token) >= 4 and token not in stopwords and not token.isdigit()
    }
    if not offering_tokens:
        return

    # Product cards and comparison tables often put the price many short lines
    # after the offering label. Keep the window bounded so a label does not leak
    # into the next product on a long page.
    matched: tuple[int, CrawledPage, str] | None = None
    for page in pages:
        lines = [re.sub(r"\s+", " ", line).strip() for line in (page.markdown or page.text).splitlines()]
        lines = [line for line in lines if line]
        for index, line in enumerate(lines):
            values = _money_values(line)
            if not any(abs(value - requested) < 0.01 for value in values):
                continue
            immediate_context = " ".join(lines[max(0, index - 2) : index + 3]).lower()
            if re.search(r"\b(save|saving|discount|discounted|off)\b", immediate_context):
                continue
            if _quote_is_scaled_or_contextual_funding(immediate_context):
                continue

            score = 0
            matched_tokens: set[str] = set()
            for context_index in range(max(0, index - 16), min(len(lines), index + 3)):
                context_tokens = set(re.findall(r"[a-z0-9]+", lines[context_index].lower()))
                overlap = offering_tokens.intersection(context_tokens)
                if not overlap:
                    continue
                matched_tokens.update(overlap)
                distance = abs(index - context_index)
                score += len(overlap) * max(1, 20 - distance)

            page_identity_tokens = set(
                re.findall(r"[a-z0-9]+", f"{page.title} {page.url}".lower())
            )
            identity_overlap = offering_tokens.intersection(page_identity_tokens)
            matched_tokens.update(identity_overlap)
            score += len(identity_overlap) * 3

            # At least one non-generic requested-offering term must identify the
            # candidate. Merely sharing words such as "membership" is unsafe.
            if not matched_tokens or score <= 0:
                continue
            candidate = (score, page, line)
            if matched is None or candidate[0] > matched[0]:
                matched = candidate
    if not matched:
        return
    _, page, quote = matched
    for criterion in criteria_by_id.values():
        if criterion.id not in {"published_fees", "published_fee", "visible_price"} and criterion.rule != "price_match":
            continue
        current = findings_by_id.get(criterion.id)
        # Keep an existing correct result, but replace a conclusive amount from a
        # different offering with this stronger item-and-price association.
        if (
            current is not None
            and current.status == FindingStatus.FOUND
            and any(abs(value - requested) < 0.01 for value in _money_values(current.quote or ""))
        ):
            continue
        note = (
            f"The recovered public page explicitly lists the requested offering at ${requested:,.2f}."
            if criterion.rule != "price_match"
            else f"The explicit public fee of ${requested:,.2f} matches the amount stated on the application."
        )
        findings_by_id[criterion.id] = Finding(
            criterion_id=criterion.id,
            label=criterion.label,
            status=FindingStatus.FOUND,
            note=note,
            url=page.url,
            quote=quote,
            source="groq-verified",
            confidence=0.99,
        )


def _derive_explicit_public_access(
    criteria_by_id: dict[str, Criterion],
    pages: list[CrawledPage],
    findings_by_id: dict[str, Finding],
) -> None:
    """Preserve an explicit public-access statement if the model omits it."""
    criterion = criteria_by_id.get("open_to_public")
    current = findings_by_id.get("open_to_public")
    if not criterion or (current is not None and current.status == FindingStatus.FOUND):
        return
    offering_terms = ("class", "classes", "lesson", "lessons", "membership", "program", "riding")
    for page in pages:
        for raw in re.split(r"\n+|(?<=[.!?])\s+", page.markdown or page.text):
            quote = re.sub(r"\s+", " ", raw).strip()
            lowered = quote.lower()
            explicit_public = any(
                phrase in lowered
                for phrase in ("to the public", "open to the public", "available to the public")
            )
            if explicit_public and any(term in lowered for term in offering_terms):
                findings_by_id[criterion.id] = Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.FOUND,
                    note="The recovered public page explicitly states that the offering is available to the public.",
                    url=page.url,
                    quote=quote,
                    source="groq-verified",
                    confidence=0.99,
                )
                return


def _visual_negative_claim_is_explicit(criterion_id: str, quote: str) -> bool:
    # Negative claims are too easy for a vision model to infer from absence.
    # If explicit text exists, the text pipeline can verify it verbatim instead.
    return criterion_id not in {"identical_fees", "noncredit", "nonclinical", "not_private_club"}


def _negative_claim_has_explicit_text(criterion_id: str, quote: str) -> bool:
    text = re.sub(r"\s+", " ", quote).strip().lower()
    required_phrases = {
        "identical_fees": ("same fee", "same price", "identical fee", "no separate"),
        "noncredit": ("noncredit", "non-credit", "does not award credit", "no college credit"),
        "nonclinical": ("not clinical", "nonclinical", "non-clinical", "not therapy", "not therapeutic"),
        "not_private_club": (
            "open to the public",
            "available to all members",
            "all fitness levels",
            "everyone feels accepted",
            "first-time gym user",
        ),
    }
    phrases = required_phrases.get(criterion_id)
    return True if phrases is None else any(phrase in text for phrase in phrases)


class ReviewWorkflow:
    def __init__(self) -> None:
        self.groq = GroqAdapter()

    async def create(
        self,
        upload_path: Path,
        original_filename: str,
        progress: ProgressCallback | None = None,
    ) -> ReviewState:
        _update_progress(progress, 5, "Saving the uploaded document")
        review_id = uuid.uuid4().hex
        review_dir = store.review_dir(review_id)
        review_dir.mkdir(parents=True, exist_ok=True)
        application_path = review_dir / "application.pdf"
        shutil.copy2(upload_path, application_path)
        _update_progress(progress, 12, "Reading and extracting the application")
        application, _ = await extract_application(application_path, self.groq)
        _update_progress(progress, 24, "Application extracted; preparing the checklist")
        state = ReviewState(
            id=review_id,
            application_filename=original_filename,
            application_sha256=sha256_file(application_path),
            application=application,
            messages=[
                ChatMessage(role="user", content=f"Review this application: {original_filename}"),
                ChatMessage(role="assistant", content="I extracted the application and prepared the relevant review checklist."),
            ],
            audit_log=[AuditEvent(actor="system", action="application_uploaded", details={"filename": original_filename})],
        )
        store.save(state)
        if not application.website_url or not application.category or not application.requested_item:
            state.phase = ReviewPhase.CLARIFICATION
            state.messages.append(
                ChatMessage(
                    role="assistant",
                    content="I need clarification before researching the website: " + " ".join(application.extraction_warnings),
                )
            )
            self._initialize_findings(state)
            _update_progress(progress, 90, "Preparing the clarification report")
            generate_report_package(state, review_dir)
            store.save(state)
            _update_progress(progress, 100, "Review ready")
            return state
        return await self.research(state, progress)

    def _initialize_findings(self, state: ReviewState) -> list:
        checklist = get_checklist(state.application.category)
        if not checklist:
            return []
        state.findings = heuristic_evaluate(state.application, checklist["criteria"], [])
        return checklist["criteria"]

    async def research(
        self,
        state: ReviewState,
        progress: ProgressCallback | None = None,
    ) -> ReviewState:
        _update_progress(progress, 28, "Validating the provider website")
        review_dir = store.review_dir(state.id)
        checklist = get_checklist(state.application.category)
        if not checklist:
            state.phase = ReviewPhase.CLARIFICATION
            state.error = "The application category is missing or unsupported."
            store.save(state)
            return state
        valid, reason = safe_public_url(state.application.website_url or "")
        if not valid:
            state.phase = ReviewPhase.CLARIFICATION
            state.error = reason
            self._initialize_findings(state)
            _update_progress(progress, 90, "Preparing the clarification report")
            generate_report_package(state, review_dir)
            store.save(state)
            _update_progress(progress, 100, "Review ready")
            return state
        state.phase = ReviewPhase.RESEARCHING
        store.save(state)
        evidence_dir = review_dir / "evidence"
        if evidence_dir.exists():
            shutil.rmtree(evidence_dir)
        criteria = checklist["criteria"]
        _update_progress(progress, 34, "Crawling relevant public website pages")
        pages, crawl_warnings = await crawl_site(
            state.application.website_url or "",
            state.application,
            criteria,
            discover_pages=self.groq.discover_official_pages if self.groq.enabled else None,
        )
        _update_progress(progress, 52, f"Collected {len(pages)} website page(s)")
        state.crawled_pages = pages
        baseline = heuristic_evaluate(state.application, criteria, pages)
        baseline_by_id = {finding.criterion_id: finding for finding in baseline}
        public_criteria = [criterion for criterion in criteria if criterion.scope == "public_web"]
        def groq_progress(completed: int, total: int) -> None:
            fraction = completed / max(1, total)
            value = 55 + round(fraction * 20)
            _update_progress(progress, value, f"Analyzing relevant snippets with Groq ({completed}/{total} chunks)")

        _update_progress(progress, 55, "Selecting and analyzing relevant website snippets with Groq")
        ai_findings = await self.groq.evaluate(state.application, public_criteria, pages, groq_progress)
        _update_progress(progress, 78, "Validating Groq conclusions and quotations")
        ai_by_id: dict[str, Finding] = {}
        analysis_warning = None
        if ai_findings is not None:
            criteria_by_id = {criterion.id: criterion for criterion in public_criteria}
            page_text_by_url = {
                page.url: re.sub(r"\s+", " ", (page.text or page.markdown)).strip().lower()
                for page in pages
            }
            for finding in ai_findings:
                criterion = criteria_by_id.get(finding.criterion_id)
                if not criterion:
                    continue
                # Reject model citations that are not present in the fully scanned page text.
                page_text = page_text_by_url.get(finding.url or "", "")
                quote = re.sub(r"\s+", " ", finding.quote or "").strip().lower()
                if finding.status in {FindingStatus.FOUND, FindingStatus.NOT_FOUND} and (
                    not quote or quote not in page_text
                ):
                    finding.status = FindingStatus.NEEDS_REVIEW
                    finding.note = (
                        "The model proposed a conclusive result, but its cited language could not be verified "
                        "verbatim in the crawled page."
                    )
                    finding.quote = None
                    finding.url = None
                elif (
                    finding.status == FindingStatus.FOUND
                    and _is_price_criterion(criterion)
                    and _quote_is_scaled_or_contextual_funding(finding.quote or "")
                ):
                    finding.status = FindingStatus.NEEDS_REVIEW
                    finding.note = (
                        "Groq found a monetary phrase, but it describes a scaled or contextual funding amount "
                        "rather than a fee explicitly charged for the requested offering."
                    )
                    finding.evidence_ids = []
                elif (
                    finding.status == FindingStatus.FOUND
                    and not _negative_claim_has_explicit_text(
                        criterion.id,
                        finding.quote or "",
                    )
                ):
                    finding.status = FindingStatus.NEEDS_REVIEW
                    finding.note = (
                        "The cited public text does not explicitly prove this negative criterion, so it requires "
                        "manual review."
                    )
                    finding.url = None
                    finding.quote = None
                    finding.evidence_ids = []
                if not finding.source.startswith("groq"):
                    finding.source = "groq"
                if not finding.note.strip():
                    finding.note = (
                        "Groq validated the cited public text for this criterion."
                        if finding.status in {FindingStatus.FOUND, FindingStatus.NOT_FOUND}
                        else "The cited public text does not fully resolve this criterion, so manual review is required."
                    )
                ai_by_id[finding.criterion_id] = finding
        else:
            diagnostic = f" ({self.groq.last_error})" if self.groq.last_error else ""
            analysis_warning = (
                "Groq could not complete analysis of every retrieved website snippet, so no regex-based public-web "
                f"conclusions were used. Public criteria were marked for review.{diagnostic}"
            )

        if ai_findings is not None:
            public_criteria_by_id = {criterion.id: criterion for criterion in public_criteria}
            _derive_explicit_requested_price(
                state.application,
                public_criteria_by_id,
                pages,
                ai_by_id,
            )
            _derive_exact_price_matches(
                state.application,
                public_criteria_by_id,
                ai_by_id,
            )
            _derive_explicit_public_access(
                public_criteria_by_id,
                pages,
                ai_by_id,
            )

        vision_captures = []
        vision_warnings: list[str] = []
        vision_findings: list[Finding] | None = None
        vision_criteria, vision_reasons = select_vision_fallback_criteria(
            public_criteria,
            ai_by_id,
            pages,
            crawl_warnings,
        )
        if vision_criteria and self.groq.enabled:
            _update_progress(progress, 79, "Checking blocked or image-based evidence with Groq vision")
            vision_captures, preparation_warnings = await capture_vision_candidates(
                state.application.website_url or "",
                pages,
                review_dir,
                state.application,
            )
            vision_warnings.extend(preparation_warnings)
            if vision_captures:
                vision_findings = await self.groq.evaluate_images(
                    state.application,
                    vision_criteria,
                    vision_captures,
                )
                if vision_findings is None:
                    diagnostic = (
                        f" ({self.groq.last_vision_error})"
                        if self.groq.last_vision_error
                        else ""
                    )
                    vision_warnings.append(
                        "Groq vision could not complete analysis of the captured webpage images."
                        + diagnostic
                    )
                else:
                    # The visual pass completed, so an unavailable text pass is no
                    # longer itself a limitation. Individual unsupported criteria
                    # still remain Needs Review below.
                    if ai_findings is None:
                        analysis_warning = None
                    criteria_by_id = {criterion.id: criterion for criterion in vision_criteria}
                    for finding in vision_findings:
                        criterion = criteria_by_id.get(finding.criterion_id)
                        if not criterion:
                            continue
                        if (
                            finding.status == FindingStatus.FOUND
                            and not _visual_negative_claim_is_explicit(
                                criterion.id,
                                finding.quote or "",
                            )
                        ):
                            finding.status = FindingStatus.NEEDS_REVIEW
                            finding.note = (
                                "The visual model inferred a negative claim, but the screenshot did not contain "
                                "an explicit statement proving it."
                            )
                            finding.url = None
                            finding.quote = None
                            finding.visual_capture_id = None
                        if (
                            finding.status == FindingStatus.FOUND
                            and _is_price_criterion(criterion)
                            and _quote_is_scaled_or_contextual_funding(finding.quote or "")
                        ):
                            finding.status = FindingStatus.NEEDS_REVIEW
                            finding.note = (
                                "The visual model found a monetary phrase, but it describes scaled or contextual "
                                "funding rather than a fee explicitly charged for the requested offering."
                            )
                            finding.url = None
                            finding.quote = None
                            finding.visual_capture_id = None
                        current = ai_by_id.get(finding.criterion_id)
                        if finding.status == FindingStatus.FOUND and (
                            current is None or current.status != FindingStatus.FOUND
                        ):
                            ai_by_id[finding.criterion_id] = finding
                        elif current is None:
                            ai_by_id[finding.criterion_id] = finding
            else:
                vision_warnings.append(
                    "Visual fallback was requested, but no webpage image could be captured for analysis."
                )

        state.findings = []
        for criterion in criteria:
            if criterion.scope != "public_web":
                state.findings.append(baseline_by_id[criterion.id])
                continue
            finding = ai_by_id.get(criterion.id)
            if finding is None:
                reason = (
                    "Groq did not return a complete, validated conclusion for this criterion after scanning "
                    "the crawled website text. Manual review is required."
                    if self.groq.enabled
                    else "Groq is not configured, so the public website text was not analyzed by the LLM."
                )
                finding = Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.NEEDS_REVIEW,
                    note=reason,
                    source="groq",
                )
            state.findings.append(finding)
        _update_progress(progress, 83, "Capturing timestamped website evidence")
        state.evidence, capture_warnings = await capture_evidence(
            state.id,
            review_dir,
            state.findings,
            fallback_url=state.application.website_url,
        )
        state.evidence.extend(
            materialize_vision_evidence(
                state.id,
                review_dir,
                state.findings,
                vision_captures,
            )
        )
        state.evidence.extend(
            materialize_recovered_text_evidence(
                state.id,
                review_dir,
                state.findings,
                pages,
            )
        )
        enforce_evidence_gate(state.findings, state.evidence)
        visible_crawl_warnings = [
            warning for warning in crawl_warnings if not warning.startswith("Recovered:")
        ]
        warnings = (
            visible_crawl_warnings
            + vision_warnings
            + capture_warnings
            + ([analysis_warning] if analysis_warning else [])
        )
        if warnings:
            state.messages.append(
                ChatMessage(
                    role="assistant",
                    content="I completed the review with limitations: " + " ".join(warnings[:4]),
                )
            )
        else:
            found = sum(1 for finding in state.findings if finding.status == FindingStatus.FOUND)
            needs = sum(1 for finding in state.findings if finding.status == FindingStatus.NEEDS_REVIEW)
            state.messages.append(
                ChatMessage(
                    role="assistant",
                    content=f"Website research is complete. I captured evidence for {found} finding(s); {needs} item(s) still need review.",
                )
            )
        state.phase = ReviewPhase.COMPLETE
        state.audit_log.append(
            AuditEvent(
                actor="system",
                action="website_review_completed",
                details={
                    "pages": len(pages),
                    "evidence": len(state.evidence),
                    "groq_relevant_snippet_analysis": ai_findings is not None,
                    "groq_model": self.groq.model if self.groq.enabled else None,
                    "groq_error": self.groq.last_error,
                    "groq_vision_model": self.groq.vision_model if self.groq.enabled else None,
                    "groq_vision_reasons": vision_reasons,
                    "groq_vision_images": len(vision_captures),
                    "groq_vision_analysis": vision_findings is not None,
                    "groq_vision_error": self.groq.last_vision_error,
                    "groq_discovery_model": self.groq.discovery_model if self.groq.enabled else None,
                    "groq_discovery_error": self.groq.last_discovery_error,
                    "crawl_notes": crawl_warnings,
                },
            )
        )
        _update_progress(progress, 94, "Generating the HTML, PDF, and evidence package")
        generate_report_package(state, review_dir)
        store.save(state)
        _update_progress(progress, 100, "Review complete")
        return state

    async def handle_message(self, state: ReviewState, message: str) -> ReviewState:
        clean = message.strip()
        state.messages.append(ChatMessage(role="user", content=clean))
        url_match = re.search(r"https?://[^\s]+", clean)
        if url_match and any(term in clean.lower() for term in ("use", "try", "check", "rerun", "re-run")):
            url = url_match.group(0).rstrip(".,)")
            valid, reason = safe_public_url(url)
            if not valid:
                reply = reason
            else:
                state.application.website_url = url
                state.evidence = []
                state.audit_log.append(AuditEvent(actor="reviewer", action="website_url_changed", details={"url": url}))
                state.messages.append(ChatMessage(role="assistant", content=f"I’ll re-run the review using {url}."))
                store.save(state)
                return await self.research(state)
        elif re.search(r"\b(re-?run|run again)\b", clean, re.I):
            state.evidence = []
            state.audit_log.append(AuditEvent(actor="reviewer", action="review_rerun_requested"))
            state.messages.append(ChatMessage(role="assistant", content="I’ll re-run the website research and refresh the evidence package."))
            store.save(state)
            return await self.research(state)
        elif clean.lower().startswith("add note"):
            note = re.sub(r"^add note\s*:?\s*", "", clean, flags=re.I).strip()
            if note:
                state.reviewer_notes.append(note)
                state.audit_log.append(AuditEvent(actor="reviewer", action="note_added", details={"note": note}))
                reply = "I added the reviewer note and regenerated the report package."
            else:
                reply = "Please include the note you want added."
        elif "regenerate" in clean.lower() and "report" in clean.lower():
            reply = "I regenerated the HTML, PDF, manifest, and downloadable evidence package."
            state.audit_log.append(AuditEvent(actor="reviewer", action="report_regenerated"))
        else:
            status_match = re.search(r"(?:change|mark|set)\s+(.+?)\s+(?:to|as)\s+(found|not found|needs review|internal)\b", clean, re.I)
            if status_match:
                target, requested = status_match.group(1).strip().lower(), status_match.group(2).lower()
                finding = next((item for item in state.findings if target in item.label.lower() or target in item.criterion_id.lower()), None)
                if not finding:
                    reply = "I couldn’t identify that checklist item. Please use a few words from its label."
                else:
                    status = {"found": FindingStatus.FOUND, "not found": FindingStatus.NOT_FOUND, "needs review": FindingStatus.NEEDS_REVIEW, "internal": FindingStatus.INTERNAL}[requested]
                    has_targeted = any(record.criterion_id == finding.criterion_id and record.kind == "targeted" for record in state.evidence)
                    if status == FindingStatus.FOUND and not has_targeted:
                        reply = "I can’t mark that item Found without a targeted evidence capture. It remains Needs Review."
                        finding.status = FindingStatus.NEEDS_REVIEW
                    else:
                        finding.status = status
                        finding.source = "reviewer"
                        finding.note += " Status manually adjusted by the reviewer."
                        state.audit_log.append(AuditEvent(actor="reviewer", action="finding_status_changed", details={"criterion_id": finding.criterion_id, "status": status.value}))
                        reply = f"I changed '{finding.label}' to {status.value} and recorded the adjustment."
            else:
                reply = await self.groq.chat(state, clean) or self._local_reply(state, clean)
        state.messages.append(ChatMessage(role="assistant", content=reply))
        generate_report_package(state, store.review_dir(state.id))
        store.save(state)
        return state

    def _local_reply(self, state: ReviewState, message: str) -> str:
        lowered = message.lower()
        if "price" in lowered or "rate" in lowered or "fee" in lowered:
            match = next((finding for finding in state.findings if finding.criterion_id == "fee_match"), None)
            return match.note if match else "This checklist has no direct fee-match finding. Review the published-fee and cap rows instead."
        if "summary" in lowered or "result" in lowered:
            counts = {status: sum(1 for item in state.findings if item.status == status) for status in FindingStatus}
            return "Review summary: " + ", ".join(f"{count} {status.value}" for status, count in counts.items()) + ". Final approval or denial remains with staff."
        return "I can summarize the findings, explain the rate comparison, add a reviewer note, change an item to Needs Review, use a different public URL, or regenerate the report."


workflow = ReviewWorkflow()
