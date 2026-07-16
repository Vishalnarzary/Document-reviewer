from pathlib import Path

import pytest

from app.extraction import deterministic_extract, extract_pdf_text


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("filename", "category", "participant", "item", "provider", "price"),
    [
        ("Sample-01---Community-Class-GallopNYC.pdf", "community_class", "Aaron M.", "Recreational Group Riding", "GallopNYC", 80.0),
        ("Sample-03---Coaching-92NY-Parenting.pdf", "coaching", "Chaim D.", "Parenting & Family Center", "92NY", 50.0),
        ("Sample-07---HRI-Laptop---exclusion-test.pdf", "hri", "Esther G.", "Laptop computer", "Amazon", None),
        ("Sample-09---Transition-Program-LaGuardia-CC.pdf", "transition_program", "Baruch Z.", "Adult & Continuing Education", "LaGuardia Community College", 300.0),
        ("Sample-10---Appeal-Gracie-Barra-Jiu-Jitsu.pdf", "appeal", "Yosef B.", "Adult Group Jiu Jitsu", "Gracie Barra", 30.0),
    ],
)
def test_sample_extraction(filename, category, participant, item, provider, price):
    text, pages, warnings = extract_pdf_text(ROOT / "samples" / filename)
    result = deterministic_extract(text, pages, warnings)
    assert result.category == category
    assert result.participant_name == participant
    assert result.requested_item == item
    assert result.provider_name == provider
    assert result.requested_price == price
    assert result.website_url.startswith("https://")


def test_all_sample_categories_are_supported():
    categories = set()
    for path in sorted((ROOT / "samples").glob("*.pdf")):
        text, pages, warnings = extract_pdf_text(path)
        categories.add(deterministic_extract(text, pages, warnings).category)
    assert categories == {"community_class", "coaching", "membership", "hri", "otps", "transition_program", "appeal"}

