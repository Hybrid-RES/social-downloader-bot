from app.platforms import detect_platform, normalize_url


def test_detect_known_platforms() -> None:
    assert detect_platform("https://youtu.be/abc").key == "youtube"
    assert detect_platform("https://www.instagram.com/reel/abc/").key == "instagram"
    assert detect_platform("https://twitter.com/user/status/1").key == "twitter"
    assert detect_platform("https://x.com/user/status/1").key == "twitter"
    assert detect_platform("https://vm.tiktok.com/abc/").key == "tiktok"
    assert detect_platform("https://fb.watch/abc/").key == "facebook"
    assert detect_platform("https://www.threads.net/@user/post/abc").key == "threads"
    assert detect_platform("https://www.threads.com/@user/post/abc").key == "threads"
    assert detect_platform("https://www.linkedin.com/posts/example").key == "linkedin"
    assert detect_platform("https://pin.it/1uvrt8N9r").key == "pinterest"
    assert detect_platform("https://www.pinterest.com/pin/123/").key == "pinterest"


def test_normalize_tracking_and_twitter_host() -> None:
    value = normalize_url(
        "https://www.twitter.com/user/status/123/?utm_source=a&ref_src=b&lang=en#fragment"
    )
    assert value == "https://x.com/user/status/123?lang=en"


def test_normalize_threads_share_tracking() -> None:
    value = normalize_url(
        "https://www.threads.com/@user/post/ABC/?xmt=tracking&slof=1&utm_source=share"
    )
    assert value == "https://threads.com/@user/post/ABC"


def test_normalize_keeps_youtube_video_id() -> None:
    value = normalize_url("https://www.youtube.com/watch?v=abc123&si=tracking&utm_source=x")
    assert value == "https://youtube.com/watch?v=abc123"
