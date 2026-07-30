from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

LOGGER = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
_MAX_HTML_BYTES = 24 * 1024 * 1024
_MAX_MEDIA_ITEMS = 20
_MEDIA_MARKERS = ("video_versions", "image_versions2", "carousel_media", "media_url")
_VIDEO_META_KEYS = {
    "og:video",
    "og:video:url",
    "og:video:secure_url",
    "twitter:player:stream",
}
_IMAGE_META_KEYS = {
    "og:image",
    "og:image:url",
    "og:image:secure_url",
    "twitter:image",
}
_ALLOWED_MEDIA_HOST_SUFFIXES = (
    "cdninstagram.com",
    "fbcdn.net",
    "fbsbx.com",
    "threads.com",
    "instagram.com",
)
_CONTENT_TYPE_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
}
_ALLOWED_SUFFIXES = {
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".avif",
    ".gif",
}


class ThreadsExtractionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ThreadsMedia:
    kind: str
    url: str
    width: int = 0
    height: int = 0

    @property
    def pixels(self) -> int:
        return max(0, self.width) * max(0, self.height)


@dataclass(frozen=True, slots=True)
class DownloadedThreadsMedia:
    kind: str
    path: Path
    source_url: str


class _ThreadsHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[tuple[str, str]] = []
        self.scripts: list[str] = []
        self.links: list[str] = []
        self.video_sources: list[str] = []
        self._in_script = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "")
            if key and content:
                self.meta.append((key, content))
        elif tag.lower() == "script":
            self._in_script = True
            self._script_parts = []
        elif tag.lower() in {"video", "source"}:
            target = values.get("src")
            source_type = values.get("type", "").lower()
            if target and (tag.lower() == "video" or source_type.startswith("video/")):
                self.video_sources.append(target)
        elif tag.lower() in {"a", "link", "iframe"}:
            target = values.get("href") or values.get("src")
            if target:
                self.links.append(target)

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            self.scripts.append("".join(self._script_parts))
            self._in_script = False
            self._script_parts = []


def canonicalize_threads_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in {"threads.com", "threads.net"} and not host.endswith(
        (".threads.com", ".threads.net")
    ):
        raise ThreadsExtractionError("URL is not a Threads post")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0].startswith("@") and parts[1] == "post":
        path = f"/{parts[0]}/post/{parts[2]}"
    elif len(parts) >= 2 and parts[0] == "t":
        path = f"/t/{parts[1]}"
    else:
        raise ThreadsExtractionError("Unsupported Threads URL format")
    return urlunsplit(("https", "www.threads.com", path, "", ""))


def threads_shortcode(url: str) -> str:
    parts = [part for part in urlsplit(canonicalize_threads_url(url)).path.split("/") if part]
    return parts[-1]


def _decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return html_lib.unescape(value).replace("\\/", "/").replace("\\u0026", "&")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _candidate_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return html_lib.unescape(value).replace("\\/", "/").replace("\\u0026", "&")
    if not isinstance(value, dict):
        return None
    for key in ("url", "src", "media_url", "video_url", "display_url"):
        result = value.get(key)
        if isinstance(result, str) and result.startswith(("https://", "http://")):
            return html_lib.unescape(result).replace("\\/", "/").replace("\\u0026", "&")
    return None


def _best_candidate(values: Any) -> tuple[str, int, int] | None:
    if isinstance(values, dict):
        values = values.get("candidates") or values.get("versions") or [values]
    if not isinstance(values, list):
        return None
    candidates: list[tuple[int, int, int, str]] = []
    for item in values:
        url = _candidate_url(item)
        if not url:
            continue
        width = _as_int(item.get("width") if isinstance(item, dict) else 0)
        height = _as_int(item.get("height") if isinstance(item, dict) else 0)
        bitrate = _as_int(
            (item.get("bitrate") or item.get("bit_rate") or item.get("bitrate_kbps"))
            if isinstance(item, dict)
            else 0
        )
        candidates.append((width * height, bitrate, width + height, url))
    if not candidates:
        return None
    _, _, _, url = max(candidates)
    matching = next(item for item in values if _candidate_url(item) == url)
    return (
        url,
        _as_int(matching.get("width") if isinstance(matching, dict) else 0),
        _as_int(matching.get("height") if isinstance(matching, dict) else 0),
    )


def _identity(media: ThreadsMedia) -> tuple[str, str, str]:
    parsed = urlsplit(media.url)
    return media.kind, parsed.netloc.lower(), unquote(parsed.path)


def _append_media(items: list[ThreadsMedia], media: ThreadsMedia) -> None:
    if not media.url.startswith(("https://", "http://")):
        return
    host = (urlsplit(media.url).hostname or "").lower()
    if not any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _ALLOWED_MEDIA_HOST_SUFFIXES
    ):
        return
    key = _identity(media)
    for index, existing in enumerate(items):
        if _identity(existing) == key:
            if media.pixels > existing.pixels:
                items[index] = media
            return
    if len(items) < _MAX_MEDIA_ITEMS:
        items.append(media)


def _select_media_from_item(item: dict[str, Any]) -> ThreadsMedia | None:
    videos = item.get("video_versions") or item.get("video_candidates")
    if result := _best_candidate(videos):
        return ThreadsMedia("video", *result)

    media_type = item.get("media_type")
    media_url = item.get("media_url") or item.get("video_url")
    if media_url and str(media_type).upper() in {"2", "VIDEO"}:
        return ThreadsMedia(
            "video",
            str(media_url),
            _as_int(item.get("original_width") or item.get("width")),
            _as_int(item.get("original_height") or item.get("height")),
        )

    images = item.get("image_versions2") or item.get("image_candidates")
    if result := _best_candidate(images):
        return ThreadsMedia("image", *result)

    if media_url and str(media_type).upper() in {"1", "IMAGE"}:
        return ThreadsMedia(
            "image",
            str(media_url),
            _as_int(item.get("original_width") or item.get("width")),
            _as_int(item.get("original_height") or item.get("height")),
        )
    return None


def _parse_nested_json_string(value: str) -> Any | None:
    if not any(marker in value for marker in _MEDIA_MARKERS):
        return None
    variants = [value, html_lib.unescape(value), value.replace("\\/", "/")]
    for candidate in variants:
        candidate = candidate.strip()
        if not candidate or candidate[0] not in "[{":
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _walk_json(node: Any, items: list[ThreadsMedia], *, depth: int = 0) -> None:
    if depth > 80 or len(items) >= _MAX_MEDIA_ITEMS:
        return
    if isinstance(node, dict):
        carousel = node.get("carousel_media")
        if isinstance(carousel, list) and carousel:
            for child in carousel:
                if isinstance(child, dict) and (media := _select_media_from_item(child)):
                    _append_media(items, media)
            return

        if media := _select_media_from_item(node):
            _append_media(items, media)
            return

        for value in node.values():
            _walk_json(value, items, depth=depth + 1)
    elif isinstance(node, list):
        for value in node:
            _walk_json(value, items, depth=depth + 1)
    elif isinstance(node, str):
        if nested := _parse_nested_json_string(node):
            _walk_json(nested, items, depth=depth + 1)


def _find_target_nodes(
    node: Any,
    shortcode: str,
    *,
    depth: int = 0,
) -> list[dict[str, Any]]:
    if depth > 80:
        return []
    result: list[dict[str, Any]] = []
    if isinstance(node, dict):
        identifiers = {str(node.get(key) or "") for key in ("code", "shortcode")}
        canonical = node.get("canonical_url") or node.get("permalink")
        if isinstance(canonical, str):
            identifiers.add(canonical.rstrip("/").rsplit("/", 1)[-1])
        if shortcode in identifiers:
            result.append(node)
            return result
        for value in node.values():
            result.extend(_find_target_nodes(value, shortcode, depth=depth + 1))
    elif isinstance(node, list):
        for value in node:
            result.extend(_find_target_nodes(value, shortcode, depth=depth + 1))
    return result


def _json_values(script: str) -> Iterable[Any]:
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
    for start in starts[:200]:
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        yield value


def _regex_media(html_text: str, kind: str) -> list[ThreadsMedia]:
    marker = "video_versions" if kind == "video" else "image_versions2"
    decoded = html_lib.unescape(html_text).replace("\\/", "/").replace("\\u0026", "&")
    result: list[ThreadsMedia] = []
    for match in re.finditer(marker, decoded):
        fragment = decoded[match.start() : match.start() + 180_000]
        url_match = re.search(r'"url"\s*:\s*"((?:\\.|[^"\\])+)"', fragment)
        if not url_match:
            continue
        url = _decode_json_string(url_match.group(1))
        width_match = re.search(
            r'"width"\s*:\s*(\d+)', fragment[: url_match.end() + 500]
        )
        height_match = re.search(
            r'"height"\s*:\s*(\d+)', fragment[: url_match.end() + 500]
        )
        result.append(
            ThreadsMedia(
                kind,
                url,
                int(width_match.group(1)) if width_match else 0,
                int(height_match.group(1)) if height_match else 0,
            )
        )
        if len(result) >= _MAX_MEDIA_ITEMS:
            break
    return result


def extract_threads_media(
    documents: Iterable[str],
    shortcode: str | None = None,
) -> list[ThreadsMedia]:
    items: list[ThreadsMedia] = []
    meta_video: list[str] = []
    meta_image: list[str] = []
    video_sources: list[str] = []

    for html_text in documents:
        parser = _ThreadsHTMLParser()
        try:
            parser.feed(html_text)
        except Exception:  # malformed HTML should not block JSON fallback
            LOGGER.debug("Threads HTML parser failed", exc_info=True)

        video_sources.extend(parser.video_sources)
        for key, content in parser.meta:
            if key in _VIDEO_META_KEYS:
                meta_video.append(content)
            elif key in _IMAGE_META_KEYS:
                meta_image.append(content)

        for script in parser.scripts:
            if not any(marker in script for marker in _MEDIA_MARKERS):
                continue
            for value in _json_values(script):
                targets = _find_target_nodes(value, shortcode) if shortcode else []
                if targets:
                    for target in targets:
                        _walk_json(target, items)
                else:
                    _walk_json(value, items)

        if not items:
            for media in _regex_media(html_text, "video"):
                _append_media(items, media)
            for media in _regex_media(html_text, "image"):
                _append_media(items, media)

    if not items:
        for url in video_sources:
            _append_media(items, ThreadsMedia("video", url))
    if not items:
        for url in meta_video:
            _append_media(items, ThreadsMedia("video", url))
        if not items:
            for url in meta_image:
                _append_media(items, ThreadsMedia("image", url))
    return items


def _parse_netscape_cookies(path: Path | None) -> list[tuple[str, str, str, str]]:
    if path is None or not path.is_file():
        return []
    cookies: list[tuple[str, str, str, str]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, _, cookie_path, _, _, name, value = fields[:7]
        cookies.append((name, value, domain, cookie_path or "/"))
    return cookies


class ThreadsExtractor:
    def __init__(self, cookie_path: Path | None = None) -> None:
        self.cookie_path = cookie_path
        self._session = None

    def _new_session(self):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # pragma: no cover - present in Docker image
            raise ThreadsExtractionError("curl-cffi is not installed") from exc

        session = curl_requests.Session()
        session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )
        for name, value, domain, cookie_path in _parse_netscape_cookies(self.cookie_path):
            try:
                session.cookies.set(name, value, domain=domain, path=cookie_path)
            except Exception:
                LOGGER.debug("Unable to load cookie %s for %s", name, domain)
        return session

    @property
    def session(self):
        if self._session is None:
            self._session = self._new_session()
        return self._session

    def _get_text(
        self,
        url: str,
        *,
        referer: str | None = None,
        json_expected: bool = False,
    ) -> str:
        headers = {"Referer": referer} if referer else None
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    impersonate="chrome",
                    allow_redirects=True,
                    timeout=45,
                )
                if response.status_code >= 400:
                    raise ThreadsExtractionError(
                        f"HTTP {response.status_code} for {urlsplit(url).netloc}"
                    )
                content = response.content
                if len(content) > _MAX_HTML_BYTES:
                    raise ThreadsExtractionError("Threads response is unexpectedly large")
                if json_expected:
                    payload = response.json()
                    return json.dumps(payload, ensure_ascii=False)
                return response.text
            except Exception as exc:
                last_error = exc
                LOGGER.debug(
                    "Threads request attempt %s failed for %s: %s",
                    attempt,
                    url,
                    exc,
                )
        raise ThreadsExtractionError(
            str(last_error) if last_error else "Threads request failed"
        )

    def _fetch_documents(self, page_url: str) -> list[str]:
        canonical = canonicalize_threads_url(page_url)
        shortcode = threads_shortcode(canonical)
        documents: list[str] = []
        errors: list[str] = []

        page_variants = [
            canonical,
            f"{canonical}/embed",
            f"https://www.threads.com/t/{shortcode}/embed",
        ]
        for variant in page_variants:
            try:
                documents.append(
                    self._get_text(variant, referer="https://www.threads.com/")
                )
            except ThreadsExtractionError as exc:
                errors.append(f"{urlsplit(variant).path}: {exc}")

        encoded = quote(canonical, safe="")
        for endpoint in (
            f"https://graph.threads.com/oembed?url={encoded}",
            f"https://graph.threads.net/v1.0/oembed?url={encoded}",
        ):
            try:
                payload_text = self._get_text(
                    endpoint,
                    referer=canonical,
                    json_expected=True,
                )
                documents.append(payload_text)
                payload = json.loads(payload_text)
                embed_html = payload.get("html")
                if isinstance(embed_html, str):
                    documents.append(embed_html)
            except (ThreadsExtractionError, json.JSONDecodeError) as exc:
                errors.append(f"oEmbed: {exc}")

        if not documents:
            raise ThreadsExtractionError(
                "; ".join(errors[-3:]) or "Unable to fetch Threads post"
            )
        return documents

    @staticmethod
    def _suffix_for(response, media: ThreadsMedia) -> str:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if suffix := _CONTENT_TYPE_SUFFIXES.get(content_type):
            return suffix
        suffix = Path(unquote(urlsplit(media.url).path)).suffix.lower()
        if suffix in _ALLOWED_SUFFIXES:
            return ".jpg" if suffix == ".jpeg" else suffix
        return ".mp4" if media.kind == "video" else ".jpg"

    def _download_one(
        self,
        media: ThreadsMedia,
        destination: Path,
        page_url: str,
    ) -> Path:
        response = self.session.get(
            media.url,
            headers={"Referer": canonicalize_threads_url(page_url)},
            impersonate="chrome",
            allow_redirects=True,
            stream=True,
            timeout=90,
        )
        if response.status_code >= 400:
            raise ThreadsExtractionError(f"media HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "").lower()
        if content_type.startswith(("text/html", "application/json")):
            raise ThreadsExtractionError(
                f"unexpected media content type {content_type}"
            )

        suffix = self._suffix_for(response, media)
        target = destination.with_suffix(suffix)
        temporary = target.with_name(f".{target.name}.part")
        temporary.unlink(missing_ok=True)
        total = 0
        with temporary.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                total += len(chunk)
        if total == 0:
            temporary.unlink(missing_ok=True)
            raise ThreadsExtractionError(
                "Threads media download returned an empty file"
            )
        temporary.replace(target)
        return target

    def extract_and_download(
        self,
        page_url: str,
        destination: Path,
    ) -> list[DownloadedThreadsMedia]:
        canonical = canonicalize_threads_url(page_url)
        shortcode = threads_shortcode(canonical)
        destination.mkdir(parents=True, exist_ok=True)
        media_items = extract_threads_media(
            self._fetch_documents(canonical),
            shortcode,
        )
        if not media_items:
            raise ThreadsExtractionError(
                "No downloadable media found in the public Threads page; "
                "the post may be private, deleted, or text-only"
            )

        downloaded: list[DownloadedThreadsMedia] = []
        errors: list[str] = []
        for index, media in enumerate(media_items, 1):
            base = destination / f"{shortcode}_{index:02d}"
            try:
                path = self._download_one(media, base, canonical)
            except ThreadsExtractionError as exc:
                errors.append(f"item #{index}: {exc}")
                continue
            downloaded.append(DownloadedThreadsMedia(media.kind, path, media.url))

        if not downloaded:
            raise ThreadsExtractionError(
                "; ".join(errors) or "Threads media download failed"
            )
        return downloaded
