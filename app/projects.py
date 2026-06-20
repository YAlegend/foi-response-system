"""Schemes / projects: detection and catalogue lookups.

One source of truth for "which scheme is this about?", shared by triage (tag a
case), ingestion (tag a crawled page or published response), and seeding. The
catalogue itself (key, label, owning department, keywords) lives in config so the
system stays council-agnostic.
"""
from __future__ import annotations

from .config import get_settings


def catalog() -> list[dict]:
    return get_settings().project_catalog


def detect_project(text: str) -> str:
    """Return the scheme key whose catalogue keyword best matches the text, or ""
    if none do. The *longest* matching keyword wins, so a Low Traffic
    Neighbourhood item — which also mentions "traffic filters" — is tagged ``ltn``
    rather than ``traffic-filters``."""
    t = (text or "").lower()
    best_key, best_len = "", 0
    for c in catalog():
        for kw in c.get("keywords", []):
            if kw in t and len(kw) > best_len:
                best_key, best_len = c["key"], len(kw)
    return best_key


def label(key: str) -> str:
    """Human label for a scheme key (the key itself if not in the catalogue)."""
    for c in catalog():
        if c["key"] == key:
            return c["label"]
    return key or ""


def owning_department(key: str) -> str:
    """The department that owns a scheme, or "" if unknown."""
    for c in catalog():
        if c["key"] == key:
            return c["department"]
    return ""
