from app.urls import extract_urls


def test_extract_multiple_urls_and_trim_punctuation() -> None:
    text = "Смотри https://example.com/a, и https://youtu.be/abc). Повтор https://example.com/a"
    assert extract_urls(text) == ["https://example.com/a", "https://youtu.be/abc"]


def test_empty_text() -> None:
    assert extract_urls(None) == []
