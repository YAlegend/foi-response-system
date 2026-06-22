"""Responsible officers — the named person on the hook for each department.

Lets the UI show *who* is responsible by name (not just a team) and lets a
reminder be addressed to them. Built-in demo names ship by default so the demo
is populated out of the box; override in production with
FOI_RESPONSIBLE_OFFICERS ("Department=Name <email>; Department=Name <email>").
Unknown or unassigned cases fall back to the central FOI / Information
Governance officer (FOI_FOI_OFFICER_NAME / FOI_FOI_OFFICER_EMAIL).
"""
from __future__ import annotations

import re

from .config import get_settings

# department (normalised) -> (name, email). Matched case-insensitively and by
# substring, so a case tagged "Highways" still matches "Highways & Transport".
_DEMO_OFFICERS = {
    "highways & transport": ("Priya Shah", "priya.shah@oxfordshire.gov.uk"),
    "environment & climate": ("Tom Fielding", "tom.fielding@oxfordshire.gov.uk"),
    "children's services": ("Sarah Okafor", "sarah.okafor@oxfordshire.gov.uk"),
    "adult social care": ("David Mensah", "david.mensah@oxfordshire.gov.uk"),
}


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("’", "'")


def _parse(spec: str) -> dict:
    out: dict[str, tuple[str, str]] = {}
    for part in (spec or "").split(";"):
        if "=" not in part:
            continue
        dept, person = part.split("=", 1)
        m = re.match(r"\s*([^<]*?)\s*(?:<([^>]+)>)?\s*$", person)
        name = (m.group(1) or "").strip() if m else ""
        email = (m.group(2) or "").strip() if m else ""
        if dept.strip() and name:
            out[_norm(dept)] = (name, email)
    return out


def _directory() -> dict:
    custom = _parse(getattr(get_settings(), "responsible_officers", "") or "")
    return custom or _DEMO_OFFICERS


def officer_for(department: str) -> dict:
    """The named officer responsible for *department*. Falls back to the central
    FOI officer for an unknown or empty department (``central=True``)."""
    s = get_settings()
    d = (department or "").strip()
    if d:
        nd = _norm(d)
        for key, (name, email) in _directory().items():
            if nd == key or nd in key or key in nd:
                return {"name": name, "email": email, "department": d, "central": False}
    return {"name": s.foi_officer_name, "email": s.foi_officer_email,
            "department": "", "central": True}
