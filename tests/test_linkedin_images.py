from __future__ import annotations

import html
import json

from app.linkedin_downloader import extract_linkedin_images


def _code_page(payload: dict) -> str:
    return (
        '<html><body><code style="display: none">'
        + html.escape(json.dumps(payload))
        + "</code></body></html>"
    )


def test_extract_highest_resolution_linkedin_post_image() -> None:
    payload = {
        "included": [
            {
                "$type": "com.linkedin.voyager.dash.feed.ImageComponent",
                "content": {
                    "images": [
                        {
                            "vectorImage": {
                                "rootUrl": "https://media.licdn.com/dms/image/v2/example/",
                                "artifacts": [
                                    {
                                        "width": 640,
                                        "height": 360,
                                        "fileIdentifyingUrlPathSegment": "small.jpg",
                                    },
                                    {
                                        "width": 1920,
                                        "height": 1080,
                                        "fileIdentifyingUrlPathSegment": "large.jpg",
                                    },
                                ],
                            }
                        }
                    ]
                },
            }
        ]
    }

    images = extract_linkedin_images(_code_page(payload))

    assert images
    assert images[0].url.endswith("large.jpg")
    assert images[0].width == 1920
    assert images[0].height == 1080


def test_ignore_profile_photo_when_post_image_exists() -> None:
    payload = {
        "included": [
            {
                "actor": {
                    "profile": {
                        "image": {
                            "rootUrl": "https://media.licdn.com/dms/image/v2/profile/",
                            "artifacts": [
                                {
                                    "width": 800,
                                    "height": 800,
                                    "fileIdentifyingUrlPathSegment": "avatar.jpg",
                                }
                            ],
                        }
                    }
                }
            },
            {
                "$type": "com.linkedin.voyager.dash.feed.ImageComponent",
                "content": {
                    "image": {
                        "rootUrl": "https://media.licdn.com/dms/image/v2/feedshare/",
                        "artifacts": [
                            {
                                "width": 1200,
                                "height": 900,
                                "fileIdentifyingUrlPathSegment": "post.jpg",
                            }
                        ],
                    }
                },
            },
        ]
    }

    urls = [item.url for item in extract_linkedin_images(_code_page(payload))]

    assert any(url.endswith("post.jpg") for url in urls)
    assert not any(url.endswith("avatar.jpg") for url in urls)


def test_video_metadata_disables_image_post_fallback() -> None:
    payload = {
        "included": [
            {
                "$type": "com.linkedin.videocontent.VideoPlayMetadata",
                "progressiveStreams": [
                    {
                        "streamingLocations": [
                            {"url": "https://dms.licdn.com/video/example.mp4"}
                        ]
                    }
                ],
                "thumbnail": {
                    "rootUrl": "https://media.licdn.com/dms/image/v2/video/",
                    "artifacts": [
                        {
                            "width": 1280,
                            "height": 720,
                            "fileIdentifyingUrlPathSegment": "preview.jpg",
                        }
                    ],
                },
            }
        ]
    }

    assert extract_linkedin_images(_code_page(payload)) == []


def test_use_open_graph_image_as_last_resort() -> None:
    page = (
        '<html><head><meta property="og:image" '
        'content="https://media.licdn.com/dms/image/v2/feedshare/fallback.jpg">'
        "</head></html>"
    )

    images = extract_linkedin_images(page)

    assert len(images) == 1
    assert images[0].url.endswith("fallback.jpg")
