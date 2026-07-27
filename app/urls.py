from __future__ import annotations

import re

URL_RE = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>\u00bb\u201d\u2019"


def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.findall(text):
        url = match.rstrip(_TRAILING)
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found
