from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class Platform:
    key: str
    folder: str
    engines: tuple[str, ...]
    cookie_names: tuple[str, ...]


PLATFORMS: dict[str, Platform] = {
    "youtube": Platform("youtube", "YouTube", ("yt-dlp",), ("youtube.txt",)),
    "instagram": Platform(
        "instagram", "Instagram", ("gallery-dl", "yt-dlp"), ("instagram.txt",)
    ),
    "twitter": Platform(
        "twitter", "Twitter", ("gallery-dl", "yt-dlp"), ("twitter.txt", "x.txt")
    ),
    "tiktok": Platform("tiktok", "TikTok", ("gallery-dl", "yt-dlp"), ("tiktok.txt",)),
    "facebook": Platform(
        "facebook", "Facebook", ("gallery-dl", "yt-dlp"), ("facebook.txt",)
    ),
    "threads": Platform(
        "threads", "Threads", ("yt-dlp", "gallery-dl"), ("threads.txt", "instagram.txt")
    ),
    "linkedin": Platform(
        "linkedin", "LinkedIn", ("yt-dlp", "gallery-dl"), ("linkedin.txt",)
    ),
    "other": Platform("other", "Other", ("yt-dlp", "gallery-dl"), ()),
}

_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "si",
    "spm",
    "ref",
    "ref_src",
    "s",
}


def detect_platform(url: str) -> Platform:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host in {"youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"} or host.endswith(
        ".youtube.com"
    ):
        return PLATFORMS["youtube"]
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return PLATFORMS["instagram"]
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "fxtwitter.com", "vxtwitter.com"}:
        return PLATFORMS["twitter"]
    if host == "tiktok.com" or host.endswith(".tiktok.com") or host == "vm.tiktok.com":
        return PLATFORMS["tiktok"]
    if host in {"facebook.com", "fb.watch", "m.facebook.com"} or host.endswith(".facebook.com"):
        return PLATFORMS["facebook"]
    if host == "threads.net" or host.endswith(".threads.net"):
        return PLATFORMS["threads"]
    if host == "linkedin.com" or host.endswith(".linkedin.com") or host == "lnkd.in":
        return PLATFORMS["linkedin"]
    return PLATFORMS["other"]


def normalize_url(url: str) -> str:
    """Normalize tracking noise while preserving parameters that may identify media."""
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower() or "https"
    host = (parsed.hostname or "").lower()
    if not host:
        return url.strip()

    # Normalize legacy Twitter URLs, but keep alternate frontends untouched.
    if host in {"www.twitter.com", "mobile.twitter.com", "twitter.com"}:
        host = "x.com"
    elif host.startswith("www."):
        host = host[4:]

    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    filtered_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in _TRACKING_KEYS:
            continue
        filtered_query.append((key, value))

    return urlunsplit((scheme, netloc, path, urlencode(filtered_query, doseq=True), ""))
