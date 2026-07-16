from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PIL import Image

from .browser_interaction import DEFAULT_GEOLOCATION, reveal_public_information
from .config import ROOT_DIR, settings
from .evidence import stamp_image
from .models import ApplicationData, CrawledPage, Criterion, EvidenceRecord, Finding, FindingStatus, VisionCapture
from .utils import format_exception, relative_to_root, sha256_file


_ANTIBOT_TERMS = (
    "access denied",
    "anti-bot",
    "bot challenge",
    "captcha",
    "checking your browser",
    "cloudflare",
    "forbidden",
    "security check",
    "unusual traffic",
    "verify you are human",
)

_CONTEXT_STOPWORDS = {
    "and", "for", "from", "membership", "member", "price", "program", "requested",
    "service", "the", "with", "year",
}


def _canonical_url(url: str) -> str:
    """Normalize only for deduplication; never invent or rewrite a destination."""
    try:
        parts = urlsplit(url.strip())
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))
    except Exception:
        return url.strip()


def _vision_url_order(start_url: str, pages: list[CrawledPage], limit: int = 3) -> list[str]:
    """Prefer crawler-ranked evidence pages over an unverified form URL."""
    ordered: list[str] = []
    seen: set[str] = set()
    candidates = [page.url for page in sorted(pages, key=lambda page: page.score, reverse=True)]
    candidates.append(start_url)
    for candidate in candidates:
        key = _canonical_url(candidate)
        if candidate and key not in seen:
            ordered.append(candidate)
            seen.add(key)
        if len(ordered) >= limit:
            break
    return ordered


def _application_context_tokens(application: ApplicationData) -> list[str]:
    text = " ".join(
        value or ""
        for value in (
            application.requested_item,
            application.category,
            application.subject_area,
        )
    ).lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9'-]{2,}", text)
    return list(dict.fromkeys(token for token in tokens if token not in _CONTEXT_STOPWORDS))[:12]


def _is_price_criterion(criterion: Criterion) -> bool:
    text = f"{criterion.id} {criterion.label} {criterion.description} {criterion.rule or ''}".lower()
    return any(term in text for term in ("price", "fee", "cost", "tuition", "cap"))


def select_vision_fallback_criteria(
    criteria: list[Criterion],
    text_findings: dict[str, Finding],
    pages: list[CrawledPage],
    crawl_warnings: list[str],
) -> tuple[list[Criterion], list[str]]:
    """Select only criteria that need visual recovery and explain why."""
    combined_warnings = " ".join(crawl_warnings).lower()
    page_text = " ".join(page.text or page.markdown for page in pages).lower()
    crawler_failed = not pages or "crawl4ai could not normalize" in combined_warnings
    antibot = any(term in combined_warnings or term in page_text for term in _ANTIBOT_TERMS)
    reasons: list[str] = []
    selected: list[Criterion] = []
    if crawler_failed or antibot:
        selected = list(criteria)
        reasons.append("crawler_or_antibot")
    for criterion in criteria:
        finding = text_findings.get(criterion.id)
        if _is_price_criterion(criterion) and (
            finding is None or finding.status != FindingStatus.FOUND
        ):
            if criterion not in selected:
                selected.append(criterion)
            if "unresolved_price" not in reasons:
                reasons.append("unresolved_price")
    return selected, reasons


def _prepare_vision_image(source: Path, target: Path) -> None:
    """Create the exact JPEG sent to Groq, bounded below its base64 request limit."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source).convert("RGB") as image:
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        quality = 82
        image.save(target, format="JPEG", quality=quality, optimize=True)
        while target.stat().st_size > 2_800_000 and quality > 42:
            quality -= 10
            image.save(target, format="JPEG", quality=quality, optimize=True)


async def capture_vision_candidates(
    start_url: str,
    pages: list[CrawledPage],
    review_dir: Path,
    application: ApplicationData,
) -> tuple[list[VisionCapture], list[str]]:
    """Capture targeted pricing context plus visual fallbacks without bypassing access controls."""
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return [], [f"Vision fallback browser is unavailable: {format_exception(exc)}"]

    urls = _vision_url_order(start_url, pages)
    context_tokens = _application_context_tokens(application)
    captures: list[VisionCapture] = []
    warnings: list[str] = []
    raw_dir = review_dir / "evidence" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
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
            for url in urls:
                if len(captures) >= settings.vision_max_images:
                    break
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=settings.crawl_timeout_ms)
                    await page.wait_for_timeout(1200)
                    await reveal_public_information(page)
                    title = (await page.title()).strip()
                    try:
                        rendered_text = (await page.locator("body").inner_text(timeout=5000)).lower()
                    except Exception:
                        rendered_text = ""
                    blocked = any(term in rendered_text or term in title.lower() for term in _ANTIBOT_TERMS)
                    height = await page.evaluate(
                        "Math.max(document.body?.scrollHeight || 0, document.documentElement.scrollHeight || 0)"
                    )
                    targeted_candidates = await page.locator("body").evaluate(
                        r"""(root, request) => {
                            const amount = request.amount;
                            const tokens = request.tokens || [];
                            const visible = el => {
                                const r = el.getBoundingClientRect();
                                const style = getComputedStyle(el);
                                return r.width >= 40 && r.height >= 16 && style.display !== 'none' && style.visibility !== 'hidden';
                            };
                            const money = /(?:\$|USD\s*)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/gi;
                            const hasMoney = /(?:\$|USD\s*)\s*[0-9]/i;
                            const leaves = Array.from(root.querySelectorAll('*')).filter(el => {
                                const text = el.innerText || '';
                                return visible(el) && text.length <= 500 && hasMoney.test(text);
                            });
                            const results = [];
                            const used = new Set();
                            for (const leaf of leaves) {
                                const leafText = (leaf.innerText || '').replace(/\s+/g, ' ').trim();
                                money.lastIndex = 0;
                                const values = Array.from(leafText.matchAll(money)).map(m => Number(m[1].replace(/,/g, '')));
                                const exact = amount != null && values.some(value => Math.abs(value - amount) < 0.005);
                                let target = leaf.closest('table');
                                if (!target) {
                                    let current = leaf;
                                    for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
                                        const text = (current.innerText || '').replace(/\s+/g, ' ').trim();
                                        const rect = current.getBoundingClientRect();
                                        const tokenHits = tokens.filter(token => text.toLowerCase().includes(token)).length;
                                        if (visible(current) && text.length <= 3500 && rect.height <= 1400 && rect.width <= 1800 &&
                                            (tokenHits > 0 || exact)) target = current;
                                    }
                                }
                                target = target || leaf;
                                const rect = target.getBoundingClientRect();
                                const text = (target.innerText || '').replace(/\s+/g, ' ').trim();
                                if (!visible(target) || rect.height > 1600 || rect.width > 1900 || text.length > 5000 || used.has(target)) continue;
                                used.add(target);
                                const tokenHits = tokens.filter(token => text.toLowerCase().includes(token)).length;
                                const tableBonus = target.tagName === 'TABLE' ? 30 : 0;
                                results.push({target, score: (exact ? 1000 : 0) + tokenHits * 80 + tableBonus, exact, tokenHits});
                            }
                            return results.sort((a, b) => b.score - a.score).slice(0, 3).map((item, index) => {
                                item.target.setAttribute('data-evidence-vision-target', String(index));
                                return {index, exact: item.exact, tokenHits: item.tokenHits};
                            });
                        }""",
                        {"amount": application.requested_price, "tokens": context_tokens},
                    )
                    for candidate in targeted_candidates[:2]:
                        if len(captures) >= settings.vision_max_images:
                            break
                        capture_number = len(captures) + 1
                        png_path = raw_dir / f"vision-{capture_number:02d}.png"
                        jpeg_path = raw_dir / f"vision-{capture_number:02d}.jpg"
                        # Hide only already-visible fixed/sticky top chrome before
                        # Playwright scrolls a pricing element into view. Otherwise
                        # site navigation can cover the offering labels.
                        await page.evaluate(
                            """() => Array.from(document.querySelectorAll('*')).forEach(el => {
                                const style = getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                if ((style.position === 'fixed' || style.position === 'sticky') &&
                                    rect.top < 250 && rect.bottom > 0 && rect.height < 400) {
                                    el.style.setProperty('visibility', 'hidden', 'important');
                                }
                            })"""
                        )
                        target = page.locator(
                            f'[data-evidence-vision-target="{int(candidate["index"])}"]'
                        )
                        await target.screenshot(
                            path=str(png_path),
                            animations="disabled",
                            timeout=10000,
                        )
                        _prepare_vision_image(png_path, jpeg_path)
                        png_path.unlink(missing_ok=True)
                        descriptor = "targeted pricing context"
                        if candidate.get("exact"):
                            descriptor = "exact requested-price context"
                        captures.append(
                            VisionCapture(
                                id=f"VIS-{capture_number:02d}",
                                url=page.url,
                                title=f"{title} — {descriptor}",
                                path=str(jpeg_path),
                                blocked=blocked,
                            )
                        )
                    money_positions = await page.locator("body").evaluate(
                        r"""root => Array.from(root.querySelectorAll('*'))
                            .filter(el => el.children.length <= 2 && /\$\s*[0-9]/.test(el.innerText || ''))
                            .map(el => ({y: el.getBoundingClientRect().top + window.scrollY, length: (el.innerText || '').length}))
                            .filter(x => x.length <= 500)
                            .sort((a, b) => a.y - b.y)
                            .slice(0, 4)"""
                    )
                    image_candidates = await page.locator("img").evaluate_all(
                        """els => els.map(el => {
                            const r = el.getBoundingClientRect();
                            const text = `${el.alt || ''} ${el.src || ''}`.toLowerCase();
                            const keyword = /(price|pricing|fee|fees|tuition|rate|rates|cost|menu|membership|class)/.test(text) ? 10 : 0;
                            return {y: r.top + window.scrollY, area: Math.max(0, r.width) * Math.max(0, r.height), keyword};
                        }).filter(x => x.area >= 12000).sort((a, b) => (b.keyword - a.keyword) || (b.area - a.area)).slice(0, 4)"""
                    )
                    positions: list[int] = [0]
                    for candidate in money_positions:
                        position = max(0, int(float(candidate.get("y", 0))) - 260)
                        if all(abs(position - existing) >= 220 for existing in positions):
                            positions.append(position)
                    for candidate in image_candidates:
                        position = max(0, int(float(candidate.get("y", 0))) - 260)
                        if all(abs(position - existing) >= 220 for existing in positions):
                            positions.append(position)
                    if len(positions) < 2 and height > 1400:
                        positions.append(max(0, int(height / 2) - 500))
                    for position in positions[:3]:
                        if len(captures) >= settings.vision_max_images:
                            break
                        await page.evaluate("y => window.scrollTo({left: 0, top: y, behavior: 'instant'})", position)
                        await page.wait_for_timeout(200)
                        capture_number = len(captures) + 1
                        png_path = raw_dir / f"vision-{capture_number:02d}.png"
                        jpeg_path = raw_dir / f"vision-{capture_number:02d}.jpg"
                        await page.screenshot(
                            path=str(png_path),
                            full_page=False,
                            animations="disabled",
                            timeout=10000,
                        )
                        _prepare_vision_image(png_path, jpeg_path)
                        png_path.unlink(missing_ok=True)
                        captures.append(
                            VisionCapture(
                                id=f"VIS-{capture_number:02d}",
                                url=page.url,
                                title=title,
                                path=str(jpeg_path),
                                blocked=blocked,
                            )
                        )
                except Exception as exc:
                    warnings.append(f"Could not prepare visual fallback for {url}: {format_exception(exc)}")
                finally:
                    await page.close()
            await context.close()
            await browser.close()
    except Exception as exc:
        warnings.append(f"Vision fallback capture failed: {format_exception(exc)}")
    return captures, warnings


def materialize_vision_evidence(
    review_id: str,
    review_dir: Path,
    findings: list[Finding],
    captures: list[VisionCapture],
) -> list[EvidenceRecord]:
    """Preserve the exact model input as both source and criterion evidence."""
    capture_by_id = {capture.id: capture for capture in captures}
    records: list[EvidenceRecord] = []
    full_record_by_capture: dict[str, str] = {}
    captured_at = datetime.now(timezone.utc)
    vision_findings = [
        finding
        for finding in findings
        if finding.status == FindingStatus.FOUND
        and finding.source == "groq-vision"
        and finding.visual_capture_id in capture_by_id
    ]
    for index, finding in enumerate(vision_findings, 1):
        capture = capture_by_id[finding.visual_capture_id or ""]
        source_path = Path(capture.path)
        full_id = full_record_by_capture.get(capture.id)
        if not full_id:
            full_id = f"EV-VIS-FULL-{len(full_record_by_capture) + 1:02d}"
            full_target = review_dir / "evidence" / "full" / f"{capture.id.lower()}-stamped.png"
            stamp_image(
                source_path,
                full_target,
                capture.url,
                "Visual fallback source image",
                review_id,
                captured_at,
            )
            records.append(
                EvidenceRecord(
                    id=full_id,
                    kind="vision_page",
                    url=capture.url,
                    captured_at=captured_at.isoformat(),
                    raw_path=relative_to_root(source_path, ROOT_DIR),
                    stamped_path=relative_to_root(full_target, ROOT_DIR),
                    sha256=sha256_file(full_target),
                )
            )
            full_record_by_capture[capture.id] = full_id
        evidence_id = f"EV-VIS-{index:03d}"
        target = review_dir / "evidence" / "targeted" / f"{finding.criterion_id}-vision-stamped.png"
        stamp_image(
            source_path,
            target,
            capture.url,
            f"Visual evidence: {finding.label}",
            review_id,
            captured_at,
        )
        records.append(
            EvidenceRecord(
                id=evidence_id,
                criterion_id=finding.criterion_id,
                kind="targeted",
                url=capture.url,
                captured_at=captured_at.isoformat(),
                raw_path=relative_to_root(source_path, ROOT_DIR),
                stamped_path=relative_to_root(target, ROOT_DIR),
                quote=finding.quote,
                sha256=sha256_file(target),
            )
        )
        finding.evidence_ids.extend([full_id, evidence_id])
    return records
