"""The scripted stand-in model for docs/LAB.md's walkthrough. It answers by
what the request contains, the way a model would react to the prompt, so a
candidate's prompt change changes the outcome. Shared by the walkthrough
test and the end-to-end check over HTTP. Its replies are written for the
walkthrough; a real model's wording differs."""

import json

# Phrases the walkthrough's candidates add to the banter instructions.
CURRENT_ONLY = "Answer only the current request"
TEASE = "tease back"
ONE_LINE = "Keep teasing to one line"


def answer(system: str, current: str, tools_offered: bool) -> tuple[str, dict | None]:
    """(text, tool call or None) for one request."""
    lowered = current.lower()
    if "describe" in system.lower() and "image" in system.lower() and not tools_offered:
        return "An image.", None
    if "make a poll" in lowered and tools_offered and "The poll is up" not in current:
        return "", {"name": "create_poll",
                    "arguments": {"question": "BBQ day?", "options": ["Saturday", "Sunday"]}}
    if "the poll is up" in lowered:
        return "[NO REPLY]", None
    if "bouldering" in lowered:
        if CURRENT_ONLY in system:
            return ("[REPLY] Oi Bob! Warm up properly, chalk up, and trust your shoes. Falling "
                    "off is part of it, believe it!"), None
        return ("[REPLY] Oi Bob! Did we settle who's bringing the grill for the BBQ? Anyway, "
                "warm up first and trust your shoes!"), None
    if "pick her up" in lowered:
        if CURRENT_ONLY in system:
            return "[REPLY] Her flight lands at 7:40, so leave the house by 7!", None
        return ("[REPLY] 7:40 landing, so leave by 7! And the BBQ is still on for Saturday, "
                "right?"), None
    if "rough" in lowered:
        return ("Hey Alice, that sounds really tough. I'm here, and we'll grab food this weekend "
                "if you want."), None
    if "better than you" in lowered or "admit it" in lowered:
        if ONE_LINE in system:
            return "Lucky bowling, Wei. Rematch Saturday, believe it!", None
        if TEASE in system.lower():
            return ("Lucky? I let you win, Wei! Rematch Saturday, and bring tissues for when I "
                    "crush you, dattebayo!"), None
        return "Heh, you got lucky this time! Rematch Saturday, believe it!", None
    if "karaoke" in lowered:
        if ONE_LINE in system:
            return "Mic's mine, Wei. Try to keep up!", None
        if TEASE in system.lower():
            return ("Karaoke? Wei, last time you cleared the room in one song! Fine, I'm in, "
                    "but I'm picking the songs!"), None
        return "Karaoke sounds fun! Count me in, believe it!", None
    return "Heh.", None


def as_openai(text: str, call: dict | None, model: str) -> dict:
    """A chat.completions response body."""
    message: dict = {"role": "assistant", "content": text}
    finish = "stop"
    if call is not None:
        message["tool_calls"] = [{"id": "call-1", "type": "function",
                                  "function": {"name": call["name"],
                                               "arguments": json.dumps(call["arguments"])}}]
        finish = "tool_calls"
    return {"id": "chatcmpl-doc", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 30, "total_tokens": 930}}
