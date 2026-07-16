from __future__ import annotations

import re
from dataclasses import dataclass, field


DEFAULT_LOCATION = "New York, United States"
DEFAULT_CITY = "New York"
DEFAULT_STATE = "New York"
DEFAULT_STATE_CODE = "NY"
DEFAULT_POSTAL_CODE = "10001"
DEFAULT_COUNTRY = "United States"
DEFAULT_COUNTRY_CODE = "US"
DEFAULT_GEOLOCATION = {"latitude": 40.7128, "longitude": -74.0060, "accuracy": 50}


_BLOCKED_TERMS = (
    "access denied",
    "checking your browser",
    "cloudflare",
    "performing security verification",
    "security service to protect against malicious bots",
    "verify you are human",
)


@dataclass
class PublicInteractionResult:
    used_defaults: bool = False
    actions: list[str] = field(default_factory=list)


def is_blocked_page(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _BLOCKED_TERMS)


def field_default(metadata: str, input_type: str = "text") -> str | None:
    """Return only non-identifying defaults that can reveal public information."""
    lowered = re.sub(r"[^a-z0-9]+", " ", metadata.lower())
    unsafe = (
        "birth",
        "card",
        "company",
        "date of birth",
        "dob",
        "email",
        "first name",
        "full name",
        "last name",
        "member name",
        "password",
        "payment",
        "phone",
        "your name",
    )
    if input_type.lower() in {"email", "password", "tel"} or any(term in lowered for term in unsafe):
        return None
    if re.search(r"city state|search.*(?:city|location)|location|near(?:by| me)?|address|find (?:a )?(?:gym|club|store)", lowered):
        return DEFAULT_LOCATION
    if re.search(r"\b(zip|zipcode|postal|postcode)\b", lowered):
        return DEFAULT_POSTAL_CODE
    if re.search(r"\bcountry\b", lowered):
        return DEFAULT_COUNTRY
    if re.search(r"\bstate\b", lowered) and not re.search(r"city state|location|near|address", lowered):
        return DEFAULT_STATE
    if re.search(r"\bcity\b", lowered) and not re.search(r"city state|location|near|address", lowered):
        return DEFAULT_CITY
    return None


def safe_action_priority(text: str) -> int | None:
    """Rank public lookup/navigation controls; never authorize enrollment or purchase."""
    clean = re.sub(r"\s+", " ", text).strip().lower()
    if not clean or any(
        term in clean
        for term in (
            "buy",
            "checkout",
            "complete enrollment",
            "confirm purchase",
            "log in",
            "login",
            "pay",
            "place order",
            "purchase",
            "sign in",
            "submit application",
        )
    ):
        return None
    patterns = (
        (1, r"^(search|find(?: a)? (?:gym|club|location|store)|show locations|see locations|apply)$"),
        (2, r"^(use this location|select(?: this)? (?:gym|club|location)|choose(?: this)? (?:gym|club|location)|view (?:gym|club|location))$"),
        (3, r"^(view|see|explore|review) (?:pricing|prices|plans|memberships|membership options|offers)$"),
        (4, r"^(join|join now|get started)$"),
    )
    for priority, pattern in patterns:
        if re.search(pattern, clean):
            return priority
    return None


async def _visible_metadata(locator) -> str:
    return await locator.evaluate(
        """el => [
            el.name, el.id, el.type, el.placeholder, el.getAttribute('aria-label'),
            el.getAttribute('autocomplete'),
            el.labels ? Array.from(el.labels).map(label => label.innerText).join(' ') : ''
        ].filter(Boolean).join(' ')"""
    )


async def _select_default(select, metadata: str) -> bool:
    desired = field_default(metadata, "select")
    if not desired:
        return False
    choices = await select.locator("option").evaluate_all(
        "els => els.map(el => ({label:(el.textContent || '').trim(), value:el.value}))"
    )
    aliases = {
        DEFAULT_COUNTRY: (DEFAULT_COUNTRY.lower(), DEFAULT_COUNTRY_CODE.lower(), "usa", "u.s."),
        DEFAULT_STATE: (DEFAULT_STATE.lower(), DEFAULT_STATE_CODE.lower()),
        DEFAULT_CITY: (DEFAULT_CITY.lower(),),
        DEFAULT_POSTAL_CODE: (DEFAULT_POSTAL_CODE,),
        DEFAULT_LOCATION: ("new york", DEFAULT_POSTAL_CODE),
    }.get(desired, (desired.lower(),))
    for choice in choices:
        label = str(choice.get("label") or "").strip().lower()
        value = str(choice.get("value") or "").strip().lower()
        if any(alias == label or alias == value or alias in label for alias in aliases):
            await select.select_option(value=str(choice.get("value") or ""), timeout=3000)
            return True
    return False


async def _fill_public_defaults(page, result: PublicInteractionResult) -> None:
    selects = page.locator("select:visible")
    for index in range(min(await selects.count(), 20)):
        select = selects.nth(index)
        try:
            metadata = await _visible_metadata(select)
            if await _select_default(select, metadata):
                result.used_defaults = True
                result.actions.append("selected public location filter")
        except Exception:
            continue

    inputs = page.locator(
        "input:visible:not([type=hidden]):not([type=email]):not([type=password]):not([type=tel])"
    )
    for index in range(min(await inputs.count(), 30)):
        input_element = inputs.nth(index)
        try:
            input_type = (await input_element.get_attribute("type") or "text").lower()
            if input_type not in {"", "search", "text", "number"}:
                continue
            metadata = await _visible_metadata(input_element)
            value = field_default(metadata, input_type)
            if not value:
                continue
            await input_element.fill(value, timeout=3000)
            result.used_defaults = True
            result.actions.append(f"filled public location as {value}")
            await page.wait_for_timeout(350)
            options = page.locator("[role=option]:visible")
            if await options.count():
                preferred = options.filter(has_text=re.compile(r"New York", re.I)).first
                if await preferred.count():
                    await preferred.click(timeout=3000)
                else:
                    await input_element.press("ArrowDown")
                    await input_element.press("Enter")
                result.actions.append("selected New York suggestion")
        except Exception:
            continue


async def _click_public_action(page, priorities: set[int], result: PublicInteractionResult) -> bool:
    controls = page.locator("button:visible, a:visible, input[type=submit]:visible")
    ranked: list[tuple[int, int, str]] = []
    for index in range(min(await controls.count(), 80)):
        control = controls.nth(index)
        try:
            text = (await control.inner_text(timeout=1000)).strip()
            if not text:
                text = (await control.get_attribute("value") or "").strip()
            priority = safe_action_priority(text)
            if priority in priorities:
                ranked.append((priority or 99, index, text))
        except Exception:
            continue
    if not ranked:
        return False
    _, index, text = sorted(ranked)[0]
    try:
        await controls.nth(index).click(timeout=5000)
        result.actions.append(f"opened public information with {text}")
        await page.wait_for_timeout(1200)
        return True
    except Exception:
        return False


async def reveal_public_information(page) -> PublicInteractionResult:
    """Fill safe public lookup gates and navigate only as far as public prices/details."""
    result = PublicInteractionResult()
    try:
        body_text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return result
    if is_blocked_page(body_text):
        return result

    await _fill_public_defaults(page, result)
    if result.used_defaults:
        await _click_public_action(page, {1}, result)
    for priorities in ({2}, {3}, {4}):
        try:
            body_text = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""
        if re.search(r"\$\s*[0-9]", body_text):
            break
        if not await _click_public_action(page, priorities, result):
            continue
    return result
