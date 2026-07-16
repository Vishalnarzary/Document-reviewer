from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pdfplumber

from .groq_client import GroqAdapter
from .models import ApplicationData
from .utils import extract_price, normalize_space


def extract_pdf_text(path: Path) -> tuple[str, int, list[str]]:
    warnings: list[str] = []
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            pages.append(page.extract_text(x_tolerance=2, y_tolerance=3) or "")
    text = "\n\n".join(pages).strip()
    if len(text) < max(120, page_count * 60):
        ocr_text = _ocr_pdf(path)
        if ocr_text:
            text = ocr_text
            warnings.append("Embedded PDF text was sparse; local OCR was used.")
        else:
            warnings.append("The PDF appears scanned and local OCR was unavailable or unsuccessful.")
    return text, page_count, warnings


def _ocr_pdf(path: Path) -> str:
    pdftoppm = shutil.which("pdftoppm")
    tesseract = shutil.which("tesseract")
    if not pdftoppm or not tesseract:
        return ""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    output: list[str] = []
    with tempfile.TemporaryDirectory(prefix="preapproval-ocr-") as tmp:
        prefix = Path(tmp) / "page"
        subprocess.run(
            [pdftoppm, "-png", "-r", "180", str(path), str(prefix)],
            check=True,
            capture_output=True,
            timeout=120,
        )
        for image_path in sorted(Path(tmp).glob("page-*.png")):
            with Image.open(image_path) as image:
                output.append(pytesseract.image_to_string(image))
    return "\n\n".join(output).strip()


def _first_group(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            return normalize_space(match.group(1))
    return None


def _detect_category(text: str) -> str | None:
    from .checklists import load_checklists

    lowered = text.lower()
    matches: list[tuple[int, str]] = []
    for category, checklist in load_checklists().items():
        phrases = {
            category.replace("_", " ").lower(),
            str(checklist.get("display_name", "")).lower(),
            *(str(alias).lower() for alias in checklist.get("aliases", [])),
        }
        for phrase in phrases:
            if phrase and phrase in lowered:
                matches.append((len(phrase), category))
    return max(matches)[1] if matches else None


def deterministic_extract(text: str, page_count: int, warnings: list[str]) -> ApplicationData:
    category = _detect_category(text)
    participant = _first_group(text, [r"Participant[’']s Name\s+Participant[’']s Age\s*\n([^\n]+)"])
    age_match = re.search(r"Participant[’']s Name\s+Participant[’']s Age\s*\n[^\n]*?\b(\d{1,3})\b", text, re.I)
    if participant and age_match:
        participant = re.sub(rf"\s+{re.escape(age_match.group(1))}\s*$", "", participant).strip()
    coordinator_line = _first_group(text, [r"FI Coordinator Name\s+Broker Name\s*\n([^\n]+)"])
    coordinator = broker = None
    if coordinator_line:
        # Sample forms place two names on the same line. Known labels make a two-token split insufficient,
        # so retain the line and let Groq refine it when configured.
        words = coordinator_line.split()
        if len(words) >= 4:
            coordinator = " ".join(words[:2])
            broker = " ".join(words[2:])
        else:
            coordinator = coordinator_line

    url_match = re.search(r"https?://[^\s<>]+", text, re.I)
    url = url_match.group(0).rstrip(".,);]") if url_match else None

    provider = _provider_from_url(url, text)

    item_patterns = {
        "community_class": [r"Class Name\s+Name of Provider/Vendor\s*\n([^\n]+)"],
        "appeal": [r"Class Name\s+Name of Provider/Vendor\s*\n([^\n]+)"],
        "coaching": [r"Name of Coaching Provider\s+Link to Webpage\s*\n([^\n]+)"],
        "membership": [r"Membership Name\s+Name of Provider/Vendor\s*\n([^\n]+)"],
        "hri": [r"Item Requested\s+Link to the Item[^\n]*\n([^\n]+)"],
        "otps": [r"Budget Line Requesting\s+Item Requested\s*\n([^\n]+)"],
        "transition_program": [r"Name of Transition Program\s+Provider\s+Fee per Course\s*\n([^\n]+)"],
    }
    combined = _first_group(text, item_patterns.get(category or "", []))
    requested_item = None
    if combined:
        if category == "coaching":
            clean = combined.split("https://", 1)[0].strip(" -–")
            if "–" in clean or "-" in clean:
                parts = re.split(r"\s+[–-]\s+", clean, maxsplit=1)
                if len(parts) == 2:
                    provider = parts[0]
                    requested_item = parts[1]
                else:
                    requested_item = clean
            else:
                requested_item = clean
        elif category in {"hri", "otps"}:
            requested_item = combined.split("https://", 1)[0].strip()
            if category == "otps":
                requested_item = re.sub(
                    r"^(?:Other goods\s*&\s*services related to (?:health and safety|independence))\s+",
                    "",
                    requested_item,
                    flags=re.I,
                )
        elif category == "transition_program":
            clean = re.sub(r"\s*\$\s*[0-9][^\n]*$", "", combined).strip()
            parts = re.split(r"\s+[–-]\s+", clean, maxsplit=1)
            if len(parts) == 2:
                provider = parts[0]
                requested_item = parts[1]
            else:
                requested_item = clean
        else:
            requested_item = _remove_provider_suffix(combined, provider)

    price_text = None
    billing_period = None
    if category in {"community_class", "appeal", "coaching", "membership", "transition_program"}:
        price_line = _first_group(
            text,
            [
                r"(?:Fee per Session|Fee per Class|Fee per Course|Membership Fee \(Amount\)|Provider Fee per Course)\s*(?:Duration per Session|Billing|Fee per Course)?\s*\n([^\n]*\$[^\n]*)",
                r"(\$\s*[0-9][^\n]*)",
            ],
        )
        if price_line:
            amounts = re.findall(
                r"\$\s*[0-9][0-9,]*(?:\.\d{1,2})?(?:\s+per\s+(?:\d+-minute\s+session|session|class|course|month|year))?",
                price_line,
                re.I,
            )
            price_text = "; ".join(normalize_space(value) for value in amounts) or price_line
            lower_price = price_line.lower()
            billing_period = next(
                (period for period in ("monthly", "yearly", "per session", "per class", "per course") if period in lower_price),
                None,
            )
    subject = _first_group(text, [r"Subject Area/Skill\s+Fee per Session\s+Duration per Session\s*\n([^\n]+)"])
    safety = _first_group(text, [r"Safety features for the item\s*\n([^\n]+)"])
    denial = _first_group(text, [r"Reason for the denial:\s*\n([^\n]+)"])
    appeal = _first_group(text, [r"Justification for Appeal[^:]*:\s*\n(.+?)(?=\nValued outcome)"])

    return ApplicationData(
        participant_name=participant,
        participant_age=int(age_match.group(1)) if age_match else None,
        fi_coordinator=coordinator,
        broker_name=broker,
        category=category,
        requested_item=requested_item,
        provider_name=provider,
        website_url=url,
        requested_price_text=price_text,
        requested_price=extract_price(price_text),
        billing_period=billing_period,
        subject_area=subject,
        safety_features=safety,
        denial_reason=denial,
        appeal_justification=appeal,
        source_pages=page_count,
        extraction_warnings=warnings,
    )


def _provider_from_url(url: str | None, source_text: str = "") -> str | None:
    if not url:
        return None
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url, flags=re.I), flags=re.I).split("/", 1)[0]
    token = host.split(".")[0].lower()
    source_words = re.findall(r"[A-Za-z0-9]+", source_text)
    for start in range(len(source_words)):
        combined = ""
        for end in range(start, min(len(source_words), start + 5)):
            combined += source_words[end].lower()
            if combined == token:
                matched = " ".join(source_words[start : end + 1])
                return matched.title() if matched.islower() else matched
            if len(combined) >= len(token):
                break
    words = re.split(r"[-_]", token)
    return " ".join(word.upper() if any(char.isdigit() for char in word) else word.title() for word in words)


def _remove_provider_suffix(value: str, provider: str | None) -> str:
    clean = normalize_space(value)
    if not provider:
        return clean
    normalized_value = re.sub(r"[^a-z0-9]", "", clean.lower())
    normalized_provider = re.sub(r"[^a-z0-9]", "", provider.lower())
    if normalized_value.endswith(normalized_provider):
        words = clean.split()
        for index in range(len(words)):
            suffix = re.sub(r"[^a-z0-9]", "", "".join(words[index:]).lower())
            if suffix == normalized_provider:
                return " ".join(words[:index]).strip()
    return clean


async def extract_application(path: Path, groq: GroqAdapter) -> tuple[ApplicationData, str]:
    text, pages, warnings = extract_pdf_text(path)
    baseline = deterministic_extract(text, pages, warnings)
    if groq.enabled:
        refined = await groq.extract_application(text, baseline)
        if refined:
            refined.source_pages = pages
            refined.extraction_warnings = warnings
            baseline = refined
    missing = []
    for label, value in (
        ("category", baseline.category),
        ("website URL", baseline.website_url),
        ("requested item or service", baseline.requested_item),
    ):
        if not value:
            missing.append(label)
    if missing:
        baseline.extraction_warnings.append("Clarification required for: " + ", ".join(missing) + ".")
    return baseline, text
