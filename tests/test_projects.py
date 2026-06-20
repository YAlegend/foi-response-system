"""Scheme detection and case tagging."""
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
