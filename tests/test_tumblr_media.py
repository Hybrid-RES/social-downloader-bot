from __future__ import annotations

import json

from app.platforms import detect_platform, normalize_url
from app.tumblr_downloader import extract_tumblr_media


POST_ID = "807248660972945408"


def _page(payload: dict) -> str:
    return f'<html><body><script type="application/json">{json.dumps(payload)}</script></body></html>'


def test_detect_and_normalize_tumblr_url() -> None:
    url = (
        "https://www.tumblr.com/ordinaryoffensivemagic/"
        f"{POST_ID}/frieren-tantrum-manga-vs-anime?source=share"
    )

    assert detect_platform(url).key == "tumblr"
    assert normalize_url(url) == (
        "https://tumblr.com/ordinaryoffensivemagic/"
        f"{POST_ID}/frieren-tantrum-manga-vs-anime"
    )


def test_extract_npf_image_blocks_and_choose_largest_variant() -> None:
    payload = {
        "posts": [
            {
                "id": POST_ID,
                "blog": {
                    "avatar": [
                        {
                            "url": "https://64.media.tumblr.com/avatar_small.jpg",
                            "type": "image/jpeg",
                            "width": 128,
                            "height": 128,
                        }
                    ]
                },
                "content": [
                    {
                        "type": "image",
                        "media": [
                            {
                                "url": "https://64.media.tumblr.com/post_540.jpg",
                                "type": "image/jpeg",
                                "width": 540,
                                "height": 720,
                            },
                            {
                                "url": "https://64.media.tumblr.com/post_1280.jpg",
                                "type": "image/jpeg",
                                "width": 1280,
                                "height": 1707,
                                "has_original_dimensions": True,
                            },
                        ],
                    },
                    {
                        "type": "image",
                        "media": [
                            {
                                "url": "https://64.media.tumblr.com/post_second.png",
                                "type": "image/png",
                                "width": 1000,
                                "height": 800,
                            }
                        ],
                    },
                ],
            }
        ]
    }

    result = extract_tumblr_media(_page(payload), POST_ID)

    assert [item.url for item in result] == [
        "https://64.media.tumblr.com/post_1280.jpg",
        "https://64.media.tumblr.com/post_second.png",
    ]
    assert all("avatar" not in item.url for item in result)


def test_extract_legacy_tumblr_photos() -> None:
    payload = {
        "response": {
            "posts": [
                {
                    "id_string": POST_ID,
                    "photos": [
                        {
                            "original_size": {
                                "url": "https://64.media.tumblr.com/legacy_original.jpg",
                                "width": 1600,
                                "height": 1200,
                            },
                            "alt_sizes": [
                                {
                                    "url": "https://64.media.tumblr.com/legacy_small.jpg",
                                    "width": 500,
                                    "height": 375,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    }

    result = extract_tumblr_media(_page(payload), POST_ID)

    assert len(result) == 1
    assert result[0].kind == "image"
    assert result[0].url.endswith("legacy_original.jpg")


def test_video_block_does_not_download_poster_as_separate_image() -> None:
    payload = {
        "post": {
            "id": POST_ID,
            "content": [
                {
                    "type": "video",
                    "media": [
                        {
                            "url": "https://va.media.tumblr.com/tumblr_video.mp4",
                            "type": "video/mp4",
                            "width": 720,
                            "height": 1280,
                        }
                    ],
                    "poster": [
                        {
                            "url": "https://64.media.tumblr.com/video_poster.jpg",
                            "type": "image/jpeg",
                            "width": 720,
                            "height": 1280,
                        }
                    ],
                }
            ],
        }
    }

    result = extract_tumblr_media(_page(payload), POST_ID)

    assert len(result) == 1
    assert result[0].kind == "video"
    assert result[0].url.endswith("tumblr_video.mp4")
