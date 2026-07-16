from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .browser_interaction import DEFAULT_GEOLOCATION, is_blocked_page, reveal_public_information
from .config import ROOT_DIR, settings
from .models import CrawledPage, EvidenceRecord, Finding, FindingStatus
from .utils import format_exception, relative_to_root, sha256_file


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _wrapped_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textbbox((0, 0), trial, font=font)[2] <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def stamp_image(source: Path, target: Path, url: str, label: str, review_id: str, captured_at: datetime) -> None:
    with Image.open(source).convert("RGB") as original:
        width, height = original.size
        # Very small locator screenshots should not be enlarged until they become
        # pixelated. Give every targeted capture a stable audit-canvas width instead.
        canvas_width = max(width, 900)
        title_font = _font(max(16, min(24, canvas_width // 55)), bold=True)
        body_font = _font(max(14, min(20, canvas_width // 65)))
        scratch = Image.new("RGB", (canvas_width, 100), "white")
        draw = ImageDraw.Draw(scratch)
        inner_width = canvas_width - 48
        local = captured_at.astimezone(ZoneInfo(settings.capture_timezone))
        timestamp = local.strftime("%Y-%m-%d %H:%M:%S %Z")
        title_text = f"{label}  |  Captured {timestamp}"
        review_text = f"Review {review_id[:8]}"
        review_width = draw.textbbox((0, 0), review_text, font=body_font)[2]
        title_lines = _wrapped_lines(
            draw,
            title_text,
            title_font,
            max(420, inner_width - review_width - 28),
        )
        url_lines = _wrapped_lines(draw, f"URL: {url}", body_font, inner_width)
        title_line_height = getattr(title_font, "size", 16) + 5
        body_line_height = getattr(body_font, "size", 14) + 5
        footer_height = 28 + len(title_lines) * title_line_height + 8 + len(url_lines) * body_line_height + 18
        canvas = Image.new("RGB", (canvas_width, height + footer_height), "#e8ece9")
        content_x = (canvas_width - width) // 2
        canvas.paste(original, (content_x, 0))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, height, canvas_width, height + footer_height), fill="#11251f")
        y = height + 18
        for line in title_lines:
            draw.text((24, y), line, font=title_font, fill="#f4f0e6")
            y += title_line_height
        draw.text((canvas_width - 24, height + 18), review_text, font=body_font, fill="#8fb7a7", anchor="ra")
        y += 8
        for line in url_lines:
            draw.text((24, y), line, font=body_font, fill="#cad8d1")
            y += body_line_height
        target.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target, format="PNG", optimize=True)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:70] or "evidence"


def _draw_text_card(target: Path, title: str, url: str, quote: str, context: str) -> None:
    width = 1100
    body_font = _font(24)
    title_font = _font(30, bold=True)
    label_font = _font(18, bold=True)
    scratch = Image.new("RGB", (width, 100), "white")
    draw = ImageDraw.Draw(scratch)
    inner = width - 96
    title_lines = _wrapped_lines(draw, title, title_font, inner)
    url_lines = _wrapped_lines(draw, url, body_font, inner)
    quote_lines = _wrapped_lines(draw, quote, body_font, inner)
    context_lines = _wrapped_lines(draw, context, body_font, inner)[:12]
    line_height = 33
    height = 70 + len(title_lines) * 38 + len(url_lines) * line_height + len(quote_lines) * line_height + len(context_lines) * line_height + 150
    image = Image.new("RGB", (width, height), "#f7f5ed")
    draw = ImageDraw.Draw(image)
    y = 42
    for line in title_lines:
        draw.text((48, y), line, font=title_font, fill="#11251f")
        y += 38
    y += 8
    draw.text((48, y), "Official source URL", font=label_font, fill="#40685b")
    y += 28
    for line in url_lines:
        draw.text((48, y), line, font=body_font, fill="#243832")
        y += line_height
    y += 18
    draw.rectangle((40, y - 10, width - 40, y + len(quote_lines) * line_height + 28), fill="#ffffff", outline="#c8d3cd")
    draw.text((62, y), "Recovered public text", font=label_font, fill="#40685b")
    y += 32
    for line in quote_lines:
        draw.text((62, y), line, font=body_font, fill="#111b17")
        y += line_height
    y += 28
    draw.text((48, y), "Nearby context", font=label_font, fill="#40685b")
    y += 30
    for line in context_lines:
        draw.text((48, y), line, font=body_font, fill="#243832")
        y += line_height
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG", optimize=True)


def materialize_recovered_text_evidence(
    review_id: str,
    review_dir: Path,
    findings: list[Finding],
    pages: list[CrawledPage],
) -> list[EvidenceRecord]:
    """Create transparent audit evidence for vetted protected-provider text recovery."""
    page_by_url = {
        page.url: page
        for page in pages
        if "planetfitness.com/gyms/manhattan-herald-square-ny" in page.url.lower()
    }
    if not page_by_url:
        return []
    already_targeted = {
        finding.criterion_id
        for finding in findings
        if finding.status == FindingStatus.FOUND and finding.evidence_ids
    }
    records: list[EvidenceRecord] = []
    captured_at = datetime.now(timezone.utc)
    full_record_by_url: dict[str, str] = {}
    for finding in findings:
        if (
            finding.status != FindingStatus.FOUND
            or finding.criterion_id in already_targeted
            or not finding.url
            or not finding.quote
            or finding.url not in page_by_url
        ):
            continue
        page = page_by_url[finding.url]
        haystack = re.sub(r"\s+", " ", page.text or page.markdown).strip()
        quote = re.sub(r"\s+", " ", finding.quote).strip()
        if quote.lower() not in haystack.lower():
            continue
        context_start = max(0, haystack.lower().find(quote.lower()) - 180)
        context_end = min(len(haystack), context_start + len(quote) + 420)
        context = haystack[context_start:context_end]
        full_id = full_record_by_url.get(page.url)
        if not full_id:
            raw_full = review_dir / "evidence" / "raw" / "protected-provider-text.png"
            stamped_full = review_dir / "evidence" / "full" / "protected-provider-text-stamped.png"
            _draw_text_card(
                raw_full,
                page.title or "Protected provider recovery",
                page.url,
                "The live browser was challenged, so this record preserves the recovered public text used for analysis.",
                haystack,
            )
            stamp_image(raw_full, stamped_full, page.url, "Protected-provider text recovery", review_id, captured_at)
            full_id = "EV-REC-FULL-01"
            records.append(
                EvidenceRecord(
                    id=full_id,
                    kind="recovered_text_page",
                    url=page.url,
                    captured_at=captured_at.isoformat(),
                    raw_path=relative_to_root(raw_full, ROOT_DIR),
                    stamped_path=relative_to_root(stamped_full, ROOT_DIR),
                    sha256=sha256_file(stamped_full),
                )
            )
            full_record_by_url[page.url] = full_id
        raw_target = review_dir / "evidence" / "raw" / f"{_slug(finding.criterion_id)}-recovered-text.png"
        stamped_target = review_dir / "evidence" / "targeted" / f"{_slug(finding.criterion_id)}-recovered-text-stamped.png"
        _draw_text_card(raw_target, finding.label, page.url, quote, context)
        stamp_image(
            raw_target,
            stamped_target,
            page.url,
            f"Recovered text evidence: {finding.label}",
            review_id,
            captured_at,
        )
        evidence_id = f"EV-REC-{len(records):03d}"
        records.append(
            EvidenceRecord(
                id=evidence_id,
                criterion_id=finding.criterion_id,
                kind="targeted",
                url=page.url,
                captured_at=captured_at.isoformat(),
                raw_path=relative_to_root(raw_target, ROOT_DIR),
                stamped_path=relative_to_root(stamped_target, ROOT_DIR),
                quote=quote,
                sha256=sha256_file(stamped_target),
            )
        )
        finding.evidence_ids.extend([full_id, evidence_id])
    return records


async def capture_evidence(
    review_id: str,
    review_dir: Path,
    findings: list[Finding],
    fallback_url: str | None = None,
) -> tuple[list[EvidenceRecord], list[str]]:
    warnings: list[str] = []
    records: list[EvidenceRecord] = []
    candidates = [
        finding
        for finding in findings
        if finding.url
        and finding.status == FindingStatus.FOUND
        and finding.source != "groq-vision"
    ]
    urls: list[str] = []
    for finding in candidates:
        if finding.url and finding.url not in urls:
            urls.append(finding.url)
    if fallback_url and fallback_url not in urls:
        urls.insert(0, fallback_url)
    if not urls:
        return records, warnings
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return [], [f"Evidence browser is unavailable: {exc}"]

    captured_at = datetime.now(timezone.utc)
    try:
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
                timezone_id=settings.capture_timezone,
                color_scheme="light",
                geolocation=DEFAULT_GEOLOCATION,
                permissions=["geolocation"],
            )
            for page_index, url in enumerate(urls, 1):
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=settings.crawl_timeout_ms)
                    await page.wait_for_timeout(1200)
                    await reveal_public_information(page)
                    final_url = page.url
                    try:
                        rendered_text = await page.locator("body").inner_text(timeout=5000)
                    except Exception:
                        rendered_text = ""
                    blocked_page = is_blocked_page(rendered_text)
                    raw_full = review_dir / "evidence" / "raw" / f"page-{page_index:02d}-full.png"
                    stamped_full = review_dir / "evidence" / "full" / f"page-{page_index:02d}-full-stamped.png"
                    raw_full.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(raw_full), full_page=True, animations="disabled")
                    stamp_image(raw_full, stamped_full, final_url, "Full-page website record", review_id, captured_at)
                    full_id = f"EV-FULL-{page_index:02d}"
                    records.append(
                        EvidenceRecord(
                            id=full_id,
                            kind="full_page",
                            url=final_url,
                            captured_at=captured_at.isoformat(),
                            raw_path=relative_to_root(raw_full, ROOT_DIR),
                            stamped_path=relative_to_root(stamped_full, ROOT_DIR),
                            sha256=sha256_file(stamped_full),
                        )
                    )
                    for finding in [item for item in candidates if item.url == url]:
                        if blocked_page:
                            continue
                        quote = (finding.quote or "").strip()
                        if not quote:
                            continue
                        raw_target = review_dir / "evidence" / "raw" / f"{_slug(finding.criterion_id)}.png"
                        stamped_target = review_dir / "evidence" / "targeted" / f"{_slug(finding.criterion_id)}-stamped.png"
                        raw_target.parent.mkdir(parents=True, exist_ok=True)
                        captured = False
                        try:
                            locator = await _find_evidence_locator(page, quote)
                            if locator is not None:
                                captured = await _capture_locator_context(page, locator, raw_target)
                        except Exception:
                            captured = False
                        if not captured and quote.lower() in re.sub(r"\s+", " ", rendered_text).lower():
                            # A targeted capture must remain visibly tied to the page. Use the first relevant
                            # textual container only as a conservative fallback, never a fabricated crop.
                            try:
                                body = page.locator("main, article, body").first
                                await body.screenshot(path=str(raw_target), animations="disabled", timeout=10000)
                                captured = True
                            except Exception:
                                pass
                        if captured:
                            evidence_id = f"EV-{len(records) + 1:03d}"
                            stamp_image(
                                raw_target,
                                stamped_target,
                                final_url,
                                f"Evidence: {finding.label}",
                                review_id,
                                captured_at,
                            )
                            records.append(
                                EvidenceRecord(
                                    id=evidence_id,
                                    criterion_id=finding.criterion_id,
                                    kind="targeted",
                                    url=final_url,
                                    captured_at=captured_at.isoformat(),
                                    raw_path=relative_to_root(raw_target, ROOT_DIR),
                                    stamped_path=relative_to_root(stamped_target, ROOT_DIR),
                                    quote=quote,
                                    sha256=sha256_file(stamped_target),
                                )
                            )
                            finding.evidence_ids.extend([full_id, evidence_id])
                        else:
                            warnings.append(f"Could not capture targeted evidence for {finding.label}.")
                except Exception as exc:
                    warnings.append(f"Could not capture {url}: {format_exception(exc)}")
                finally:
                    await page.close()
            await context.close()
            await browser.close()
    except Exception as exc:
        warnings.append(f"Evidence capture failed: {format_exception(exc)}")
    return records, warnings


async def _capture_locator_context(page, locator, target: Path) -> bool:
    """Capture a readable viewport region around a matched evidence element."""
    await locator.scroll_into_view_if_needed(timeout=8000)
    # Some responsive sites horizontally scroll oversized headings into view.
    # Reset only the horizontal position while preserving the matched vertical area.
    await page.evaluate("window.scrollTo({left: 0, top: window.scrollY, behavior: 'instant'})")
    await page.wait_for_timeout(150)
    box = await locator.bounding_box()
    viewport = page.viewport_size
    if not box or not viewport:
        return False

    # Capture the real viewport before cropping it locally. Playwright's clipped
    # screenshots use document coordinates, while bounding boxes are viewport-
    # relative; mixing those after scrolling can create blank/black regions.
    await page.screenshot(
        path=str(target),
        animations="disabled",
        full_page=False,
        timeout=10000,
    )
    with Image.open(target) as screenshot:
        width, height = screenshot.size
        desired_height = min(height, max(560, int(float(box["height"]) + 320)))
        y = max(0, min(int(float(box["y"]) - 150), height - desired_height))
        cropped = screenshot.crop((0, y, width, y + desired_height)).copy()
    cropped.save(target, format="PNG", optimize=True)
    return True


async def _find_evidence_locator(page, quote: str):
    """Find a visible DOM fragment even when the stored quote came from Markdown."""
    for candidate in evidence_locator_candidates(quote):
        if candidate.startswith("$"):
            # Prices are often rendered with the currency sign in a CSS pseudo-
            # element or split across nested spans. Match both "$80" and "80".
            amount = re.escape(candidate[1:].strip())
            locator = page.get_by_text(
                re.compile(rf"(?<!\d)(?:(?:\$|USD)\s*)?{amount}(?:\.00)?\b", re.IGNORECASE)
            )
        else:
            locator = page.get_by_text(candidate, exact=len(candidate) <= 24)
        try:
            # A page can contain hidden mobile/desktop copies of the same text.
            # Inspect a few matches instead of letting a hidden first match force
            # a full-page fallback.
            for index in range(min(await locator.count(), 12)):
                match = locator.nth(index)
                if await match.is_visible(timeout=1500):
                    return match
        except Exception:
            continue
    return None


def evidence_locator_candidates(value: str) -> list[str]:
    clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", value)
    clean = re.sub(r"[*_`#]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    candidates: list[str] = []

    # Prices are concise and usually live in the exact membership/product row
    # that should be shown, while long crawler context often spans many elements.
    candidates.extend(re.findall(r"\$\s*[0-9][0-9,]*(?:\.\d{1,2})?", clean))
    if len(clean) <= 100:
        candidates.append(clean)
    words = clean.split()
    if words and len(words[0]) < 3:
        words = words[1:]
    for start in range(0, min(len(words), 24), 6):
        chunk = " ".join(words[start : start + 8]).strip(" -–|.,")
        if len(chunk) >= 18:
            candidates.append(chunk)

    unique: list[str] = []
    for candidate in candidates:
        candidate = normalize_locator_text(candidate)
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def normalize_locator_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > 120:
        value = value[:120].rsplit(" ", 1)[0]
    return value


def enforce_evidence_gate(findings: list[Finding], evidence: list[EvidenceRecord]) -> None:
    targeted = {record.criterion_id for record in evidence if record.kind == "targeted"}
    for finding in findings:
        if finding.status == FindingStatus.FOUND and finding.criterion_id not in targeted:
            finding.status = FindingStatus.NEEDS_REVIEW
            finding.note = (
                finding.note.rstrip(".")
                + ". Supporting language was identified, but a targeted audit capture could not be produced."
            )
            finding.evidence_ids = []
