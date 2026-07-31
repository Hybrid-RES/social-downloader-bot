from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any, Iterator

from yt_dlp.extractor.linkedin import LinkedInIE
from yt_dlp.utils import (
    extract_attributes,
    float_or_none,
    int_or_none,
    mimetype2ext,
    url_or_none,
)
from yt_dlp.utils.traversal import traverse_obj

_CODE_BLOCK_RE = re.compile(r"<code\b[^>]*>(?P<body>.*?)</code>", re.IGNORECASE | re.DOTALL)


def _iter_code_payloads(webpage: str) -> Iterator[Any]:
    """Yield JSON payloads embedded in LinkedIn's hidden <code> blocks."""
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


def _find_media_node(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if value.get("progressiveStreams") or value.get("adaptiveStreams"):
            return value
        for child in value.values():
            found = _find_media_node(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_media_node(child)
            if found:
                return found
    return None


def extract_linkedin_media_node(webpage: str) -> dict[str, Any] | None:
    """Return the first LinkedIn video metadata object from embedded page JSON."""
    for payload in _iter_code_payloads(webpage):
        included = payload.get("included") if isinstance(payload, dict) else None
        found = _find_media_node(included if included is not None else payload)
        if found:
            return found
    return None


def _nested_text(value: Any, *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        current = value
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current.strip():
            return current.strip()
    return None


class _LinkedInSocialDownloaderIE(LinkedInIE, plugin_name="social_downloader"):
    """LinkedIn extractor fallback for posts whose media lives in hidden JSON blocks."""

    def _extract_embedded_metadata(self, video_id: str, webpage: str) -> dict[str, Any]:
        media = extract_linkedin_media_node(webpage)
        if not media:
            return {}

        formats: list[dict[str, Any]] = []
        subtitles: dict[str, list[dict[str, Any]]] = {}
        seen_urls: set[str] = set()

        for stream in media.get("progressiveStreams") or []:
            if not isinstance(stream, dict):
                continue
            for location in stream.get("streamingLocations") or []:
                if not isinstance(location, dict):
                    continue
                stream_url = url_or_none(location.get("url"))
                if not stream_url or stream_url in seen_urls:
                    continue
                seen_urls.add(stream_url)
                formats.append(
                    {
                        "url": stream_url,
                        "ext": mimetype2ext(stream.get("mediaType")) or "mp4",
                        "filesize": int_or_none(stream.get("size")),
                        "tbr": float_or_none(stream.get("bitRate"), scale=1000),
                        "width": int_or_none(stream.get("width")),
                        "height": int_or_none(stream.get("height")),
                    }
                )

        for stream in media.get("adaptiveStreams") or []:
            if not isinstance(stream, dict):
                continue
            protocol = str(stream.get("protocol") or "").upper()
            for playlist in stream.get("masterPlaylists") or []:
                if not isinstance(playlist, dict):
                    continue
                playlist_url = url_or_none(playlist.get("url"))
                if not playlist_url or playlist_url in seen_urls:
                    continue
                seen_urls.add(playlist_url)
                if protocol == "HLS":
                    fmts, subs = self._extract_m3u8_formats_and_subtitles(
                        playlist_url,
                        video_id,
                        "mp4",
                        m3u8_id="hls",
                        fatal=False,
                    )
                elif protocol == "DASH":
                    fmts, subs = self._extract_mpd_formats_and_subtitles(
                        playlist_url,
                        video_id,
                        mpd_id="dash",
                        fatal=False,
                    )
                    for fmt in fmts:
                        if fmt.get("ext") in {None, "iso.segment"}:
                            fmt["ext"] = "mp4"
                else:
                    continue
                formats.extend(fmts)
                self._merge_subtitles(subs, target=subtitles)

        for transcript in media.get("transcripts") or []:
            if not isinstance(transcript, dict):
                continue
            caption_url = url_or_none(transcript.get("captionFile"))
            if not caption_url:
                continue
            locale = transcript.get("locale") or {}
            language = locale.get("language") if isinstance(locale, dict) else None
            subtitles.setdefault(language or "en", []).append(
                {
                    "url": caption_url,
                    "ext": str(transcript.get("captionFormat") or "vtt").lower(),
                }
            )

        thumbnails: list[dict[str, Any]] = []
        thumbnail = media.get("thumbnail") or {}
        if isinstance(thumbnail, dict):
            root_url = str(thumbnail.get("rootUrl") or "")
            for artifact in thumbnail.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                path = artifact.get("fileIdentifyingUrlPathSegment")
                if not isinstance(path, str) or not path:
                    continue
                thumbnails.append(
                    {
                        "url": f"{root_url}{path}",
                        "width": int_or_none(artifact.get("width")),
                        "height": int_or_none(artifact.get("height")),
                    }
                )

        metadata = media.get("metadata") or {}
        uploader = _nested_text(
            metadata,
            ("actor", "description", "text"),
            ("actor", "description", "accessibilityText"),
            ("actor", "name", "text"),
            ("actor", "name"),
        )
        description = _nested_text(
            metadata,
            ("commentary", "text", "text"),
            ("commentary", "text"),
        )

        result: dict[str, Any] = {
            "formats": formats,
            "subtitles": subtitles,
            "thumbnails": thumbnails,
        }
        if uploader:
            result["uploader"] = uploader
        if description:
            result["description"] = description
        return result

    def _real_extract(self, url: str) -> dict[str, Any]:
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)

        formats: list[dict[str, Any]] = []
        subtitles: dict[str, list[dict[str, Any]]] = {}

        video_block = self._search_regex(
            r"(<video[^>]+>)",
            webpage,
            "video",
            default=None,
        )
        if video_block:
            video_attrs = extract_attributes(video_block)
            sources_raw = video_attrs.get("data-sources")
            if sources_raw:
                sources = self._parse_json(sources_raw, video_id)
                formats = [
                    {
                        "url": source["src"],
                        "ext": mimetype2ext(source.get("type")),
                        "tbr": float_or_none(source.get("data-bitrate"), scale=1000),
                    }
                    for source in sources
                    if isinstance(source, dict) and url_or_none(source.get("src"))
                ]
            captions_url = url_or_none(video_attrs.get("data-captions-url"))
            if captions_url:
                subtitles = {"en": [{"url": captions_url, "ext": "vtt"}]}

        embedded: dict[str, Any] = {}
        if not formats:
            embedded = self._extract_embedded_metadata(video_id, webpage)
            formats = embedded.pop("formats", [])
            subtitles = embedded.pop("subtitles", {})

        result: dict[str, Any] = {
            "id": video_id,
            "formats": formats,
            "title": self._og_search_title(webpage, default=None)
            or self._html_extract_title(webpage),
            "like_count": int_or_none(
                self._search_regex(
                    r'\bdata-num-reactions="(\d+)"',
                    webpage,
                    "reactions",
                    default=None,
                )
            ),
            "uploader": traverse_obj(
                self._yield_json_ld(webpage, video_id),
                (
                    lambda _, value: value.get("@type") == "SocialMediaPosting",
                    "author",
                    "name",
                    {str},
                ),
                get_all=False,
            ),
            "thumbnail": self._og_search_thumbnail(webpage, default=None),
            "description": self._og_search_description(webpage, default=None),
            "subtitles": subtitles,
        }
        result.update({key: value for key, value in embedded.items() if value not in (None, [], {})})
        return result


__all__ = ["_LinkedInSocialDownloaderIE"]
