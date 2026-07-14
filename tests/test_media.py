import base64
from datetime import timedelta
from io import BytesIO
import logging

import pytest

PIL = pytest.importorskip("PIL.Image")


class FakeTelegramFile:
    def __init__(self, data, file_size=None):
        self.data = data
        self.file_size = len(data) if file_size is None else file_size

    async def download_to_memory(self, out):
        out.write(self.data)


class FakeMedia:
    def __init__(self, data=b"", **attrs):
        self.data = data
        self.file_size = attrs.pop("file_size", len(data))
        self.downloads = 0
        for key, value in attrs.items():
            setattr(self, key, value)

    async def get_file(self):
        self.downloads += 1
        return FakeTelegramFile(self.data, self.file_size)


class FakeMessage:
    def __init__(self):
        self.photo = []
        self.sticker = None
        self.animation = None
        self.video = None
        self.video_note = None
        self.document = None


def image_bytes(fmt="JPEG", size=(16, 8), color="red"):
    output = BytesIO()
    mode = "RGB" if fmt in ("JPEG", "BMP") else "RGBA"
    PIL.new(mode, size, color).save(output, format=fmt)
    return output.getvalue()


@pytest.fixture
def media_module(initialized_config):
    import media
    return media


async def test_photo_downloads_largest_and_preserves_jpeg(media_module):
    small = FakeMedia(image_bytes(size=(4, 4)))
    large_bytes = image_bytes(size=(20, 10))
    large = FakeMedia(large_bytes)
    message = FakeMessage()
    message.photo = [small, large]

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert small.downloads == 0
    assert base64.b64decode(attachments[0]["base64"]) == large_bytes
    assert attachments[0]["mime_type"] == "image/jpeg"
    assert (attachments[0]["width"], attachments[0]["height"]) == (20, 10)


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("WEBP", "image/webp")])
async def test_supported_static_formats_are_preserved(media_module, fmt, mime):
    source = image_bytes(fmt=fmt)
    message = FakeMessage()
    message.document = FakeMedia(source, mime_type=mime, thumbnail=None)

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert base64.b64decode(attachments[0]["base64"]) == source
    assert attachments[0]["mime_type"] == mime


async def test_unsupported_static_image_is_normalized_to_png(media_module):
    message = FakeMessage()
    message.document = FakeMedia(
        image_bytes(fmt="BMP"),
        mime_type="image/bmp",
        thumbnail=None,
    )

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert attachments[0]["mime_type"] == "image/png"
    assert base64.b64decode(attachments[0]["base64"]).startswith(b"\x89PNG")


@pytest.mark.parametrize("attribute,kind,mime", [
    ("animation", "animation", "video/mp4"),
    ("video", "video", "video/mp4"),
    ("video_note", "video_note", "video/mp4"),
    ("document", "video_document", "video/webm"),
])
async def test_moving_media_prefers_thumbnail(media_module, attribute, kind, mime):
    thumbnail = FakeMedia(image_bytes(fmt="PNG", size=(2000, 1000)))
    source = FakeMedia(
        b"not downloaded",
        mime_type=mime,
        thumbnail=thumbnail,
        duration=20,
    )
    message = FakeMessage()
    setattr(message, attribute, source)

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert source.downloads == 0
    assert thumbnail.downloads == 1
    assert attachments[0]["kind"] == kind
    assert attachments[0]["mime_type"] == "image/jpeg"
    assert max(attachments[0]["width"], attachments[0]["height"]) == 1280


async def test_static_sticker_is_stored_as_webp(media_module):
    message = FakeMessage()
    message.sticker = FakeMedia(
        image_bytes(fmt="WEBP"),
        is_animated=False,
        is_video=False,
        thumbnail=None,
        emoji="🍥",
    )

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert attachments[0]["kind"] == "sticker"
    assert attachments[0]["mime_type"] == "image/webp"
    assert media_module.media_label(message) == "[sent a sticker 🍥]"


async def test_missing_video_thumbnail_uses_ffmpeg_fallback(media_module, monkeypatch):
    frame = image_bytes(fmt="PNG")
    calls = []
    monkeypatch.setattr(
        media_module,
        "_extract_video_frame",
        lambda data, mime, duration: calls.append((data, mime, duration)) or frame,
    )
    message = FakeMessage()
    message.video = FakeMedia(
        b"video bytes",
        mime_type="video/mp4",
        thumbnail=None,
        duration=40,
    )

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert calls == [(b"video bytes", "video/mp4", 40.0)]
    assert attachments[0]["mime_type"] == "image/jpeg"


async def test_animated_tgs_sticker_uses_lottie_fallback(media_module, monkeypatch):
    frame = image_bytes(fmt="PNG")
    monkeypatch.setattr(media_module, "_render_tgs_frame", lambda data: frame)
    message = FakeMessage()
    message.sticker = FakeMedia(
        b"tgs bytes",
        is_animated=True,
        is_video=False,
        thumbnail=None,
    )

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert attachments[0]["kind"] == "sticker"
    assert attachments[0]["mime_type"] == "image/jpeg"


async def test_video_sticker_prefers_thumbnail(media_module):
    thumbnail = FakeMedia(image_bytes(fmt="PNG"))
    message = FakeMessage()
    message.sticker = FakeMedia(
        b"webm bytes",
        is_animated=False,
        is_video=True,
        thumbnail=thumbnail,
    )

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert message.sticker.downloads == 0
    assert thumbnail.downloads == 1
    assert attachments[0]["kind"] == "sticker"


async def test_gif_document_uses_frame_extraction(media_module, monkeypatch):
    frame = image_bytes(fmt="PNG")
    calls = []
    monkeypatch.setattr(
        media_module,
        "_extract_video_frame",
        lambda data, mime, duration: calls.append((data, mime, duration)) or frame,
    )
    message = FakeMessage()
    message.document = FakeMedia(
        b"gif bytes",
        mime_type="image/gif",
        thumbnail=None,
    )

    attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert marker is None
    assert calls == [(b"gif bytes", "image/gif", 0)]
    assert attachments[0]["kind"] == "image_document"


def test_telegram_timedelta_duration_is_supported(media_module):
    message = FakeMessage()
    message.video = FakeMedia(
        b"video",
        mime_type="video/mp4",
        thumbnail=FakeMedia(image_bytes()),
        duration=timedelta(seconds=24),
    )

    assert media_module.describe_media(message).duration == 24


async def test_oversized_media_degrades_without_download(media_module):
    source = FakeMedia(image_bytes(), file_size=101)
    message = FakeMessage()
    message.photo = [source]

    attachments, marker = await media_module.extract_attachments(message, 100)

    assert attachments == []
    assert marker == "[media unavailable: too large]"
    assert source.downloads == 0


async def test_corrupt_media_and_errors_never_log_download_details(media_module, caplog):
    class FailingMedia(FakeMedia):
        async def get_file(self):
            raise RuntimeError("https://api.telegram.org/file/SECRET_TOKEN")

    message = FakeMessage()
    message.photo = [FailingMedia(b"broken")]

    with caplog.at_level(logging.WARNING):
        attachments, marker = await media_module.extract_attachments(message, 1_000_000)

    assert attachments == []
    assert marker == "[media unavailable: conversion failed]"
    assert "SECRET_TOKEN" not in caplog.text
