"""describe_image: look at an older image in the chat, on demand.

The image is downloaded by its Telegram file_id, described by the model in a
separate request (counted against the run's model requests), and the
description is kept with the message, so later requests see it in the
transcript without looking again. Image bytes are never stored.
"""

from naruto import media
from naruto.agent.tools.base import Tool, ToolContext, ToolError, params
from naruto.agent.tools.lookup import message_in_chat
from naruto.llm import LLMError


async def describe_image(ctx: ToolContext, args: dict) -> str:
    services = ctx.services
    message = message_in_chat(ctx, args["message_id"])
    cached = services.messages.descriptions([message.id]).get(message.id)
    if cached:
        return f"Image in message {message.id}: {cached}"
    if not message.media_kind:
        raise ToolError(f"Message {message.id} has no image.")
    if not message.is_live or not message.media_file_id:
        raise ToolError("That image comes from an imported history; only its marker was "
                        "kept, so I can't look at it. Ask them to send it again.")
    if not media.stored_media_supported(message.media_kind, message.media_meta):
        raise ToolError(f"Message {message.id} has a {message.media_kind}, not an image.")
    if not services.settings.for_chat(ctx.chat.chat_id)["media.enabled"]:
        raise ToolError("Looking at images is turned off (Settings → Media).")
    if ctx.state.model_requests_left < 2:
        raise ToolError("No time left in this response to look at the image. Answer now and "
                        "offer to look if they ask again.")
    max_bytes = services.settings["media.max_size_mb"] * 1024 * 1024
    attachment, failure = await media.extract_stored(
        ctx.telegram, message.media_kind, message.media_file_id, message.media_meta, max_bytes)
    if attachment is None:
        raise ToolError(f"Couldn't open the image {failure or ''}".strip())
    caption = f" Its caption: {message.text}" if message.text else ""
    request = [
        {"role": "system", "content": services.settings["media.description_prompt"]},
        {"role": "user", "content": [
            {"type": "text", "text": f"An image from a group chat.{caption}"},
            {"type": "image_url",
             "image_url": {"url": f"data:{attachment['mime_type']};base64,{attachment['base64']}"}},
        ]},
    ]
    ctx.state.model_requests += 1
    try:
        result = await services.llm.chat(
            request, reasoning=False, max_tokens=services.settings["media.description_max_tokens"])
    except LLMError as exc:
        raise ToolError(f"Looking at the image failed ({exc}).") from None
    description = " ".join(result.text.split())
    if not description:
        raise ToolError("I couldn't make out the image.")
    services.messages.save_description(message, description, result.model)
    ctx.state.steps.append({"type": "model", "request": ctx.state.model_requests,
                            "purpose": f"describe image {message.id}",
                            "latency_ms": result.latency_ms, "finish_reason": result.finish_reason,
                            "text": description, "tool_calls": []})
    return f"Image in message {message.id}: {description}"


TOOLS = [
    Tool(
        "describe_image",
        "Look at an image (photo, sticker, GIF or video frame) sent earlier in the chat and "
        "get a description. Images attached to the current request, or to the message it "
        "replies to, are already shown to you.",
        params({"message_id": {"type": "integer", "description": "The [id] of the message."}},
               ("message_id",)),
        describe_image,
    ),
]
