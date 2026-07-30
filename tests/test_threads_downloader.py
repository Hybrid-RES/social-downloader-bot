from app.threads_downloader import (
    _extract_threads_post_url,
    _is_threads_share_url,
    _threads_job_url,
)


def test_threads_job_url_prefers_normalized_url() -> None:
    job = {
        "url": "https://www.threads.com/@user/post/ABC?xmt=tracking&slof=1",
        "normalized_url": "https://threads.com/@user/post/ABC",
    }

    assert _threads_job_url(job) == "https://threads.com/@user/post/ABC"


def test_threads_job_url_falls_back_to_original() -> None:
    job = {
        "url": " https://www.threads.com/@user/post/ABC ",
        "normalized_url": "",
    }

    assert _threads_job_url(job) == "https://www.threads.com/@user/post/ABC"


def test_detect_threads_share_url() -> None:
    assert _is_threads_share_url("https://www.threads.com/share/BAX2bUftZO")
    assert not _is_threads_share_url("https://threads.com/@user/post/ABC")


def test_extract_post_url_from_redirect_html() -> None:
    document = (
        '<script>window.location.href='
        '"https:\\/\\/www.threads.com\\/@marmarchuk\\/post\\/Dba6c4IjubN?xmt=test";'
        "</script>"
    )

    assert (
        _extract_threads_post_url(document)
        == "https://www.threads.com/@marmarchuk/post/Dba6c4IjubN"
    )


def test_extract_relative_post_url_from_json() -> None:
    document = '{"target":"/@marmarchuk/post/Dba6c4IjubN"}'

    assert (
        _extract_threads_post_url(document)
        == "https://www.threads.com/@marmarchuk/post/Dba6c4IjubN"
    )
