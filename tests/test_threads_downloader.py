from app.threads_downloader import _threads_job_url


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
