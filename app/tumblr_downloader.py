from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import mimetypes
import re
import shutil
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit

from .downloader import DownloadResult
from .linkedin_downloader import (
    DownloadCancelled,
    DownloadFailed,
    Downloader as LinkedInDownloader,
)
from .platforms import Platform
from .storage import finalize_files, media_files
from .threads_extractor import _parse_netscape_cookies

LOGGER = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
_MAX_HTML_BYTES = 32 * 1024 * 1024
_MAX_MEDIA_BYTES = 256 * 1024 * 1024
_MAX_MEDIA = 20
_POST_ID_RE = re.compile(r"(?<!\d)(\d{8,})(?!\d)")
_JSON_MARKERS = (
    '"content"',
    '"media"',
    '"photos"',
    '"original_size"',
    'media.tumblr.com',
)
_MEDIA_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/heic": ".heic",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
}
_POSITIVE_PATH_MARKERS = {
    "content": 24,
    "media": 18,
    "photos": 24,
    "photo": 18,
    "image": 18,
    "video": 18,
    "audio": 18,
    "post": 12,
    "trail": 8,
    "reblog": 8,
    "blocks": 10,
}
_NEGATIVE_PATH_MARKERS = {
    "avatar": -80,
    "profile": -70,
    "portrait": -60,
    "blog": -12,
    "header": -55,
    "logo": -60,
    "theme": -70,
    "icon": -45,
    "badge": -45,
}


class TumblrExtractionError(RuntimeError):
    pass


class TumblrNoMediaPost(TumblrExtractionError):
    pass


@dataclass(frozen=True, slots=True)
class TumblrMediaCandidate:
    kind: str
    url: str
    width: int = 0
    height: int = 0
    content_type: str = ""
    score: int = 0

    @property
    def pixels(self) -> int:
        return max(0, self.width) * max(0, self.height)


class _TumblrHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[tuple[str, str]] = []
        self.scripts: list[str] = []
        self.media_tags: list[tuple[str, str]] = []
        self._in_script = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        lower_tag = tag.lower()
        if lower_tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "")
            if key and content:
                self.meta.append((key, content))
        elif lower_tag == "script":
            self._in_script = True
            self._script_parts = []
        elif lower_tag == "img":
            if src := values.get("src") or values.get("data-src"):
                self.media_tags.append(("image", src))
        elif lower_tag in {"video", "source"}:
            if src := values.get("src"):
                source_type = values.get("type", "").lower()
                kind = "audio" if source_type.startswith("audio/") else "video"
                self.media_tags.append((kind, src))
        elif lower_tag == "audio":
            if src := values.get("src"):
                self.media_tags.append(("audio", src))

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            self.scripts.append("".join(self._script_parts))
            self._in_script = False
            self._script_parts = []


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _post_identifier(url: str) -> str:
    path = urlsplit(url).path
    if match := _POST_ID_RE.search(path):
        return match.group(1)
    if match := _POST_ID_RE.search(url):
        return match.group(1)
    raise TumblrExtractionError("Unsupported Tumblr post URL format")


def _allowed_media_url(value: str) -> bool:
    if not value.startswith(("https://", "http://")):
        return False
    host = (urlsplit(value).hostname or "").lower()
    if host == "a.tumblr.com" or host in {
        "vt.tumblr.com",
        "vtt.tumblr.com",
        "ve.tumblr.com",
        "va.tumblr.com",
    }:
        return True
    if host.endswith(".media.tumblr.com"):
        return True
    if host == "media.tumblr.com" or host.endswith(".tumblrusercontent.com"):
        return True
    return False


def _path_score(path: tuple[str, ...], node_type: str = "") -> int:
    tokens: set[str] = set()
    for item in path:
        tokens.update(re.findall(r"[a-z0-9]+", item.lower()))
    tokens.update(re.findall(r"[a-z0-9]+", node_type.lower()))
    score = sum(value for marker, value in _POSITIVE_PATH_MARKERS.items() if marker in tokens)
    score += sum(value for marker, value in _NEGATIVE_PATH_MARKERS.items() if marker in tokens)
    return score


def _kind_from_type(content_type: str, fallback: str = "") -> str:
    value = content_type.lower()
    if value.startswith("image/"):
        return "image"
    if value.startswith("video/"):
        return "video"
    if value.startswith("audio/"):
        return "audio"
    return fallback


def _candidate_from_media(
    media: Any,
    *,
    fallback_kind: str,
    score: int,
) -> TumblrMediaCandidate | None:
    if not isinstance(media, dict):
        return None
    url = media.get("url") or media.get("media_url") or media.get("src")
    if not isinstance(url, str) or not _allowed_media_url(url):
        return None
    content_type = str(media.get("type") or media.get("mime_type") or "")
    kind = _kind_from_type(content_type, fallback_kind)
    if kind not in {"image", "video", "audio"}:
        return None
    return TumblrMediaCandidate(
        kind=kind,
        url=html_lib.unescape(url).replace("\\/", "/"),
        width=_as_int(media.get("width")),
        height=_as_int(media.get("height")),
        content_type=content_type,
        score=score + (8 if media.get("has_original_dimensions") else 0),
    )


def _best_media_variant(
    values: Any,
    *,
    fallback_kind: str,
    score: int,
) -> TumblrMediaCandidate | None:
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        return None
    candidates = [
        candidate
        for value in values
        if (candidate := _candidate_from_media(
            value,
            fallback_kind=fallback_kind,
            score=score,
        ))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.pixels, item.score, item.url))


def _legacy_photo_candidate(photo: Any, score: int) -> TumblrMediaCandidate | None:
    if not isinstance(photo, dict):
        return None
    variants: list[Any] = []
    if isinstance(photo.get("original_size"), dict):
        variants.append(photo["original_size"])
    if isinstance(photo.get("alt_sizes"), list):
        variants.extend(photo["alt_sizes"])
    if isinstance(photo.get("url"), str):
        variants.append(photo)
    return _best_media_variant(variants, fallback_kind="image", score=score)


def _append_candidate(
    items: list[TumblrMediaCandidate],
    candidate: TumblrMediaCandidate | None,
) -> None:
    if candidate is None or len(items) >= _MAX_MEDIA:
        return
    parsed = urlsplit(candidate.url)
    identity = (candidate.kind, parsed.netloc.lower(), unquote(parsed.path))
    for index, existing in enumerate(items):
        existing_parsed = urlsplit(existing.url)
        existing_identity = (
            existing.kind,
            existing_parsed.netloc.lower(),
            unquote(existing_parsed.path),
        )
        if existing_identity == identity:
            if (candidate.score, candidate.pixels) > (existing.score, existing.pixels):
                items[index] = candidate
            return
    items.append(candidate)


def _parse_nested_json(value: str) -> Any | None:
    text = html_lib.unescape(value).replace("\\/", "/").strip()
    if not text or text[0] not in "[{" or not any(marker in text for marker in _JSON_MARKERS):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _collect_media(
    node: Any,
    items: list[TumblrMediaCandidate],
    *,
    path: tuple[str, ...] = (),
    depth: int = 0,
) -> None:
    if depth > 80 or len(items) >= _MAX_MEDIA:
        return
    if isinstance(node, dict):
        node_type = str(node.get("type") or node.get("$type") or "")
        lower_type = node_type.lower()
        score = _path_score(path, node_type)

        if lower_type in {"image", "video", "audio"}:
            media = node.get("media") or node.get("media_objects")
            candidate = _best_media_variant(
                media,
                fallback_kind=lower_type,
                score=score + 30,
            )
            _append_candidate(items, candidate)
            # Posters and thumbnails inside a media block are not separate post media.
            if candidate:
                return

        photos = node.get("photos")
        if isinstance(photos, list):
            for photo in photos:
                _append_candidate(items, _legacy_photo_candidate(photo, score + 24))

        if isinstance(node.get("original_size"), dict):
            _append_candidate(items, _legacy_photo_candidate(node, score + 18))

        content_type = str(node.get("mime_type") or node.get("media_type") or "")
        direct_kind = _kind_from_type(content_type)
        if direct_kind and score >= 0:
            _append_candidate(
                items,
                _candidate_from_media(node, fallback_kind=direct_kind, score=score),
            )

        for key, value in node.items():
            _collect_media(
                value,
                items,
                path=path + (str(key),),
                depth=depth + 1,
            )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _collect_media(
                value,
                items,
                path=path + (str(index),),
                depth=depth + 1,
            )
    elif isinstance(node, str):
        if nested := _parse_nested_json(node):
            _collect_media(nested, items, path=path, depth=depth + 1)


def _node_matches_post(node: dict[str, Any], post_id: str) -> bool:
    for key in ("id", "id_string", "post_id", "postId"):
        if str(node.get(key) or "") == post_id:
            return True
    for key in ("post_url", "permalink_url", "url", "href"):
        value = node.get(key)
        if isinstance(value, str) and post_id in urlsplit(value).path:
            return True
    return False


def _find_post_nodes(
    node: Any,
    post_id: str,
    *,
    depth: int = 0,
) -> list[dict[str, Any]]:
    if depth > 80:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if _node_matches_post(node, post_id):
            return [node]
        for value in node.values():
            found.extend(_find_post_nodes(value, post_id, depth=depth + 1))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_post_nodes(value, post_id, depth=depth + 1))
    elif isinstance(node, str) and post_id in node:
        if nested := _parse_nested_json(node):
            found.extend(_find_post_nodes(nested, post_id, depth=depth + 1))
    return found


def _json_values(script: str) -> Iterator[Any]:
    text = html_lib.unescape(script).strip()
    if not text:
        return
    for prefix in ("for (;;);", "<!--"):
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip()
    if text.endswith("-->"):
        text = text[:-3].rstrip()
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    starts = [match.start() for match in re.finditer(r"[\[{]", text)]
    for start in starts[:300]:
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        yield value


def _regex_media(webpage: str) -> list[TumblrMediaCandidate]:
    decoded = html_lib.unescape(webpage).replace("\\/", "/").replace("\\u0026", "&")
    pattern = re.compile(
        r'https?://(?:[a-z0-9-]+\.)?(?:media\.)?tumblr\.com/[^\s"\'<>\\]+'
        r'|https?://[a-z0-9.-]+\.tumblrusercontent\.com/[^\s"\'<>\\]+',
        re.IGNORECASE,
    )
    result: list[TumblrMediaCandidate] = []
    for match in pattern.finditer(decoded):
        url = match.group(0).rstrip("),.;]")
        if not _allowed_media_url(url):
            continue
        suffix = Path(urlsplit(url).path).suffix.lower()
        kind = "video" if suffix in {".mp4", ".webm", ".mov"} else "audio" if suffix in {".mp3", ".m4a", ".aac", ".ogg"} else "image"
        _append_candidate(result, TumblrMediaCandidate(kind, url, score=1))
        if len(result) >= _MAX_MEDIA:
            break
    return result


def extract_tumblr_media(webpage: str, post_id: str) -> list[TumblrMediaCandidate]:
    parser = _TumblrHTMLParser()
    try:
        parser.feed(webpage)
    except Exception:
        LOGGER.debug("Unable to parse Tumblr HTML", exc_info=True)

    items: list[TumblrMediaCandidate] = []
    for script in parser.scripts:
        if post_id not in script or not any(marker in script for marker in _JSON_MARKERS):
            continue
        for value in _json_values(script):
            targets = _find_post_nodes(value, post_id)
            if targets:
                for target in targets:
                    _collect_media(target, items, path=("post",))
            else:
                _collect_media(value, items, path=("post",))

    if items:
        return sorted(items, key=lambda item: (item.score, item.pixels), reverse=True)

    # Open Graph only represents the primary asset, but is a useful fallback for
    # pages whose application JSON changes.
    for key, content in parser.meta:
        if key in {"og:video", "og:video:url", "og:video:secure_url"} and _allowed_media_url(content):
            _append_candidate(items, TumblrMediaCandidate("video", content, score=4))
        elif key in {"og:image", "og:image:url", "og:image:secure_url"} and _allowed_media_url(content):
            _append_candidate(items, TumblrMediaCandidate("image", content, score=3))
        elif key in {"og:audio", "og:audio:url", "og:audio:secure_url"} and _allowed_media_url(content):
            _append_candidate(items, TumblrMediaCandidate("audio", content, score=4))

    if items:
        return items

    for kind, url in parser.media_tags:
        if _allowed_media_url(url):
            _append_candidate(items, TumblrMediaCandidate(kind, url, score=2))
    if items:
        return items

    return _regex_media(webpage)


def _suffix_for_response(url: str, content_type: str, kind: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in _MEDIA_CONTENT_TYPES:
        return _MEDIA_CONTENT_TYPES[media_type]
    suffix = Path(urlsplit(url).path).suffix.lower()
    allowed = {
        ".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".heic",
        ".mp4", ".webm", ".mov", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav",
    }
    if suffix in allowed:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(media_type) if media_type else None
    if guessed:
        return guessed
    return {"video": ".mp4", "audio": ".m4a"}.get(kind, ".jpg")


class TumblrMediaExtractor:
    def __init__(self, cookie_path: Path | None = None) -> None:
        self.cookie_path = cookie_path
        self._session = None

    def _new_session(self):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # pragma: no cover - installed in Docker image
            raise TumblrExtractionError("curl-cffi is not installed") from exc

        session = curl_requests.Session()
        session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )
        for name, value, domain, cookie_path in _parse_netscape_cookies(self.cookie_path):
            try:
                session.cookies.set(name, value, domain=domain, path=cookie_path)
            except Exception:
                LOGGER.debug("Unable to load Tumblr cookie %s for %s", name, domain)
        return session

    @property
    def session(self):
        if self._session is None:
            self._session = self._new_session()
        return self._session

    def _get_page(self, url: str) -> str:
        try:
            response = self.session.get(
                url,
                headers={"Referer": "https://www.tumblr.com/"},
                impersonate="chrome",
                allow_redirects=True,
                timeout=45,
            )
        except Exception as exc:
            raise TumblrExtractionError(
                f"Tumblr page request failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise TumblrExtractionError(f"Tumblr page returned HTTP {response.status_code}")
        if len(response.content) > _MAX_HTML_BYTES:
            raise TumblrExtractionError("Tumblr page is unexpectedly large")
        return response.text

    def _download_media(
        self,
        candidate: TumblrMediaCandidate,
        page_url: str,
        destination: Path,
        post_id: str,
        index: int,
    ) -> Path:
        try:
            response = self.session.get(
                candidate.url,
                headers={"Referer": page_url},
                impersonate="chrome",
                allow_redirects=True,
                timeout=90,
            )
        except Exception as exc:
            raise TumblrExtractionError(
                f"Tumblr media #{index} request failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise TumblrExtractionError(
                f"Tumblr media #{index} returned HTTP {response.status_code}"
            )
        content_type = str(response.headers.get("content-type") or "")
        actual_kind = _kind_from_type(content_type, candidate.kind)
        if actual_kind not in {"image", "video", "audio"}:
            raise TumblrExtractionError(
                f"Tumblr media #{index} returned {content_type or 'unknown content type'}"
            )
        content = response.content
        if not content:
            raise TumblrExtractionError(f"Tumblr media #{index} is empty")
        if len(content) > _MAX_MEDIA_BYTES:
            raise TumblrExtractionError(f"Tumblr media #{index} is too large")

        suffix = _suffix_for_response(candidate.url, content_type, actual_kind)
        target = destination / f"tumblr_{post_id}_{index:02d}{suffix}"
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_bytes(content)
        temp.replace(target)
        return target

    def extract_and_download(self, url: str, destination: Path) -> list[Path]:
        post_id = _post_identifier(url)
        webpage = self._get_page(url)
        candidates = extract_tumblr_media(webpage, post_id)
        if not candidates:
            raise TumblrNoMediaPost("Tumblr post contains no extractable media")

        destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        failures: list[str] = []
        for index, candidate in enumerate(candidates, 1):
            try:
                downloaded.append(
                    self._download_media(candidate, url, destination, post_id, index)
                )
            except TumblrExtractionError as exc:
                failures.append(str(exc))
                LOGGER.warning("Tumblr media candidate #%s failed: %s", index, exc)
        if downloaded:
            return downloaded
        raise TumblrExtractionError(
            "; ".join(failures[:3]) or "Tumblr media download produced no files"
        )


class Downloader(LinkedInDownloader):
    async def download(
        self,
        *,
        job: dict,
        platform: Platform,
        cancel_check,
    ) -> DownloadResult:
        if platform.key != "tumblr":
            return await super().download(
                job=job,
                platform=platform,
                cancel_check=cancel_check,
            )

        job_id = int(job["id"])
        work_dir = self.settings.work_root / f"job-{job_id}"
        source_url = str(job.get("normalized_url") or job["url"]).strip()
        cookie = self._cookie_file(platform)
        native_dir = work_dir / "tumblr-native"

        if work_dir.exists():
            shutil.rmtree(work_dir)
        native_dir.mkdir(parents=True, exist_ok=True)

        native_error: Exception | None = None
        try:
            if await cancel_check():
                raise DownloadCancelled("Cancelled before Tumblr native extraction")
            extractor = TumblrMediaExtractor(cookie)
            await asyncio.to_thread(
                extractor.extract_and_download,
                source_url,
                native_dir,
            )
            if await cancel_check():
                raise DownloadCancelled("Cancelled after Tumblr native extraction")
            if not media_files(native_dir):
                raise TumblrExtractionError("Tumblr native extractor produced no media files")
            stored = finalize_files(
                work_dir=native_dir,
                download_root=self.settings.download_root,
                platform_folder=platform.folder,
                created_at=job["created_at"],
            )
            return DownloadResult(engine="tumblr-native", stored=stored)
        except DownloadCancelled:
            raise
        except TumblrNoMediaPost as exc:
            native_error = exc
            LOGGER.info("job=%s is not a Tumblr media post", job_id)
        except Exception as exc:  # noqa: BLE001 - generic downloaders remain fallback
            native_error = exc
            LOGGER.warning("job=%s Tumblr native extraction failed: %s", job_id, exc)

        shutil.rmtree(native_dir, ignore_errors=True)
        fallback_job = dict(job)
        fallback_job["url"] = source_url
        try:
            return await super().download(
                job=fallback_job,
                platform=platform,
                cancel_check=cancel_check,
            )
        except DownloadFailed as fallback_exc:
            if native_error is None:
                raise
            raise DownloadFailed(
                f"tumblr-native: {type(native_error).__name__}: {native_error} | {fallback_exc}",
                fallback_exc.report_path,
            ) from fallback_exc


__all__ = [
    "DownloadCancelled",
    "DownloadFailed",
    "Downloader",
    "TumblrExtractionError",
    "TumblrMediaCandidate",
    "TumblrMediaExtractor",
    "extract_tumblr_media",
]
