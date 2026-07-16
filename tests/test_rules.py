from pathlib import Path

from app.checklists import load_checklists
from app.browser_interaction import field_default, is_blocked_page, safe_action_priority
from app.evidence import (
    enforce_evidence_gate,
    evidence_locator_candidates,
    materialize_recovered_text_evidence,
    stamp_image,
)
from app.groq_client import (
    GroqAdapter,
    _analysis_cache_key,
    _deduplicated_analysis_pages,
    _public_analysis_context,
    _text_chunks,
)
from app.models import (
    ApplicationData,
    CrawledPage,
    Criterion,
    EvidenceRecord,
    Finding,
    FindingStatus,
    VisionCapture,
)
from app.research import heuristic_evaluate
from app.research import _is_unusable_page, _link_relevance, _same_domain
from app.utils import format_exception, safe_public_url
from app.vision import (
    _application_context_tokens,
    _vision_url_order,
    materialize_vision_evidence,
    select_vision_fallback_criteria,
)
from app.workflow import (
    _derive_exact_price_matches,
    _derive_explicit_public_access,
    _derive_explicit_requested_price,
    _negative_claim_has_explicit_text,
    _quote_is_scaled_or_contextual_funding,
    _visual_negative_claim_is_explicit,
)


def test_all_seven_checklists_are_configured():
    checklists = load_checklists()
    assert set(checklists) == {"community_class", "coaching", "membership", "hri", "otps", "transition_program", "appeal"}
    assert all(item["criteria"] for item in checklists.values())


def test_public_lookup_defaults_use_new_york_without_identity_fields():
    assert field_default("Search by city, state, or ZIP") == "New York, United States"
    assert field_default("Postal code") == "10001"
    assert field_default("State") == "New York"
    assert field_default("Country") == "United States"
    assert field_default("Email address", "email") is None
    assert field_default("First name") is None
    assert field_default("Phone number", "tel") is None


def test_public_lookup_actions_stop_before_transaction():
    assert safe_action_priority("Find a club") == 1
    assert safe_action_priority("Select this location") == 2
    assert safe_action_priority("View pricing") == 3
    assert safe_action_priority("Join now") == 4
    assert safe_action_priority("Complete enrollment") is None
    assert safe_action_priority("Pay now") is None
    assert safe_action_priority("Place order") is None


def test_access_verification_is_not_treated_as_recovered_public_content():
    assert is_blocked_page("Performing security verification with Cloudflare") is True
    assert is_blocked_page("Choose a location to see membership pricing") is False


def test_research_navigation_is_same_site_without_guessed_provider_routes():
    assert _same_domain("https://example.org/start", "https://www.example.org/pricing")
    assert _same_domain("https://example.org/start", "https://members.example.org/plans")
    assert not _same_domain("https://example.org/start", "https://unrelated.example/pricing")


def test_link_ranking_uses_current_request_and_checklist_terms():
    application = ApplicationData(requested_item="Individual pottery studio membership", category="custom")
    criteria = [
        Criterion(
            id="published_cost",
            label="Cost is published",
            scope="public_web",
            evidence_terms=["annual price", "studio access"],
        )
    ]
    relevant = _link_relevance("/support/studio-membership Individual studio plans", application, criteria)
    unrelated = _link_relevance("/about Board and staff", application, criteria)
    assert relevant > unrelated


def test_generic_error_pages_are_excluded_from_research():
    assert _is_unusable_page(
        CrawledPage(url="https://example.org/find", title="Server error", text="500 Server error")
    )
    assert _is_unusable_page(
        CrawledPage(url="https://example.org", title="Home", text="Checking your browser with Cloudflare")
    )
    assert not _is_unusable_page(
        CrawledPage(url="https://example.org/locations/downtown", title="Downtown", text="Classic plan $19 per month")
    )


def test_compound_discovery_keeps_only_official_https_pages():
    from types import SimpleNamespace
    import asyncio

    class DiscoveryStub(GroqAdapter):
        async def _create_completion(self, request):
            self.request = request
            content = (
                '{"urls":["https://www.example.org/locations/downtown",'
                '"https://offers.example.org/pricing",'
                '"https://evil.example.net/fake",'
                '"http://example.org/insecure"]}'
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    adapter = DiscoveryStub.__new__(DiscoveryStub)
    adapter.enabled = True
    adapter._client = object()
    adapter.discovery_model = "groq/compound-mini"
    adapter.last_error = None
    adapter.last_discovery_error = None
    result = asyncio.run(
        adapter.discover_official_pages(
            ApplicationData(provider_name="Example", requested_item="Classic membership"),
            "https://www.example.org/find",
        )
    )
    assert result == [
        "https://www.example.org/locations/downtown",
        "https://offers.example.org/pricing",
    ]
    assert adapter.request["search_settings"]["include_domains"] == ["example.org"]
    assert adapter.request["model"] == "groq/compound-mini"


def test_recovered_text_evidence_satisfies_audit_gate(tmp_path, monkeypatch):
    monkeypatch.setattr("app.evidence.ROOT_DIR", tmp_path)
    review_dir = tmp_path / "output" / "reviews" / "review-1"
    page = CrawledPage(
        url="https://www.planetfitness.com/gyms/manhattan-herald-square-ny",
        title="Manhattan (Herald Square), NY | Planet Fitness",
        markdown="Classic\n$19 /mo\nplus taxes & fees\nUnlimited access to your home club.",
        text="Classic $19 /mo plus taxes & fees Unlimited access to your home club.",
    )
    finding = Finding(
        criterion_id="published_fee",
        label="Membership fee is published",
        status=FindingStatus.FOUND,
        note="A public monthly price is visible.",
        url=page.url,
        quote="$19 /mo",
        source="groq-verified",
    )
    records = materialize_recovered_text_evidence("review-1", review_dir, [finding], [page])
    enforce_evidence_gate([finding], records)
    assert finding.status == FindingStatus.FOUND
    assert any(record.kind == "targeted" and record.criterion_id == "published_fee" for record in records)


def test_hri_laptop_is_flagged_as_excluded():
    criteria = load_checklists()["hri"]["criteria"]
    findings = heuristic_evaluate(
        ApplicationData(category="hri", requested_item="Laptop computer", website_url="https://example.org/laptop"),
        criteria,
        [CrawledPage(url="https://example.org/laptop", title="Laptop", text="Laptop computer product page")],
    )
    exclusion = next(item for item in findings if item.criterion_id == "exclusion_check")
    assert exclusion.status == FindingStatus.NOT_FOUND


def test_found_requires_targeted_evidence():
    findings = [Finding(criterion_id="price", label="Published price", status=FindingStatus.FOUND, note="Price found")]
    enforce_evidence_gate(findings, [])
    assert findings[0].status == FindingStatus.NEEDS_REVIEW


def test_found_survives_with_targeted_evidence():
    findings = [Finding(criterion_id="price", label="Published price", status=FindingStatus.FOUND, note="Price found")]
    evidence = [EvidenceRecord(id="EV-1", criterion_id="price", kind="targeted", url="https://example.org", captured_at="2026-01-01T00:00:00Z", raw_path="raw.png", stamped_path="stamp.png")]
    enforce_evidence_gate(findings, evidence)
    assert findings[0].status == FindingStatus.FOUND


def test_private_urls_are_rejected():
    assert safe_public_url("http://127.0.0.1/private")[0] is False
    assert safe_public_url("http://192.168.1.1/private")[0] is False
    assert safe_public_url("https://example.org/public")[0] is True


def test_empty_exception_messages_still_produce_useful_diagnostics():
    assert format_exception(TimeoutError()) == "TimeoutError"


def test_timestamp_overlay_expands_image(tmp_path):
    from datetime import datetime, timezone
    from PIL import Image

    source = tmp_path / "source.png"
    target = tmp_path / "stamped.png"
    Image.new("RGB", (800, 500), "white").save(source)
    stamp_image(source, target, "https://example.org/fees", "Evidence: published fee", "abc123", datetime.now(timezone.utc))
    with Image.open(source) as original, Image.open(target) as stamped:
        assert stamped.width >= max(original.width, 900)
        assert stamped.height > original.height


def test_long_markdown_quote_prefers_concise_price_locator():
    candidates = evidence_locator_candidates(
        "ve invites Previews and viewing hours Discounts As listed above $80 * [Join or renew](https://example.org/join)"
    )
    assert candidates[0] == "$80"
    assert all("](" not in candidate for candidate in candidates)


def test_scaled_scholarship_amount_is_not_a_program_fee():
    quote = "Each year, we award students over $2 million in Foundation Scholarships"
    assert _quote_is_scaled_or_contextual_funding(quote) is True
    assert _quote_is_scaled_or_contextual_funding("Registration fee: $80 per class") is False


def test_validated_public_fee_is_reused_for_exact_application_match():
    published = Criterion(
        id="published_fees",
        label="Published fees are visible",
        scope="public_web",
    )
    fee_match = Criterion(
        id="fee_match",
        label="Published fee matches the application",
        scope="public_web",
        rule="price_match",
    )
    findings = {
        "published_fees": Finding(
            criterion_id="published_fees",
            label=published.label,
            status=FindingStatus.FOUND,
            note="Direct price",
            url="https://example.org/class",
            quote="30-Minute Group - $80",
            source="groq",
            confidence=0.97,
        ),
        "fee_match": Finding(
            criterion_id="fee_match",
            label=fee_match.label,
            status=FindingStatus.NEEDS_REVIEW,
            note="No evidence found",
            source="groq",
        ),
    }
    _derive_exact_price_matches(
        ApplicationData(requested_price=80),
        {published.id: published, fee_match.id: fee_match},
        findings,
    )
    assert findings["fee_match"].status == FindingStatus.FOUND
    assert findings["fee_match"].quote == "30-Minute Group - $80"
    assert findings["fee_match"].source == "groq-derived"

    scaled = {
        "published_fees": Finding(
            criterion_id="published_fees",
            label=published.label,
            status=FindingStatus.FOUND,
            note="Funding",
            url="https://example.org",
            quote="$2 million in scholarships",
            source="groq",
        )
    }
    _derive_exact_price_matches(
        ApplicationData(requested_price=2),
        {published.id: published, fee_match.id: fee_match},
        scaled,
    )
    assert "fee_match" not in scaled


def test_explicit_public_access_survives_model_omission():
    criterion = Criterion(
        id="open_to_public",
        label="Open to the broader public",
        scope="public_web",
    )
    findings = {
        criterion.id: Finding(
            criterion_id=criterion.id,
            label=criterion.label,
            status=FindingStatus.NEEDS_REVIEW,
            note="Model omitted evidence",
            source="groq",
        )
    }
    _derive_explicit_public_access(
        {criterion.id: criterion},
        [
            CrawledPage(
                url="https://example.org/riding",
                markdown=(
                    "However, we offer a limited number of riding lessons to the public. "
                    "Our lessons are available for children and adults."
                ),
            )
        ],
        findings,
    )
    assert findings[criterion.id].status == FindingStatus.FOUND
    assert "riding lessons to the public" in findings[criterion.id].quote
    assert findings[criterion.id].source == "groq-verified"


def test_explicit_requested_price_survives_model_omission_without_scaling_error():
    published = Criterion(
        id="published_fees",
        label="Published fees are visible",
        scope="public_web",
    )
    fee_match = Criterion(
        id="fee_match",
        label="Published fee matches the application",
        scope="public_web",
        rule="price_match",
    )
    findings = {}
    _derive_explicit_requested_price(
        ApplicationData(
            requested_item="Recreational Group Riding",
            subject_area="Recreational Horseback Riding",
            requested_price=80,
        ),
        {published.id: published, fee_match.id: fee_match},
        [
            CrawledPage(
                url="https://example.org/riding",
                markdown="Pricing\n\nGroup Classes\n\n30-Minute Group - $80",
            )
        ],
        findings,
    )
    assert findings["published_fees"].status == FindingStatus.FOUND
    assert findings["fee_match"].status == FindingStatus.FOUND
    assert findings["published_fees"].quote == "30-Minute Group - $80"

    scaled = {}
    _derive_explicit_requested_price(
        ApplicationData(requested_item="English class", requested_price=2),
        {published.id: published, fee_match.id: fee_match},
        [
            CrawledPage(
                url="https://example.org",
                markdown="English class scholarships\n\n$2 million in scholarships awarded",
            )
        ],
        scaled,
    )
    assert scaled == {}


def test_requested_offering_price_replaces_unrelated_membership_price():
    published = Criterion(
        id="published_fee",
        label="Membership fee is published",
        scope="public_web",
    )
    fee_match = Criterion(
        id="fee_match",
        label="Published membership fee matches the application",
        scope="public_web",
        rule="price_match",
    )
    findings = {
        "published_fee": Finding(
            criterion_id="published_fee",
            label=published.label,
            status=FindingStatus.FOUND,
            note="Fee explicitly listed.",
            url="https://example.org/support/university-membership",
            quote="Monthly e-newsletter Price $7,500",
            source="groq",
        ),
        "fee_match": Finding(
            criterion_id="fee_match",
            label=fee_match.label,
            status=FindingStatus.NEEDS_REVIEW,
            note="Fee $7,500 does not match requested $80.00.",
            url="https://example.org/support/university-membership",
            quote="Monthly e-newsletter Price $7,500",
            source="groq",
        ),
    }
    _derive_explicit_requested_price(
        ApplicationData(requested_item="Individual Membership", requested_price=80),
        {published.id: published, fee_match.id: fee_match},
        [
            CrawledPage(
                url="https://example.org/support/membership",
                title="Become a Member",
                markdown=(
                    "Membership levels\nIndividual\nFree admission\nIncludes ticketed exhibitions\n"
                    "1 Member\nGuests\nEarly access\nExclusive invites\nPreviews\nDiscounts\n"
                    "Reciprocal benefits\nAs listed above\n$80\nJoin or renew"
                ),
            ),
            CrawledPage(
                url="https://example.org/support/university-membership",
                title="University Membership",
                markdown="Monthly e-newsletter\nPrice\n$7,500\nUniversity Membership",
            ),
        ],
        findings,
    )

    assert findings["published_fee"].status == FindingStatus.FOUND
    assert findings["published_fee"].url == "https://example.org/support/membership"
    assert findings["published_fee"].quote == "$80"
    assert findings["fee_match"].status == FindingStatus.FOUND
    assert findings["fee_match"].quote == "$80"


def test_discount_amount_is_not_promoted_as_requested_fee():
    published = Criterion(
        id="published_fee",
        label="Membership fee is published",
        scope="public_web",
    )
    findings = {}
    _derive_explicit_requested_price(
        ApplicationData(requested_item="Individual Membership", requested_price=10),
        {published.id: published},
        [
            CrawledPage(
                url="https://example.org/support/membership",
                title="Membership",
                markdown="Adults can save $10 on annual Individual Memberships.\nIndividual\nPrice\n$80",
            )
        ],
        findings,
    )
    assert findings == {}


def test_vision_cannot_infer_negative_claims_from_absence():
    assert _visual_negative_claim_is_explicit("identical_fees", "No separate fee is shown") is False
    assert _visual_negative_claim_is_explicit("noncredit", "Recreational Riding") is False
    assert _visual_negative_claim_is_explicit("nonclinical", "Recreational Riding") is False
    assert _visual_negative_claim_is_explicit("not_private_club", "Only $19 a month!") is False
    assert _visual_negative_claim_is_explicit("published_fees", "30-Minute Group - $80") is True


def test_negative_text_findings_require_explicit_language():
    assert _negative_claim_has_explicit_text("identical_fees", "Riding lessons are open to the public") is False
    assert _negative_claim_has_explicit_text("identical_fees", "The same fee applies to all participants") is True
    assert _negative_claim_has_explicit_text("noncredit", "A semester-based horsemanship program") is False
    assert _negative_claim_has_explicit_text("noncredit", "This is a non-credit recreational class") is True
    assert _negative_claim_has_explicit_text("nonclinical", "Horse education programs") is False
    assert _negative_claim_has_explicit_text("nonclinical", "This class is not therapy") is True
    assert _negative_claim_has_explicit_text("not_private_club", "Only $19 a month!") is False
    assert _negative_claim_has_explicit_text("not_private_club", "The program is available to all members, of all fitness levels") is True


def test_text_chunks_include_beginning_middle_and_end():
    text = "BEGIN " + ("word " * 5500) + " MIDDLE " + ("more " * 5500) + " END"
    chunks = _text_chunks(text, size=12000, overlap=400)
    combined = " ".join(chunks)
    assert len(chunks) > 2
    assert "BEGIN" in combined
    assert "MIDDLE" in combined
    assert "END" in combined


def test_public_groq_context_excludes_personal_form_fields():
    context = _public_analysis_context(
        ApplicationData(
            participant_name="Private Participant",
            fi_coordinator="Private Coordinator",
            broker_name="Private Broker",
            requested_item="English class",
            provider_name="Example College",
            requested_price=300,
        )
    )
    assert context["requested_item"] == "English class"
    assert "participant_name" not in context
    assert "fi_coordinator" not in context
    assert "broker_name" not in context


def test_exact_repeated_web_blocks_are_analyzed_once():
    repeated = "This identical navigation and provider boilerplate appears on every public page."
    pages = [
        CrawledPage(url="https://example.org/a", markdown=f"Unique first page text\n\n{repeated}", text=""),
        CrawledPage(url="https://example.org/b", markdown=f"Unique second page text\n\n{repeated}", text=""),
    ]
    deduplicated = _deduplicated_analysis_pages(pages)
    assert len(deduplicated) == 2
    assert sum(page.text.count(repeated) for page in deduplicated) == 1
    assert "Unique first page text" in deduplicated[0].text
    assert "Unique second page text" in deduplicated[1].text


def test_analysis_cache_key_changes_with_page_content():
    first = [CrawledPage(url="https://example.org", text="Fee: $80")]
    second = [CrawledPage(url="https://example.org", text="Fee: $90")]
    key_a = _analysis_cache_key("groq/compound", "{}", "[]", first)
    key_b = _analysis_cache_key("groq/compound", "{}", "[]", second)
    assert key_a != key_b


def test_compound_uses_json_mode_without_external_tools():
    from types import SimpleNamespace

    class FakeCompletions:
        def __init__(self):
            self.request = None

        async def create(self, **kwargs):
            self.request = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
            )

    import asyncio

    completions = FakeCompletions()
    adapter = GroqAdapter.__new__(GroqAdapter)
    adapter.enabled = True
    adapter.model = "groq/compound"
    adapter.last_error = None
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    result = asyncio.run(adapter._structured("system", "user", {"type": "object"}, "test"))
    assert result == {"ok": True}
    assert completions.request["response_format"] == {"type": "json_object"}
    assert completions.request["tool_choice"] == "none"


def test_gpt_oss_uses_strict_json_schema():
    from types import SimpleNamespace
    import asyncio

    class FakeCompletions:
        def __init__(self):
            self.request = None

        async def create(self, **kwargs):
            self.request = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
            )

    completions = FakeCompletions()
    adapter = GroqAdapter.__new__(GroqAdapter)
    adapter.enabled = True
    adapter.model = "openai/gpt-oss-20b"
    adapter.last_error = None
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    result = asyncio.run(adapter._structured("system", "user", schema, "test"))
    assert result == {"ok": True}
    assert completions.request["model"] == "openai/gpt-oss-20b"
    assert completions.request["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "test", "strict": True, "schema": schema},
    }
    assert "tool_choice" not in completions.request


def test_gpt_oss_recovers_valid_json_rejected_for_missing_optional_field():
    from types import SimpleNamespace
    import asyncio

    class SchemaError(Exception):
        status_code = 400

        def __init__(self):
            super().__init__("generated JSON omitted confidence")
            self.body = str(
                {
                    "error": {
                        "failed_generation": (
                            '{"findings":[{"criterion_id":"published_fee",'
                            '"label":"Membership fee is published","status":"Found",'
                            '"note":"Individual is listed at $80.",'
                            '"url":"https://www.brooklynmuseum.org/support/membership",'
                            '"quote":"$80"}]}'
                        )
                    }
                }
            )

    class FakeCompletions:
        async def create(self, **kwargs):
            raise SchemaError()

    adapter = GroqAdapter.__new__(GroqAdapter)
    adapter.enabled = True
    adapter.model = "openai/gpt-oss-20b"
    adapter.last_error = None
    adapter._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    result = asyncio.run(
        adapter._structured(
            "system",
            "user",
            {"type": "object", "properties": {}, "required": []},
            "test",
        )
    )
    assert result["findings"][0]["quote"] == "$80"
    assert adapter.last_error is None


def test_rate_limit_retries_every_ten_seconds_up_to_six_times(monkeypatch):
    from types import SimpleNamespace
    import asyncio

    class RateLimitError(Exception):
        status_code = 429

    class AlwaysRateLimitedCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            raise RateLimitError("rate limited")

    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("app.groq_client.asyncio.sleep", fake_sleep)
    completions = AlwaysRateLimitedCompletions()
    adapter = GroqAdapter.__new__(GroqAdapter)
    adapter.enabled = True
    adapter.model = "openai/gpt-oss-20b"
    adapter.last_error = None
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    result = asyncio.run(
        adapter._structured(
            "system",
            "user",
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "test",
        )
    )
    assert result is None
    assert completions.calls == 7
    assert delays == [10] * 6


def test_scout_vision_uses_bounded_image_input_and_validates_capture(tmp_path):
    from types import SimpleNamespace
    import asyncio
    from PIL import Image

    class FakeCompletions:
        def __init__(self):
            self.request = None

        async def create(self, **kwargs):
            self.request = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"findings":[{"criterion_id":"published_fees",'
                                '"status":"Found","note":"The image shows the class fee.",'
                                '"image_id":"VIS-01","visual_evidence":"English class fee: $200 per course",'
                                '"confidence":0.96}]}'
                            )
                        )
                    )
                ]
            )

    image_path = tmp_path / "page.jpg"
    Image.new("RGB", (640, 480), "white").save(image_path, format="JPEG")
    completions = FakeCompletions()
    adapter = GroqAdapter.__new__(GroqAdapter)
    adapter.enabled = True
    adapter.model = "openai/gpt-oss-20b"
    adapter.vision_model = "meta-llama/llama-4-scout-17b-16e-instruct"
    adapter.last_error = None
    adapter.last_vision_error = None
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    result = asyncio.run(
        adapter.evaluate_images(
            ApplicationData(requested_item="English class", requested_price=200),
            [Criterion(id="published_fees", label="Program fees are published", scope="public_web")],
            [
                VisionCapture(
                    id="VIS-01",
                    url="https://example.org/class",
                    title="Class",
                    path=str(image_path),
                )
            ],
        )
    )
    assert result and result[0].status == FindingStatus.FOUND
    assert result[0].source == "groq-vision"
    assert result[0].visual_capture_id == "VIS-01"
    assert completions.request["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
    image_parts = [
        part
        for part in completions.request["messages"][1]["content"]
        if part["type"] == "image_url"
    ]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    blocked_result = asyncio.run(
        adapter.evaluate_images(
            ApplicationData(requested_item="English class", requested_price=200),
            [Criterion(id="published_fees", label="Program fees are published", scope="public_web")],
            [
                VisionCapture(
                    id="VIS-01",
                    url="https://example.org/challenge",
                    title="Verify you are human",
                    path=str(image_path),
                    blocked=True,
                )
            ],
        )
    )
    assert blocked_result and blocked_result[0].status == FindingStatus.NEEDS_REVIEW


def test_vision_fallback_is_limited_to_antibot_or_unresolved_price():
    price = Criterion(id="published_fees", label="Program fees are published", scope="public_web")
    nonprice = Criterion(id="noncredit", label="Offering is noncredit", scope="public_web")
    selected, reasons = select_vision_fallback_criteria(
        [price, nonprice],
        {
            "published_fees": Finding(
                criterion_id="published_fees",
                label=price.label,
                status=FindingStatus.NEEDS_REVIEW,
                note="No text price",
            ),
            "noncredit": Finding(
                criterion_id="noncredit",
                label=nonprice.label,
                status=FindingStatus.FOUND,
                note="Found in text",
                url="https://example.org",
                quote="Noncredit",
            ),
        },
        [CrawledPage(url="https://example.org", text="Class information")],
        [],
    )
    assert [criterion.id for criterion in selected] == ["published_fees"]
    assert reasons == ["unresolved_price"]

    selected, reasons = select_vision_fallback_criteria(
        [price, nonprice],
        {},
        [CrawledPage(url="https://example.org", text="Verify you are human")],
        [],
    )
    assert {criterion.id for criterion in selected} == {"published_fees", "noncredit"}
    assert "crawler_or_antibot" in reasons


def test_vision_capture_prefers_ranked_crawled_pages_and_deduplicates_start_url():
    pages = [
        CrawledPage(url="https://example.org/old-form", score=0.2),
        CrawledPage(url="https://example.org/pricing/", score=4.2),
        CrawledPage(url="https://example.org/details", score=1.5),
    ]
    assert _vision_url_order("https://example.org/pricing", pages) == [
        "https://example.org/pricing/",
        "https://example.org/details",
        "https://example.org/old-form",
    ]


def test_vision_context_tokens_are_generic_and_request_specific():
    application = ApplicationData(
        requested_item="Individual pottery studio membership",
        category="Community recreation",
        subject_area="Ceramics",
    )
    tokens = _application_context_tokens(application)
    assert "individual" in tokens
    assert "pottery" in tokens
    assert "studio" in tokens
    assert "ceramics" in tokens
    assert "membership" not in tokens


def test_visual_found_survives_only_with_preserved_targeted_capture(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr("app.vision.ROOT_DIR", tmp_path)
    review_dir = tmp_path / "output" / "reviews" / "review-1"
    source = review_dir / "evidence" / "raw" / "vision-01.jpg"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (900, 650), "white").save(source, format="JPEG")
    finding = Finding(
        criterion_id="published_fees",
        label="Program fees are published",
        status=FindingStatus.FOUND,
        note="The visual price is direct.",
        url="https://example.org/class",
        quote="English class fee: $200 per course",
        source="groq-vision",
        visual_capture_id="VIS-01",
    )
    records = materialize_vision_evidence(
        "review-1",
        review_dir,
        [finding],
        [
            VisionCapture(
                id="VIS-01",
                url="https://example.org/class",
                title="Class",
                path=str(source),
            )
        ],
    )
    enforce_evidence_gate([finding], records)
    assert finding.status == FindingStatus.FOUND
    assert any(record.kind == "targeted" and record.criterion_id == "published_fees" for record in records)


def test_groq_evaluation_sends_only_relevant_snippets():
    class StubGroq(GroqAdapter):
        def __init__(self):
            self.enabled = True
            self._client = object()
            self.model = "test-model"
            self.cache_enabled = False
            self.calls = []

        async def _structured(self, system, user, schema, name):
            self.calls.append((name, user))
            if name == "website_observations":
                return {
                    "observations": [
                        {
                            "criterion_id": "published_fees",
                            "url": "https://example.org/class/",
                            "quote": "The English class registration fee is $200 per course.",
                            "analysis": "The amount is directly tied to the requested class.",
                        }
                    ]
                }
            return {
                "findings": [
                    {
                        "criterion_id": "published_fees",
                        "label": "Program fees are published",
                        "status": "Found",
                        "note": "A direct program fee was established.",
                        "url": "https://example.org/class/",
                        "quote": "The English class registration fee is $200 per course.",
                        "confidence": 0.98,
                    }
                ]
            }

    import asyncio

    page_text = "\n\n".join(
        [
            "REMOTE_IRRELEVANT_A: campus landscaping history and parking lot maintenance.",
            "The continuing education catalog lists many available subject areas.",
            "The English class registration fee is $200 per course.",
            "Registration is completed online before the first class session.",
            "REMOTE_IRRELEVANT_B: alumni athletics results and cafeteria renovations.",
        ]
    )
    adapter = StubGroq()
    result = asyncio.run(
        adapter.evaluate(
            ApplicationData(requested_item="English class", requested_price=200),
            [Criterion(id="published_fees", label="Program fees are published", scope="public_web")],
            [CrawledPage(url="https://example.org/class", title="Class", markdown=page_text, text=page_text)],
        )
    )
    scan_calls = [user for name, user in adapter.calls if name == "website_observations"]
    assert len(scan_calls) == 1
    assert "registration fee is $200" in scan_calls[0]
    assert "REMOTE_IRRELEVANT_A" not in scan_calls[0]
    assert "REMOTE_IRRELEVANT_B" not in scan_calls[0]
    assert adapter.calls[-1][0] == "website_findings"
    assert "VALIDATED SOURCE PASSAGES" in adapter.calls[-1][1]
    assert '"url": "https://example.org/class"' in adapter.calls[-1][1]
    assert result and result[0].source == "groq"
    assert result[0].status == FindingStatus.FOUND
    assert result[0].url == "https://example.org/class"


def test_all_inconclusive_groq_findings_are_not_cached(monkeypatch):
    import asyncio

    saved = []
    monkeypatch.setattr("app.groq_client._load_analysis_cache", lambda *args: None)
    monkeypatch.setattr("app.groq_client._save_analysis_cache", lambda *args: saved.append(args))

    class StubGroq(GroqAdapter):
        def __init__(self):
            self.enabled = True
            self._client = object()
            self.model = "test-model"
            self.cache_enabled = True
            self.last_error = None

        async def _structured(self, system, user, schema, name):
            if name == "website_observations":
                return {"observations": []}
            return {
                "findings": [
                    {
                        "criterion_id": "published_fees",
                        "label": "Program fees are published",
                        "status": "Needs Review",
                        "note": "No validated observation found.",
                        "url": None,
                        "quote": None,
                        "confidence": None,
                    }
                ]
            }

    adapter = StubGroq()
    result = asyncio.run(
        adapter.evaluate(
            ApplicationData(requested_item="English class", requested_price=200),
            [Criterion(id="published_fees", label="Program fees are published", scope="public_web")],
            [
                CrawledPage(
                    url="https://example.org/class",
                    title="Class",
                    text="The English class registration fee is $200 per course.",
                )
            ],
        )
    )
    assert result and result[0].status == FindingStatus.NEEDS_REVIEW
    assert saved == []
