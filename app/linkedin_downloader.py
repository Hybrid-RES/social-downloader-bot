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

from .downloader import DownloadFailed, DownloadResult
from .platforms import Platform
from .storage import finalize_files, media_files
from .threads_downloader import (
    DownloadCancelled,
    Downloader as ThreadsDownloader,
)
from .threads_extractor import _parse_netscape_cookies

LOGGER = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
_MAX_HTML_BYTES = 32 * 1024 * 1024
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_IMAGES = 20
_CODE_BLOCK_RE = re.compile(
    r"<code\b[^>]*>(?P<body>.*?)</code>", re.IGNORECASE | re.DOTALL
)
_POST_ID_RE = re.compile(r"(?:activity-|ugcPost-|share-)(\d{8,})", re.IGNORECASE)
_IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/heic": ".heic",
}
_ALLOWED_IMAGE_HOST_SUFFIXES = ("licdn.com", "linkedin.com")
_POSITIVE_PATH_MARKERS = {
    "content": 14,
    "image": 12,
    "images": 14,
    "multiimage": 18,
    "media": 12,
    "carousel": 16,
    "feed": 4,
    "update": 5,
    "document": 8,
    "article": 5,
}
_NEGATIVE_PATH_MARKERS = {
    "actor": -35,
    "author": -30,
    "profile": -40,
    "avatar": -50,
    "logo": -35,
    "company": -20,
    "thumbnail": -12,
    "badge": -25,
}


class LinkedInImageExtractionError(RuntimeError):
    pass


class LinkedInNoImagePost(LinkedInImageExtractionError):
    pass


@dataclass(frozen=True, slots=True)
class LinkedInImageCandidate:
    url: str
    width: int = 0
    height: int = 0
    score: int = 0

    @property
    def pixels(self) -> int:
        return max(0, self.width) * max(0, self.height)


class _LinkedInMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        key = (values.get("property") or values.get("name") or "").lower()
        content = values.get("content", "")
        if key and content:
            self.meta.append((key, content))


def _iter_code_payloads(webpage: str) -> Iterator[Any]:
    for match in _CODE_BLOCK_RE.finditer(webpage):
        raw = html_lib.unescape(match.group("body")).strip()
        if not raw:
            continue
        if raw.startswith("<!--") and raw.endswith("-->"):
            raw = raw[4:-3].strip()
        try:
            yield json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _path_score(path: tuple[str, ...], node_type: str = "") -> int:
    score = 0
    tokens: set[str] = set()
    for item in path:
        tokens.update(re.findall(r"[a-z0-9]+", item.lower()))
    tokens.update(re.findall(r"[a-z0-9]+", node_type.lower()))
    for marker, value in _POSITIVE_PATH_MARKERS.items():
        if marker in tokens:
            score += value
    for marker, value in _NEGATIVE_PATH_MARKERS.items():
        if marker in tokens:
            score += value
    if "imagecomponent" in node_type.lower() or "multiimage" in node_type.lower():
        score += 35
    if "profilephoto" in node_type.lower() or "companylogo" in node_type.lower():
        score -= 70
    return score


def _is_allowed_image_url(value: str) -> bool:
    if not value.startswith(("https://", "http://")):
        return False
    host = (urlsplit(value).hostname or "").lower()
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_IMAGE_HOST_SUFFIXES):
        return False
    path = unquote(urlsplit(value).path).lower()
    return "/image/" in path or "/dms/image/" in path or "feedshare" in path


def _vector_image_candidate(
    node: dict[str, Any],
    path: tuple[str, ...],
) -> LinkedInImageCandidate | None:
    root = node.get("rootUrl")
    artifacts = node.get("artifacts")
    if not isinstance(root, str) or not isinstance(artifacts, list):
        return None

    choices: list[tuple[int, int, int, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        segment = artifact.get("fileIdentifyingUrlPathSegment")
        if not isinstance(segment, str) or not segment:
            continue
        width = _as_int(artifact.get("width"))
        height = _as_int(artifact.get("height"))
        url = f"{root}{segment}"
        if not _is_allowed_image_url(url):
            continue
        choices.append((width * height, width + height, len(url), url))
    if not choices:
        return None

    _, _, _, best_url = max(choices)
    best_artifact = next(
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and f"{root}{artifact.get('fileIdentifyingUrlPathSegment') or ''}" == best_url
    )
    node_type = str(node.get("$type") or node.get("type") or "")
    return LinkedInImageCandidate(
        url=best_url,
        width=_as_int(best_artifact.get("width")),
        height=_as_int(best_artifact.get("height")),
        score=_path_score(path, node_type),
    )


def _contains_video_metadata(node: Any, *, depth: int = 0) -> bool:
    if depth > 80:
        return False
    if isinstance(node, dict):
        node_type = str(node.get("$type") or node.get("type") or "").lower()
        if (
            node.get("progressiveStreams")
            or node.get("adaptiveStreams")
            or "videoplaymetadata" in node_type
            or "videocomponent" in node_type
        ):
            return True
        return any(_contains_video_metadata(value, depth=depth + 1) for value in node.values())
    if isinstance(node, list):
        return any(_contains_video_metadata(value, depth=depth + 1) for value in node)
    return False


def _collect_image_candidates(
    node: Any,
    candidates: list[LinkedInImageCandidate],
    *,
    path: tuple[str, ...] = (),
    depth: int = 0,
) -> None:
    if depth > 80 or len(candidates) >= 200:
        return
    if isinstance(node, dict):
        if candidate := _vector_image_candidate(node, path):
            if candidate.score >= 0:
                candidates.append(candidate)
        node_type = str(node.get("$type") or node.get("type") or "")
        current_score = _path_score(path, node_type)
        for key, value in node.items():
            if isinstance(value, str) and _is_allowed_image_url(value) and current_score >= 0:
                candidates.append(
                    LinkedInImageCandidate(url=value, score=current_score)
                )
            else:
                _collect_image_candidates(
                    value,
                    candidates,
                    path=path + (str(key),),
                    depth=depth + 1,
                )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _collect_image_candidates(
                value,
                candidates,
                path=path + (str(index),),
                depth=depth + 1,
            )


def _deduplicate_candidates(
    candidates: list[LinkedInImageCandidate],
) -> list[LinkedInImageCandidate]:
    best: dict[tuple[str, str], LinkedInImageCandidate] = {}
    for candidate in candidates:
        parsed = urlsplit(candidate.url)
        identity = (parsed.netloc.lower(), unquote(parsed.path))
        existing = best.get(identity)
        if existing is None or (candidate.score, candidate.pixels) > (
            existing.score,
            existing.pixels,
        ):
            best[identity] = candidate
    ordered = sorted(
        best.values(),
        key=lambda item: (item.score, item.pixels, item.url),
        reverse=True,
    )
    return ordered[:_MAX_IMAGES]


def extract_linkedin_images(webpage: str) -> list[LinkedInImageCandidate]:
    payloads = list(_iter_code_payloads(webpage))
    if any(_contains_video_metadata(payload) for payload in payloads):
        return []

    candidates: list[LinkedInImageCandidate] = []
    for payload in payloads:
        _collect_image_candidates(payload, candidates)

    selected = _deduplicate_candidates(candidates)
    if selected:
        return selected

    parser = _LinkedInMetaParser()
    try:
        parser.feed(webpage)
    except Exception:
        LOGGER.debug("Unable to parse LinkedIn meta tags", exc_info=True)
    for key, content in parser.meta:
        if key in {"og:image", "og:image:url", "og:image:secure_url"} and _is_allowed_image_url(content):
            return [LinkedInImageCandidate(content, score=1)]
    return []


def _post_identifier(url: str) -> str:
    match = _POST_ID_RE.search(url)
    if match:
        return match.group(1)
    path = [part for part in urlsplit(url).path.split("/") if part]
    return re.sub(r"[^A-Za-z0-9_-]+", "_", path[-1] if path else "post")[:80]


def _suffix_for_response(url: str, content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in _IMAGE_CONTENT_TYPES:
        return _IMAGE_CONTENT_TYPES[media_type]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".heic"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(media_type) if media_type else None
    return guessed if guessed in _IMAGE_CONTENT_TYPES.values() else ".jpg"


class LinkedInImageExtractor:
    def __init__(self, cookie_path: Path | None = None) -> None:
        self.cookie_path = cookie_path
        self._session = None

    def _new_session(self):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # pragma: no cover - installed in Docker image
            raise LinkedInImageExtractionError("curl-cffi is not installed") from exc

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
                LOGGER.debug("Unable to load LinkedIn cookie %s for %s", name, domain)
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
                headers={"Referer": "https://www.linkedin.com/feed/"},
                impersonate="chrome",
                allow_redirects=True,
                timeout=45,
            )
        except Exception as exc:
            raise LinkedInImageExtractionError(
                f"LinkedIn page request failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise LinkedInImageExtractionError(
                f"LinkedIn page returned HTTP {response.status_code}"
            )
        if len(response.content) > _MAX_HTML_BYTES:
            raise LinkedInImageExtractionError("LinkedIn page is unexpectedly large")
        return response.text

    def _download_image(
        self,
        candidate: LinkedInImageCandidate,
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
                timeout=60,
            )
        except Exception as exc:
            raise LinkedInImageExtractionError(
                f"LinkedIn image #{index} request failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise LinkedInImageExtractionError(
                f"LinkedIn image #{index} returned HTTP {response.status_code}"
            )
        content_type = str(response.headers.get("content-type") or "")
        if not content_type.lower().startswith("image/"):
            raise LinkedInImageExtractionError(
                f"LinkedIn image #{index} returned {content_type or 'unknown content type'}"
            )
        content = response.content
        if not content:
            raise LinkedInImageExtractionError(f"LinkedIn image #{index} is empty")
        if len(content) > _MAX_IMAGE_BYTES:
            raise LinkedInImageExtractionError(f"LinkedIn image #{index} is too large")

        suffix = _suffix_for_response(candidate.url, content_type)
        target = destination / f"linkedin_{post_id}_{index:02d}{suffix}"
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_bytes(content)
        temp.replace(target)
        return target

    def extract_and_download(self, url: str, destination: Path) -> list[Path]:
        webpage = self._get_page(url)
        candidates = extract_linkedin_images(webpage)
        if not candidates:
            raise LinkedInNoImagePost("LinkedIn post contains no extractable images")

        destination.mkdir(parents=True, exist_ok=True)
        post_id = _post_identifier(url)
        downloaded: list[Path] = []
        failures: list[str] = []
        for index, candidate in enumerate(candidates, 1):
            try:
                downloaded.append(
                    self._download_image(candidate, url, destination, post_id, index)
                )
            except LinkedInImageExtractionError as exc:
                failures.append(str(exc))
                LOGGER.warning("LinkedIn image candidate #%s failed: %s", index, exc)
        if downloaded:
            return downloaded
        raise LinkedInImageExtractionError(
            "; ".join(failures[:3]) or "LinkedIn image download produced no files"
        )


class Downloader(ThreadsDownloader):
    async def download(
        self,
        *,
        job: dict,
        platform: Platform,
        cancel_check,
    ) -> DownloadResult:
        if platform.key != "linkedin":
            return await super().download(
                job=job,
                platform=platform,
                cancel_check=cancel_check,
            )

        job_id = int(job["id"])
        work_dir = self.settings.work_root / f"job-{job_id}"
        source_url = str(job.get("normalized_url") or job["url"]).strip()
        cookie = self._cookie_file(platform)
        native_dir = work_dir / "linkedin-native"

        if work_dir.exists():
            shutil.rmtree(work_dir)
        native_dir.mkdir(parents=True, exist_ok=True)

        native_error: Exception | None = None
        try:
            if await cancel_check():
                raise DownloadCancelled("Cancelled before LinkedIn image extraction")
            extractor = LinkedInImageExtractor(cookie)
            await asyncio.to_thread(
                extractor.extract_and_download,
                source_url,
                native_dir,
            )
            if await cancel_check():
                raise DownloadCancelled("Cancelled after LinkedIn image extraction")
            if not media_files(native_dir):
                raise LinkedInImageExtractionError(
                    "LinkedIn image extractor produced no media files"
                )
            stored = finalize_files(
                work_dir=native_dir,
                download_root=self.settings.download_root,
                platform_folder=platform.folder,
                created_at=job["created_at"],
            )
            return DownloadResult(engine="linkedin-native-images", stored=stored)
        except DownloadCancelled:
            raise
        except LinkedInNoImagePost as exc:
            native_error = exc
            LOGGER.info("job=%s is not a LinkedIn image post", job_id)
        except Exception as exc:  # noqa: BLE001 - use video fallback below
            native_error = exc
            LOGGER.warning("job=%s LinkedIn image extraction failed: %s", job_id, exc)

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
                f"linkedin-native-images: {type(native_error).__name__}: {native_error} | {fallback_exc}",
                fallback_exc.report_path,
            ) from fallback_exc


__all__ = [
    "DownloadCancelled",
    "DownloadFailed",
    "Downloader",
    "LinkedInImageCandidate",
    "LinkedInImageExtractionError",
    "LinkedInImageExtractor",
    "extract_linkedin_images",
]
