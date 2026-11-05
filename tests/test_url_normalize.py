import pytest
from bot.services.downloader import detect_platform, normalize_url


@pytest.mark.parametrize("url,expected_platform", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.tiktok.com/@user/video/123", "tiktok"),
    ("https://vm.tiktok.com/abc123/", "tiktok"),
    ("https://www.instagram.com/reel/abc123/", "instagram"),
    ("https://twitter.com/user/status/123", "twitter"),
    ("https://x.com/user/status/123", "twitter"),
    ("https://www.facebook.com/watch?v=123", "facebook"),
    ("https://fb.watch/abc/", "facebook"),
    ("https://pin.it/abc", "pinterest"),
    ("https://vimeo.com/123456", "vimeo"),
    ("https://example.com/video", None),
])
def test_detect_platform(url, expected_platform):
    assert detect_platform(url) == expected_platform


@pytest.mark.parametrize("raw,expected_clean", [
    (
        "https://youtu.be/dQw4w9WgXcQ?si=abc123",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    ),
    (
        "https://www.youtube.com/watch?v=abc&utm_source=share&utm_medium=web",
        "https://www.youtube.com/watch?v=abc",
    ),
    (
        "https://www.tiktok.com/@user/video/123?utm_campaign=test&feature=x",
        "https://www.tiktok.com/@user/video/123",
    ),
    (
        "https://example.com/path#fragment",
        "https://example.com/path",
    ),
])
def test_normalize_url(raw, expected_clean):
    assert normalize_url(raw) == expected_clean
