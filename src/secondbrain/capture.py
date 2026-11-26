"""Explicit read capture: deterministic input interpretation, then shared material intake."""

from __future__ import annotations

import re

from secondbrain import ops

_URL = re.compile(r"https?://[^\s<>\"`“”‘’。，；！？】》）]+", re.IGNORECASE)


def parse_shared_url(value: str) -> str:
    """Find exactly one HTTP(S) URL, without interpreting prose as a title or command."""
    found = []
    for match in _URL.finditer(value):
        candidate = match[0].rstrip(".,;!'")
        # Markdown and ordinary prose may wrap a link in brackets. Do not
        # remove closing delimiters that are part of a balanced URL path.
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while candidate.endswith(closing) and candidate.count(closing) > candidate.count(
                opening
            ):
                candidate = candidate[:-1]
        if candidate not in found:
            found.append(candidate)
    if len(found) != 1:
        raise ValueError("请提供一个明确的链接；无链接的论文请切换到「论文名」")
    try:
        return ops.validate_manual(found, "read", None, False, None)[0]
    except ops.OpsError as exc:
        raise ValueError(str(exc)) from exc


def import_url_or_content(
    shared: str,
    echo,
    *,
    title: str | None = None,
    text: str | None = None,
    pdf: bytes | None = None,
    client=None,
) -> dict:
    """Save one read material; keep a reference when a remote document cannot be fetched.

    The caller manages model authorization and resumable task state separately.
    """
    result = ops.add_manual(
        [parse_shared_url(shared)] if shared.strip() else [],
        echo,
        state="read",
        title=title,
        text=text,
        pdf=pdf,
        client=client,
        fallback_to_reference=True,
    )
    result["capture_fallback"] = any(row["capture_fallback"] for row in result["results"])
    return result
