from __future__ import annotations

import json

import pytest

from app.threads_extractor import (
    ThreadsExtractionError,
    canonicalize_threads_url,
    extract_threads_media,
)


def test_canonicalize_threads_post_urls() -> None:
    assert (
        canonicalize_threads_url("https://www.threads.com/@user/post/ABC?xmt=tracking")
        == "https://www.threads.com/@user/post/ABC"
    )
    assert (
        canonicalize_threads_url("https://threads.net/t/ABC/")
        == "https://www.threads.com/t/ABC"
    )


def test_reject_non_threads_url() -> None:
    with pytest.raises(ThreadsExtractionError):
        canonicalize_threads_url("https://example.com/@user/post/ABC")


def test_extract_target_post_carousel_and_best_quality() -> None:
    payload = {
        "data": {
            "thread_items": [
                {
                    "post": {
                        "code": "OTHER",
                        "video_versions": [
                            {
                                "url": "https://video.cdninstagram.com/other.mp4",
                                "width": 1080,
                                "height": 1920,
                            }
                        ],
                    }
                },
                {
                    "post": {
                        "code": "ABC",
                        "carousel_media": [
                            {
                                "video_versions": [
                                    {
                                        "url": "https://video.cdninstagram.com/v-low.mp4",
                                        "width": 320,
                                        "height": 480,
                                    },
                                    {
                                        "url": "https://video.cdninstagram.com/v-high.mp4",
                                        "width": 1080,
                                        "height": 1920,
                                    },
                                ]
                            },
                            {
                                "image_versions2": {
                                    "candidates": [
                                        {
                                            "url": "https://scontent.cdninstagram.com/i-low.jpg",
                                            "width": 320,
                                            "height": 320,
                                        },
                                        {
                                            "url": "https://scontent.cdninstagram.com/i-high.jpg",
                                            "width": 1080,
                                            "height": 1080,
                                        },
                                    ]
                                }
                            },
                        ],
                    }
                },
            ]
        }
    }
    document = '<script type="application/json">' + json.dumps(payload) + "</script>"

    items = extract_threads_media([document], "ABC")

    assert [(item.kind, item.url) for item in items] == [
        ("video", "https://video.cdninstagram.com/v-high.mp4"),
        ("image", "https://scontent.cdninstagram.com/i-high.jpg"),
    ]


def test_extract_video_element_as_fallback() -> None:
    document = (
        '<html><video src="https://video.cdninstagram.com/direct.mp4"></video></html>'
    )
    items = extract_threads_media([document], "ABC")
    assert [(item.kind, item.url) for item in items] == [
        ("video", "https://video.cdninstagram.com/direct.mp4")
    ]


def test_ignore_non_meta_media_url() -> None:
    document = '<meta property="og:video" content="https://evil.example/video.mp4">'
    assert extract_threads_media([document], "ABC") == []
