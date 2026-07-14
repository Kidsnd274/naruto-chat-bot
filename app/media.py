"""Telegram media ingestion without any model-side preprocessing."""

import asyncio
import base64
from dataclasses import dataclass
from io import BytesIO
import logging
from pathlib import Path
import subprocess
import tempfile

from PIL import Image

logger = logging.getLogger("media")


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


def media_label(message) -> str:
    descriptor = describe_media(message)
    if descriptor is None:
        return ""
    if descriptor.kind == "sticker":
        emoji = (getattr(descriptor.source, "emoji", None) or "").strip()
        return f"[sent a sticker {emoji}]" if emoji else "[sent a sticker]"
    labels = {
        "photo": "[sent a photo]",
        "image_document": "[sent an image]",
        "animation": "[sent an animation]",
        "video": "[sent a video]",
        "video_note": "[sent a video note]",
        "video_document": "[sent a video]",
    }
    return labels.get(descriptor.kind, "[sent media]")


def reply_media_kind(message) -> str | None:
    descriptor = describe_media(message)
    return descriptor.kind if descriptor else None


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
