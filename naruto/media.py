"""Download Telegram media on demand and convert it to an image the model
can read (a still frame for moving media). Nothing is stored."""

import asyncio
import base64
from dataclasses import dataclass
from io import BytesIO
import logging
from pathlib import Path
import subprocess
import tempfile

from PIL import Image

logger = logging.getLogger(__name__)


class MediaTooLarge(Exception):
    pass


@dataclass
class MediaDescriptor:
    kind: str
    source: object
    mime_type: str
    moving: bool = False
    thumbnail: object | None = None
    duration: float = 0
    tgs: bool = False


def _duration_seconds(value) -> float:
    if value is None:
        return 0
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(value)


def describe_media(message) -> MediaDescriptor | None:
    if getattr(message, "photo", None):
        return MediaDescriptor("photo", message.photo[-1], "image/jpeg")

    sticker = getattr(message, "sticker", None)
    if sticker is not None:
        animated = bool(getattr(sticker, "is_animated", False))
        video = bool(getattr(sticker, "is_video", False))
        return MediaDescriptor(
            "sticker",
            sticker,
            "application/x-tgsticker" if animated else (
                "video/webm" if video else "image/webp"
            ),
            moving=animated or video,
            thumbnail=getattr(sticker, "thumbnail", None),
            tgs=animated,
        )

    animation = getattr(message, "animation", None)
    if animation is not None:
        return MediaDescriptor(
            "animation",
            animation,
            getattr(animation, "mime_type", None) or "video/mp4",
            moving=True,
            thumbnail=getattr(animation, "thumbnail", None),
            duration=_duration_seconds(getattr(animation, "duration", 0)),
        )

    video = getattr(message, "video", None)
    if video is not None:
        return MediaDescriptor(
            "video",
            video,
            getattr(video, "mime_type", None) or "video/mp4",
            moving=True,
            thumbnail=getattr(video, "thumbnail", None),
            duration=_duration_seconds(getattr(video, "duration", 0)),
        )

    video_note = getattr(message, "video_note", None)
    if video_note is not None:
        return MediaDescriptor(
            "video_note",
            video_note,
            "video/mp4",
            moving=True,
            thumbnail=getattr(video_note, "thumbnail", None),
            duration=_duration_seconds(getattr(video_note, "duration", 0)),
        )

    document = getattr(message, "document", None)
    mime_type = (getattr(document, "mime_type", None) or "") if document else ""
    if document is not None and mime_type.startswith("image/"):
        moving = mime_type == "image/gif"
        return MediaDescriptor(
            "image_document",
            document,
            mime_type,
            moving=moving,
            thumbnail=getattr(document, "thumbnail", None) if moving else None,
        )
    if document is not None and mime_type.startswith("video/"):
        return MediaDescriptor(
            "video_document",
            document,
            mime_type,
            moving=True,
            thumbnail=getattr(document, "thumbnail", None),
        )
    return None


def has_supported_media(message) -> bool:
    return describe_media(message) is not None


def _check_declared_size(file_object, max_bytes: int) -> None:
    size = getattr(file_object, "file_size", None)
    if size is not None and size > max_bytes:
        raise MediaTooLarge


async def _download(file_object, max_bytes: int) -> bytes:
    _check_declared_size(file_object, max_bytes)
    telegram_file = await file_object.get_file()
    _check_declared_size(telegram_file, max_bytes)
    output = BytesIO()
    await telegram_file.download_to_memory(out=output)
    data = output.getvalue()
    if len(data) > max_bytes:
        raise MediaTooLarge
    return data


def _inspect_or_normalize_image(data: bytes) -> tuple[bytes, str, int, int]:
    with Image.open(BytesIO(data)) as image:
        image.load()
        width, height = image.size
        image_format = (image.format or "").upper()
        mime_types = {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
        }
        if image_format in mime_types:
            return data, mime_types[image_format], width, height

        output = BytesIO()
        image.convert("RGBA").save(output, format="PNG", optimize=True)
        return output.getvalue(), "image/png", width, height


def _normalize_frame(data: bytes) -> tuple[bytes, str, int, int]:
    with Image.open(BytesIO(data)) as image:
        image.load()
        image = image.convert("RGB")
        image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        width, height = image.size
        output = BytesIO()
        image.save(output, format="JPEG", quality=85, optimize=True)
        return output.getvalue(), "image/jpeg", width, height


def _video_suffix(mime_type: str) -> str:
    return {
        "image/gif": ".gif",
        "video/webm": ".webm",
        "video/mp4": ".mp4",
    }.get(mime_type, ".mp4")


def _extract_video_frame(data: bytes, mime_type: str, duration: float) -> bytes:
    with tempfile.TemporaryDirectory(prefix="naruto-media-") as tmp:
        input_path = Path(tmp) / f"input{_video_suffix(mime_type)}"
        output_path = Path(tmp) / "frame.png"
        input_path.write_bytes(data)
        if duration <= 0:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(input_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            try:
                duration = float(probe.stdout.strip())
            except (TypeError, ValueError):
                duration = 0
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if duration > 0:
            command.extend(["-ss", str(duration * 0.25)])
        command.extend(["-i", str(input_path), "-frames:v", "1", str(output_path)])
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=45,
        )
        return output_path.read_bytes()


def _render_tgs_frame(data: bytes) -> bytes:
    from rlottie_python import LottieAnimation

    with tempfile.TemporaryDirectory(prefix="naruto-tgs-") as tmp:
        input_path = Path(tmp) / "sticker.tgs"
        input_path.write_bytes(data)
        animation = LottieAnimation.from_tgs(str(input_path))
        frame_count = max(int(animation.lottie_animation_get_totalframe()), 1)
        image = animation.render_pillow_frame(frame_num=int((frame_count - 1) * 0.25))
        output = BytesIO()
        image.convert("RGBA").save(output, format="PNG")
        return output.getvalue()


async def extract_attachments(
    message,
    max_bytes: int,
) -> tuple[list[dict], str | None]:
    """Download and convert one Telegram message's visual media.

    Returns ``(attachments, failure_marker)``. Errors are intentionally
    reduced to safe textual markers so logs never expose Telegram URLs or
    downloaded Base64 data.
    """
    descriptor = describe_media(message)
    if descriptor is None:
        return [], None

    try:
        # Enforce the cap against the original media even when a cheap
        # Telegram thumbnail is available and is all that will be downloaded.
        _check_declared_size(descriptor.source, max_bytes)
        if descriptor.moving:
            if descriptor.thumbnail is not None:
                raw_frame = await _download(descriptor.thumbnail, max_bytes)
            else:
                source = await _download(descriptor.source, max_bytes)
                if descriptor.tgs:
                    raw_frame = await asyncio.to_thread(_render_tgs_frame, source)
                else:
                    raw_frame = await asyncio.to_thread(
                        _extract_video_frame,
                        source,
                        descriptor.mime_type,
                        descriptor.duration,
                    )
            data, mime_type, width, height = await asyncio.to_thread(
                _normalize_frame,
                raw_frame,
            )
        else:
            source = await _download(descriptor.source, max_bytes)
            data, mime_type, width, height = await asyncio.to_thread(
                _inspect_or_normalize_image,
                source,
            )

        return [{
            "kind": descriptor.kind,
            "mime_type": mime_type,
            "base64": base64.b64encode(data).decode("ascii"),
            "width": width,
            "height": height,
        }], None
    except MediaTooLarge:
        return [], "[media unavailable: too large]"
    except Exception as exc:
        logger.warning(
            "Could not download or convert Telegram %s (%s)",
            descriptor.kind,
            type(exc).__name__,
        )
        return [], "[media unavailable: conversion failed]"


# Stored media kinds that can be turned into an image, with the MIME type to
# assume when the stored metadata has none.
_STORED_KINDS = {
    "photo": "image/jpeg",
    "sticker": "image/webp",
    "animation": "video/mp4",
    "video": "video/mp4",
    "video_note": "video/mp4",
    "document": "",
}


def stored_media_supported(kind: str | None, meta: dict | None) -> bool:
    if kind not in _STORED_KINDS:
        return False
    if kind == "document":
        mime = (meta or {}).get("mime_type") or ""
        return mime.startswith("image/") or mime.startswith("video/")
    return True


async def extract_stored(bot, kind: str, file_id: str, meta: dict | None,
                         max_bytes: int) -> tuple[dict | None, str | None]:
    """Download a stored message's media by its Telegram file_id and turn it
    into one image. Returns ``(attachment, failure_marker)``; nothing is
    kept on disk."""
    meta = meta or {}
    mime = meta.get("mime_type") or _STORED_KINDS.get(kind) or "application/octet-stream"
    if kind == "sticker":
        mime = ("application/x-tgsticker" if meta.get("animated")
                else "video/webm" if meta.get("video") else "image/webp")
    try:
        declared = meta.get("file_size")
        if declared and declared > max_bytes:
            raise MediaTooLarge
        telegram_file = await bot.get_file(file_id)
        _check_declared_size(telegram_file, max_bytes)
        output = BytesIO()
        await telegram_file.download_to_memory(out=output)
        data = output.getvalue()
        if len(data) > max_bytes:
            raise MediaTooLarge
        if mime == "application/x-tgsticker":
            frame = await asyncio.to_thread(_render_tgs_frame, data)
            converted = await asyncio.to_thread(_normalize_frame, frame)
        elif mime.startswith("video/") or mime == "image/gif":
            frame = await asyncio.to_thread(_extract_video_frame, data, mime,
                                            float(meta.get("duration") or 0))
            converted = await asyncio.to_thread(_normalize_frame, frame)
        else:
            converted = await asyncio.to_thread(_inspect_or_normalize_image, data)
    except MediaTooLarge:
        return None, "[media unavailable: too large]"
    except Exception as exc:
        logger.warning("Could not download or convert stored %s (%s)", kind, type(exc).__name__)
        return None, "[media unavailable: download or conversion failed]"
    data, mime_type, width, height = converted
    return {"kind": kind, "mime_type": mime_type, "width": width, "height": height,
            "base64": base64.b64encode(data).decode("ascii")}, None
