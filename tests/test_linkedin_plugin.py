from __future__ import annotations

import html
import json

from yt_dlp_plugins.extractor.linkedin_social_downloader import (
    _LinkedInSocialDownloaderIE,
    extract_linkedin_media_node,
)


def test_extract_linkedin_media_from_hidden_code_block() -> None:
    media = {
        "$type": "com.linkedin.videocontent.VideoPlayMetadata",
        "progressiveStreams": [
            {
                "width": 720,
                "height": 1280,
                "bitRate": 1500000,
                "mediaType": "video/mp4",
                "streamingLocations": [
                    {"url": "https://dms.licdn.com/video/example.mp4"}
                ],
            }
        ],
        "metadata": {
            "actor": {"description": {"text": "Example uploader"}},
            "commentary": {"text": {"text": "Example post"}},
        },
    }
    payload = {"included": [{"unrelated": True}, media]}
    webpage = (
        "<html><body><code style=\"display: none\">"
        + html.escape(json.dumps(payload))
        + "</code></body></html>"
    )

    assert extract_linkedin_media_node(webpage) == media


def test_ignore_invalid_linkedin_code_blocks() -> None:
    webpage = "<code>not json</code><code>{\"included\": [{\"text\": \"no video\"}]}</code>"
    assert extract_linkedin_media_node(webpage) is None


def test_patched_linkedin_extractor_matches_post_url() -> None:
    url = (
        "https://www.linkedin.com/posts/nksingal_"
        "bess-electricalcomponent-protectiondevices-share-"
        "7488165291341021184-4FLk/"
    )
    assert _LinkedInSocialDownloaderIE.suitable(url)
