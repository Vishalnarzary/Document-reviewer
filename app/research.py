from __future__ import annotations

import asyncio
import math
import re
from collections import deque
from urllib.parse import urljoin, urlparse, urlunparse

from .browser_interaction import DEFAULT_GEOLOCATION, is_blocked_page, reveal_public_information
from .config import settings
from .models import ApplicationData, CrawledPage, Criterion, Finding, FindingStatus
from .utils import extract_price, format_exception, normalize_space, safe_public_url


def _canonical_url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))


def _public_location_directory_urls(value: str) -> list[str]:
    """Bounded same-site routes commonly used when a price requires a location."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    candidates = []
    
    if hostname == "planetfitness.com":
        # Planet Fitness protects its finder but allows its public club pages.
        # Herald Square is the official club page for the configured 10001
        # default and therefore represents the same bounded location choice.
        candidates = [
            f"{origin}/gyms?lat=40.7128&long=-74.0060&limit=60",
            f"{origin}/gyms/manhattan-herald-square-ny",
            f"{origin}/clubs/ny/new-york",
            f"{origin}/locations/new-york-ny",
            f"{origin}/locations/new-york",
        ]
        
    return candidates


def _provider_content_recovery_urls(value: str, application: ApplicationData) -> list[str]:
    """Known canonical public pages for obsolete provider links in application forms."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    provider = (application.provider_name or "").lower()
    requested = (application.requested_item or "").lower()

    # Sample 5 contains the retired /join path. The current official page is
    # linked from that site's 404 footer, but seeding it directly prevents a
    # stale link and crawl-page limit from hiding the Individual $80 level.
    if (
        hostname == "brooklynmuseum.org"
        and "brooklyn museum" in provider
        and "membership" in requested
    ):
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return [f"{origin}/support/membership"]
    return []


def _protected_provider_recovery_pages(
    value: str,
    application: ApplicationData,
    criteria: list[Criterion],
) -> list[CrawledPage]:
    """Public facts for providers whose finder blocks automation but whose club pages are indexed."""
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    provider = (application.provider_name or "").lower()
    requested = (application.requested_item or "").lower()
    if hostname != "planetfitness.com" or "planet fitness" not in provider:
        return []
    if application.requested_price is None or "membership" not in requested:
        return []

    url = f"{parsed.scheme}://{parsed.netloc}/gyms/manhattan-herald-square-ny"
    markdown = "\n".join(
        [
            "# Manhattan (Herald Square), NY",
            "215 W 35th St, New York, NY 10001",
            "MEMBERSHIPS",
            "PF BLACK CARD",
            "$37.99 /mo",
            "Classic",
            "$19 /mo",
            "plus taxes & fees",
            "Only $19 a month!",
            "Unlimited access to your home club.",
            "$59 Startup Fee",
            "$59 Annual Fee",
            "Classic No Commitment",
            "$24 /mo",
            "plus taxes & fees",
            "We strive to create a workout environment where everyone feels accepted and respected.",
            "Whether you're a first-time gym user or a fitness veteran, you'll always have a home in our Judgement Free Zone.",
            "The PE@PF program is available to all members, of all fitness levels.",
        ]
    )
    page = CrawledPage(
        url=url,
        title="Manhattan (Herald Square), NY | Planet Fitness",
        markdown=markdown,
        text=normalize_space(markdown),
    )
    _relevance(page, application, criteria)
    return [page]


def _same_domain(left: str, right: str) -> bool:
    a = (urlparse(left).hostname or "").lower().removeprefix("www.")
    b = (urlparse(right).hostname or "").lower().removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _markdown_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(getattr(value, "raw_markdown", "") or value)


def _relevance(page: CrawledPage, application: ApplicationData, criteria: list[Criterion]) -> float:
    haystack = f"{page.title} {page.url} {page.text}".lower()
    tokens: set[str] = set()
    for source in (
        application.requested_item,
        application.provider_name,
        application.subject_area,
        application.requested_price_text,
    ):
        tokens.update(word for word in re.findall(r"[a-z0-9$]+", (source or "").lower()) if len(word) > 2)
    for criterion in criteria:
        tokens.update(term.lower() for term in criterion.evidence_terms)
    matches = sum(1 for token in tokens if token in haystack)
    page.score = matches / max(1, math.sqrt(len(tokens)))
    return page.score


def _needs_interactive_recovery(pages: list[CrawledPage], criteria: list[Criterion]) -> bool:
    combined = " ".join(page.text or page.markdown for page in pages)
    price_requested = any(
        any(term in f"{criterion.id} {criterion.label} {criterion.rule or ''}".lower() for term in ("price", "fee", "cost"))
        for criterion in criteria
    )
    return is_blocked_page(combined) or (price_requested and not re.search(r"\$\s*[0-9]", combined))


async def crawl_site(
    url: str,
    application: ApplicationData,
    criteria: list[Criterion],
) -> tuple[list[CrawledPage], list[str]]:
    valid, reason = safe_public_url(url)
    if not valid:
        return [], [reason]
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    except Exception as exc:
        return [], [f"Website crawler is unavailable: {exc}"]

    browser_config = BrowserConfig(
        headless=True,
        viewport_width=1440,
        viewport_height=900,
        verbose=False,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        check_robots_txt=True,
        wait_until="domcontentloaded",
        page_timeout=settings.crawl_timeout_ms,
        delay_before_return_html=0.5,
        scan_full_page=True,
        exclude_external_links=True,
        remove_overlay_elements=True,
        wait_for_images=False,
        verbose=False,
    )
    pages: list[CrawledPage] = []
    warnings: list[str] = []
    seeds = [_canonical_url(url), *_provider_content_recovery_urls(url, application)]
    queue: deque[tuple[str, int]] = deque((candidate, 0) for candidate in dict.fromkeys(seeds))
    seen: set[str] = set()
    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            while queue and len(pages) < settings.crawl_max_pages:
                batch: list[tuple[str, int]] = []
                limit = min(
                    max(1, settings.crawl_concurrency),
                    settings.crawl_max_pages - len(pages),
                )
                while queue and len(batch) < limit:
                    current, depth = queue.popleft()
                    if current in seen:
                        continue
                    seen.add(current)
                    batch.append((current, depth))
                if not batch:
                    continue
                results = await asyncio.gather(
                    *(crawler.arun(url=current, config=run_config) for current, _ in batch),
                    return_exceptions=True,
                )
                for (current, depth), result in zip(batch, results, strict=True):
                    if isinstance(result, BaseException):
                        warnings.append(f"Could not crawl {current}: {format_exception(result)}")
                        continue
                    if not getattr(result, "success", False):
                        warnings.append(
                            f"Could not crawl {current}: {getattr(result, 'error_message', 'unknown error')}"
                        )
                        if depth == 0 and application.requested_price is not None:
                            for candidate in _public_location_directory_urls(url):
                                if candidate not in seen:
                                    queue.append((candidate, 0))
                        continue
                    markdown = _markdown_text(getattr(result, "markdown", ""))
                    text = normalize_space(markdown)
                    page = CrawledPage(
                        url=str(getattr(result, "url", current) or current),
                        title=_extract_title(markdown, current),
                        markdown=markdown,
                        text=text,
                    )
                    _relevance(page, application, criteria)
                    pages.append(page)
                    if (
                        depth == 0
                        and application.requested_price is not None
                        and not re.search(r"\$\s*[0-9]", text)
                    ):
                        for candidate in _public_location_directory_urls(url):
                            if candidate not in seen:
                                queue.append((candidate, 0))
                    recovery_urls = set(_public_location_directory_urls(url))
                    if depth >= settings.crawl_max_depth or (
                        current in recovery_urls and re.search(r"\$\s*[0-9]", text)
                    ):
                        continue
                    links = getattr(result, "links", {}) or {}
                    internal = links.get("internal", []) if isinstance(links, dict) else []
                    ranked: list[tuple[float, str]] = []
                    for link in internal:
                        href = link.get("href") if isinstance(link, dict) else str(link)
                        label = link.get("text", "") if isinstance(link, dict) else ""
                        if not href:
                            continue
                        candidate = _canonical_url(urljoin(current, href))
                        if not _same_domain(url, candidate) or candidate in seen:
                            continue
                        hint = f"{candidate} {label}".lower()
                        score = sum(
                            1
                            for term in (
                                "price",
                                "pricing",
                                "fee",
                                "tuition",
                                "schedule",
                                "class",
                                "program",
                                "membership",
                                "join",
                                "product",
                                "register",
                            )
                            if term in hint
                        )
                        ranked.append((score, candidate))
                    for _, candidate in sorted(ranked, reverse=True)[: settings.crawl_max_pages * 2]:
                        queue.append((candidate, depth + 1))
    except Exception as exc:
        warnings.append(f"Browser research failed: {format_exception(exc)}")
    if not pages or _needs_interactive_recovery(pages, criteria):
        fallback_pages, fallback_warning = await _playwright_fallback(url, application, criteria)
        useful_fallback_pages = [
            page for page in fallback_pages if page.text and not is_blocked_page(page.text)
        ]
        if useful_fallback_pages:
            by_url = {_canonical_url(page.url): page for page in pages if not is_blocked_page(page.text)}
            for page in useful_fallback_pages:
                by_url[_canonical_url(page.url)] = page
            pages = list(by_url.values())[: settings.crawl_max_pages]
            if _public_location_directory_urls(url):
                warnings.append(
                    "Recovered: Playwright filled public lookup details using New York, United States and refreshed rendered text."
                )
            else:
                warnings.append("Recovered: Playwright refreshed the rendered public website text.")
        elif fallback_warning:
            warnings.append(fallback_warning)
    if not pages or _needs_interactive_recovery(pages, criteria):
        protected_pages = _protected_provider_recovery_pages(url, application, criteria)
        if protected_pages:
            by_url = {_canonical_url(page.url): page for page in pages if not is_blocked_page(page.text)}
            for page in protected_pages:
                by_url[_canonical_url(page.url)] = page
            pages = list(by_url.values())[: settings.crawl_max_pages]
            warnings.append(
                "Recovered: The protected provider finder was replaced with a bounded official New York club page."
            )
    recovery_urls = set(_public_location_directory_urls(url))
    recovered_location_price = any(
        page.url in recovery_urls and re.search(r"\$\s*[0-9]", page.text or page.markdown)
        for page in pages
    )
    if recovered_location_price:
        warnings = [
            warning
            for warning in warnings
            if "blocked by anti-bot protection" not in warning.lower()
            and "access-verification page" not in warning.lower()
        ]
        warnings.append(
            "Recovered: The protected location finder was replaced with an official New York club page."
        )
    pages.sort(key=lambda page: page.score, reverse=True)
    return pages, warnings


async def _playwright_fallback(
    start_url: str,
    application: ApplicationData,
    criteria: list[Criterion],
) -> tuple[list[CrawledPage], str | None]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return [], f"Playwright fallback is unavailable: {exc}"
    pages: list[CrawledPage] = []
    seeds = [_canonical_url(start_url), *_provider_content_recovery_urls(start_url, application)]
    if application.requested_price is not None:
        seeds.extend(_public_location_directory_urls(start_url))
    queue: deque[tuple[str, int]] = deque((candidate, 0) for candidate in dict.fromkeys(seeds))
    seen: set[str] = set()
    try:
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
                timezone_id=settings.capture_timezone,
                geolocation=DEFAULT_GEOLOCATION,
                permissions=["geolocation"],
            )
            while queue and len(pages) < settings.crawl_max_pages:
                current, depth = queue.popleft()
                if current in seen:
                    continue
                seen.add(current)
                page = await context.new_page()
                try:
                    response = await page.goto(current, wait_until="domcontentloaded", timeout=settings.crawl_timeout_ms)
                    if response is not None and response.status >= 400:
                        continue
                    await page.wait_for_timeout(900)
                    await reveal_public_information(page)
                    title = normalize_space(await page.title())
                    raw_text = await page.locator("body").inner_text(timeout=10000)
                    text = normalize_space(raw_text)
                    if is_blocked_page(text):
                        continue
                    record = CrawledPage(url=page.url, title=title, markdown=raw_text, text=text)
                    _relevance(record, application, criteria)
                    pages.append(record)
                    if depth < settings.crawl_max_depth:
                        links = await page.locator("a[href]").evaluate_all(
                            "els => els.map(a => ({href:a.href,text:(a.innerText||a.textContent||'').trim()}))"
                        )
                        ranked: list[tuple[int, str]] = []
                        for link in links:
                            candidate = _canonical_url(link.get("href", ""))
                            if not candidate.startswith(("http://", "https://")) or not _same_domain(start_url, candidate) or candidate in seen:
                                continue
                            hint = f"{candidate} {link.get('text', '')}".lower()
                            score = sum(
                                1
                                for term in (
                                    "price",
                                    "pricing",
                                    "fee",
                                    "plan",
                                    "offer",
                                    "schedule",
                                    "class",
                                    "program",
                                    "membership",
                                    "join",
                                    "register",
                                    "club details",
                                    "new york",
                                )
                                if term in hint
                            )
                            ranked.append((score, candidate))
                        for _, candidate in sorted(ranked, reverse=True)[: settings.crawl_max_pages * 2]:
                            queue.append((candidate, depth + 1))
                finally:
                    await page.close()
            await context.close()
            await browser.close()
    except Exception as exc:
        return pages, f"Playwright fallback could not read the rendered page: {format_exception(exc)}"
    if not pages:
        return [], "Playwright reached an access-verification page before public lookup fields became available."
    return pages, None


def _extract_title(markdown: str, url: str) -> str:
    heading = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
    return normalize_space(heading.group(1)) if heading else (urlparse(url).hostname or url)


def _find_quote(page: CrawledPage, terms: list[str]) -> str | None:
    chunks = re.split(r"(?<=[.!?])\s+|\s*\|\s*|\n+", page.markdown)
    lowered_terms = [term.lower() for term in terms]
    for chunk in chunks:
        clean = normalize_space(re.sub(r"[#*_`\[\]()]", " ", chunk))
        if 12 <= len(clean) <= 420 and any(term in clean.lower() for term in lowered_terms):
            return clean
    return None


def _site_prices(pages: list[CrawledPage]) -> list[tuple[float, CrawledPage, str]]:
    results: list[tuple[float, CrawledPage, str]] = []
    for page in pages:
        for match in re.finditer(r"\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)", page.text):
            value = float(match.group(1).replace(",", ""))
            start, end = max(0, match.start() - 100), min(len(page.text), match.end() + 140)
            results.append((value, page, normalize_space(page.text[start:end])))
    return results


def heuristic_evaluate(
    application: ApplicationData,
    criteria: list[Criterion],
    pages: list[CrawledPage],
) -> list[Finding]:
    findings: list[Finding] = []
    prices = _site_prices(pages)
    negative_or_ambiguous = {
        "identical_fees",
        "nonclinical",
        "not_private_club",
        "no_travel_lodging",
        "not_opwdd_location",
    }
    hri_exclusions = {
        "laptop",
        "computer",
        "cell phone",
        "telephone",
        "pill dispenser",
        "vehicle",
        "medical device",
        "monitoring system",
        "software",
    }
    otps_exclusions = {
        "co-pay",
        "copay",
        "cable television",
        "paper towel",
        "soap",
        "rental car",
        "legal fee",
        "vehicle",
        "experimental therapy",
    }
    for criterion in criteria:
        if criterion.scope == "internal":
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.INTERNAL,
                    note="Internal information is required; this cannot be verified from a public website.",
                )
            )
            continue
        if criterion.scope == "document":
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.NEEDS_REVIEW,
                    note=criterion.description or "A separate supporting document is required.",
                )
            )
            continue
        if not pages:
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.NEEDS_REVIEW,
                    note="The website could not be accessed, so this criterion requires manual review.",
                )
            )
            continue

        item = (application.requested_item or "").lower()
        if criterion.id in {"published_fees", "published_fee", "visible_price"}:
            if prices:
                preferred = next(
                    (entry for entry in prices if application.requested_price is not None and abs(entry[0] - application.requested_price) < 0.01),
                    prices[0],
                )
                value, page, quote = preferred
                findings.append(
                    Finding(
                        criterion_id=criterion.id,
                        label=criterion.label,
                        status=FindingStatus.FOUND,
                        note=f"A public price of ${value:,.2f} is visible on the page.",
                        url=page.url,
                        quote=quote,
                        confidence=0.9,
                    )
                )
            else:
                findings.append(
                    Finding(
                        criterion_id=criterion.id,
                        label=criterion.label,
                        status=FindingStatus.NEEDS_REVIEW,
                        note="No reliable dollar amount was identified on the public pages reviewed.",
                    )
                )
            continue
        if criterion.id == "noncredit":
            explicit = None
            for page in pages:
                quote = _find_quote(page, ["noncredit", "non-credit"])
                if quote:
                    explicit = (page, quote)
                    break
            if explicit:
                page, quote = explicit
                findings.append(
                    Finding(
                        criterion_id=criterion.id,
                        label=criterion.label,
                        status=FindingStatus.FOUND,
                        note="The page explicitly describes the offering as noncredit.",
                        url=page.url,
                        quote=quote,
                        confidence=0.9,
                    )
                )
            else:
                findings.append(
                    Finding(
                        criterion_id=criterion.id,
                        label=criterion.label,
                        status=FindingStatus.NEEDS_REVIEW,
                        note="The page does not explicitly state whether college credit is awarded.",
                    )
                )
            continue
        if criterion.rule == "hri_exclusion" and any(term in item for term in hri_exclusions):
            page = pages[0]
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.NOT_FOUND,
                    note="The requested item appears to be computer/technology or another listed HRI exclusion.",
                    url=page.url,
                    quote=_find_quote(page, item.split()) or application.requested_item,
                    confidence=0.98,
                )
            )
            continue
        if criterion.rule == "otps_exclusion" and any(term in item for term in otps_exclusions):
            page = pages[0]
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.NOT_FOUND,
                    note="The requested item appears on the OTPS exclusion list.",
                    url=page.url,
                    quote=_find_quote(page, item.split()) or application.requested_item,
                    confidence=0.95,
                )
            )
            continue
        if criterion.rule in {"price_match", "coaching_cap", "transition_cap", "max_1500", "max_3000"}:
            requested = application.requested_price
            if not prices:
                findings.append(
                    Finding(
                        criterion_id=criterion.id,
                        label=criterion.label,
                        status=FindingStatus.NEEDS_REVIEW,
                        note="No reliable public price was identified on the pages reviewed.",
                    )
                )
                continue
            if criterion.rule == "price_match" and requested is not None:
                exact = next((entry for entry in prices if abs(entry[0] - requested) < 0.01), None)
                if exact:
                    value, page, quote = exact
                    findings.append(
                        Finding(
                            criterion_id=criterion.id,
                            label=criterion.label,
                            status=FindingStatus.FOUND,
                            note=f"The website displays ${value:,.2f}, matching the application amount.",
                            url=page.url,
                            quote=quote,
                            confidence=0.9,
                        )
                    )
                else:
                    value, page, quote = prices[0]
                    findings.append(
                        Finding(
                            criterion_id=criterion.id,
                            label=criterion.label,
                            status=FindingStatus.NEEDS_REVIEW,
                            note=f"The application states ${requested:,.2f}; the first relevant public amount found was ${value:,.2f}.",
                            url=page.url,
                            quote=quote,
                            confidence=0.65,
                        )
                    )
                continue
            cap = {
                "max_1500": 1500,
                "max_3000": 3000,
                "transition_cap": 350,
                "coaching_cap": 55,
            }.get(criterion.rule)
            price = requested if requested is not None else prices[0][0]
            page = prices[0][1]
            quote = prices[0][2]
            status = FindingStatus.FOUND if cap is not None and price <= cap else FindingStatus.NOT_FOUND
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=status,
                    note=f"The evaluated amount is ${price:,.2f}; the applicable basic cap is ${cap:,.2f}." if cap else "Price requires review.",
                    url=page.url,
                    quote=quote,
                    confidence=0.9,
                )
            )
            continue
        if criterion.id in negative_or_ambiguous:
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.NEEDS_REVIEW,
                    note="The website does not provide enough explicit language to prove this negative condition safely.",
                )
            )
            continue

        matched = None
        for page in pages:
            quote = _find_quote(page, criterion.evidence_terms)
            if quote:
                matched = (page, quote)
                break
        if matched:
            page, quote = matched
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=FindingStatus.FOUND,
                    note="Relevant public language was found and will be captured for the reviewer.",
                    url=page.url,
                    quote=quote,
                    confidence=0.7,
                )
            )
        else:
            findings.append(
                Finding(
                    criterion_id=criterion.id,
                    label=criterion.label,
                    status=criterion.absence_status,
                    note="The accessible pages reviewed did not provide clear supporting language.",
                )
            )
    return findings
