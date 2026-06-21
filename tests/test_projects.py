"""Scheme detection and case tagging."""
from app.ingestion.website_crawler import _scheme_for
from app.projects import detect_project, label, owning_department
from app.services import triage


def test_detect_prefers_most_specific_scheme():
    # An LTN item also mentions "traffic filters" — longest keyword wins.
    assert detect_project("Low Traffic Neighbourhood traffic filters in Cowley") == "ltn"
    assert detect_project("How many ANPR cameras enforce the traffic filters?") == "traffic-filters"
    assert detect_project("charges under the Zero Emission Zone pilot") == "zez"
    assert detect_project("How many potholes were reported last year?") == ""


def test_catalogue_lookups():
    assert label("zez") == "Zero Emission Zone"
    assert owning_department("zez") == "Environment & Climate"
    assert owning_department("traffic-filters") == "Highways & Transport"
    assert label("unknown-key") == "unknown-key"   # graceful fallback


def test_triage_tags_the_scheme():
    r = triage.classify("Oxford traffic filters", "How many ANPR cameras enforce the filters?")
    assert r.project == "traffic-filters"
    assert any("traffic-filters" in n for n in r.notes)
    # A general request carries no scheme tag.
    assert triage.classify("Council staff headcount", "How many staff?").project == ""


def test_crawler_tags_scheme_by_keyword_when_url_unseeded():
    # No seed match -> keyword detection from title/text still tags the page.
    assert _scheme_for("https://www.oxfordshire.gov.uk/x/charges-oxfords-zez",
                       "Charges for Oxford's Zero Emission Zone",
                       "daily charge details", []) == "zez"
    # A configured seed-URL prefix still takes precedence.
    seeds = [("traffic-filters", "https://www.oxfordshire.gov.uk/tf")]
    assert _scheme_for("https://www.oxfordshire.gov.uk/tf/cameras",
                       "Cameras", "body text", seeds) == "traffic-filters"
    # A general page is left untagged.
    assert _scheme_for("https://www.oxfordshire.gov.uk/libraries",
                       "Library opening hours", "books and computers", []) == ""
