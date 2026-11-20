"""Evidence-bearing bibliographic facts shared by acquisition and local identity matching."""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime


def normalized_title(value: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def _identifier(value: str, kind: str) -> str:
    value = str(value or "").strip().casefold()
    if kind == "doi":
        return re.sub(r"^(?:https?://(?:dx\.)?doi.org/|doi:\s*)", "", value)
    return re.sub(r"v\d+$", "", value.removeprefix("https://arxiv.org/abs/"))


def split_authors(value: str) -> list[str]:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[\u200b-\u200d\ufeff]", "", value)
    value = re.sub(r"^\s*and\s+", "", value)
    parts = re.split(r"\s{3,}|\s*;\s*|,\s*(?:and\s+)?|\s+and\s+|\s*&\s*", value)
    names = []
    for part in parts:
        name = re.sub(r"^[\s\d*†‡∗⋆★♠♣♥♦+]+|[\s\d*†‡∗⋆★♠♣♥♦+]+$", "", part)
        if name:
            names.append(name)
    return names


def _authors(values) -> set[str]:
    if isinstance(values, str):
        values = split_authors(values)
    return {normalized_title(v) for v in values or [] if normalized_title(v)}


def _compatible_identified_authors(left: set[str], right: set[str]) -> bool:
    # Only a shared public identifier licenses provider initials/middle-name
    # omissions. Still require the same complete author roster and unique keys.
    def keys(names):
        return {(parts[-1], parts[0][0]) for name in names if (parts := name.split())}
    return len(left) == len(right) == len(keys(left)) and keys(left) == keys(right)


def compare_bibliography(left: dict, right: dict) -> dict:
    """No title-only merges; explicit conflicts always beat a matching identifier."""
    conflicts, evidence = [], []
    for kind in ("doi", "arxiv_id"):
        a, b = _identifier(left.get(kind), kind), _identifier(right.get(kind), kind)
        if a and b:
            if a != b:
                conflicts.append(kind)
            else:
                evidence.append(kind)
    a, b = _authors(left.get("authors")), _authors(right.get("authors"))
    identified = bool({"doi", "arxiv_id"} & set(evidence))
    if a and b and a != b and not (
        identified and _compatible_identified_authors(a, b)
    ):
        conflicts.append("authors")
    elif a and b:
        evidence.append("authors")
    if left.get("year") and right.get("year"):
        if str(left["year"]) != str(right["year"]):
            conflicts.append("year")
        else:
            evidence.append("year")
    title = normalized_title(left.get("title") or "")
    if title and title == normalized_title(right.get("title") or ""):
        evidence.append("title")
    venue = normalized_title(left.get("venue") or "")
    if venue and venue == normalized_title(right.get("venue") or ""):
        evidence.append("venue")
    strong = bool({"doi", "arxiv_id"} & set(evidence))
    # Initials-only and single-word author names are insufficient for a weak identity.
    named = bool(a) and all(
        len(parts := name.split()) >= 2 and len(parts[0]) > 1 and len(parts[-1]) > 1
        for name in a
    )
    weak = (all(k in evidence for k in ("title", "authors")) and named and a == b
            and bool({"year", "venue"} & set(evidence))
            and bool(left.get("evidence")) and bool(right.get("evidence")))
    return {"match": not conflicts and (strong or weak),
            "conflicts": conflicts, "evidence": evidence}


def apply_bibliography(entry, bibliography: dict) -> None:
    """Retain explicit display titles; improve URL placeholders and missing fields."""
    if not bibliography:
        return
    identity = {"doi": getattr(entry, "doi", None),
                "arxiv_id": getattr(entry, "arxiv_id", None)}
    identity_conflicts = compare_bibliography(identity, bibliography)["conflicts"]
    if identity_conflicts:
        raise ValueError("材料身份冲突：" + "、".join(identity_conflicts))
    previous = getattr(entry, "bibliography", {}) or {}
    comparison = compare_bibliography(previous, bibliography)
    if comparison["conflicts"]:
        raise ValueError("材料书目冲突：" + "、".join(comparison["conflicts"]))
    facts = {**previous, **{k: v for k, v in bibliography.items() if v not in (None, "", [])}}
    facts["evidence"] = list(dict.fromkeys([*previous.get("evidence", []),
                                           *bibliography.get("evidence", [])]))
    entry.bibliography = facts
    if facts.get("title") and (not entry.title or entry.title == entry.url
                               or entry.title.startswith(("http://", "https://", "urn:"))):
        entry.title = facts["title"]
    entry.author = entry.author or ", ".join(facts.get("authors", [])) or None
    if facts.get("year") and (
        not entry.published_at or (previous.get("preprint_year") and not previous.get("year"))
    ):
        entry.published_at = datetime(int(facts["year"]), 1, 1, tzinfo=UTC)
    entry.doi = entry.doi or facts.get("doi")
    entry.arxiv_id = entry.arxiv_id or facts.get("arxiv_id")


def entry_bibliography(entry, *, evidence: str) -> dict:
    """Convert an explicitly academic structured record, not arbitrary web metadata."""
    year = entry.published_at.year if entry.published_at else None
    preprint = evidence == "arxiv:api"
    return {"title": entry.title, "authors": split_authors(entry.author or ""),
            "year": None if preprint else year, "preprint_year": year if preprint else None,
            "doi": entry.doi, "arxiv_id": entry.arxiv_id, "kind": "paper",
            "evidence": [evidence], "verified": True}
