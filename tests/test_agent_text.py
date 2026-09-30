"""Prompt and output text helpers (ported from the old test_bot_context.py)."""

import pytest

from naruto.agent.text import (
    clean_model_output,
    estimate_message_tokens,
    parse_reply_marker,
    strip_bot_mention,
    strip_internal_json,
    without_image_data,
)


@pytest.mark.parametrize("text,expected_reply,expected_clean", [
    ("[REPLY] hello", True, "hello"),
    ("[reply] hello", True, "hello"),
    ("[Reply]hello", True, "hello"),
    ("  [REPLY]   hello world  ", True, "hello world  "),
    ("**[REPLY]** sure thing", True, "sure thing"),
    ("hello [REPLY] world", False, "hello [REPLY] world"),
    ("hello world", False, "hello world"),
    ("", False, ""),
])
def test_parse_reply_marker(text, expected_reply, expected_clean):
    assert parse_reply_marker(text) == (expected_reply, expected_clean)


@pytest.mark.parametrize("text,expected", [
    ("Sure thing!", (False, "Sure thing!")),
    ("[REPLY] Sure", (True, "Sure")),
    ("[123] Naruto (you) (18:05): Sure", (False, "Sure")),
    ("[REPLY] [123] Naruto (18:05): Sure", (True, "Sure")),
    ("[123] Naruto (you) (18:05): [REPLY] Sure", (True, "Sure")),
    ("Naruto: Sure", (False, "Sure")),
    ("Naruto (you): Sure", (False, "Sure")),
    ("[1] is the answer (really): no", (False, "no")),
    ("Remember [1]? It was fun", (False, "Remember [1]? It was fun")),
    ("   ", (False, "")),
])
def test_clean_model_output(text, expected):
    assert clean_model_output(text, "Naruto") == expected


@pytest.mark.parametrize("text,expected", [
    ("@naruto_bot hi", "hi"),
    ("hey @Naruto_Bot what's up", "hey what's up"),
    ("@naruto_bot", ""),
    ("@naruto_bot2 is someone else", "@naruto_bot2 is someone else"),
    ("line one @naruto_bot\nline two", "line one\nline two"),
    ("no mention", "no mention"),
])
def test_strip_bot_mention(text, expected):
    assert strip_bot_mention(text, "naruto_bot") == expected


def test_image_token_estimate_ignores_base64_length():
    def request(data):
        return [{"role": "user", "content": [
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
        ]}]

    short, long = request("A"), request("A" * 100_000)
    assert estimate_message_tokens(short, 321) == estimate_message_tokens(long, 321)
    assert estimate_message_tokens(short, 321) >= 321


def test_without_image_data_never_contains_base64():
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,U0VDUkVU" * 50}},
        ]},
    ]
    cleaned = without_image_data(messages)
    assert "U0VDUkVU" not in str(cleaned)
    assert cleaned[1]["content"][1]["image_url"]["url"].startswith("<image/jpeg,")
    assert cleaned[0] == messages[0]
    assert "U0VDUkVU" in str(messages)  # the original is untouched


@pytest.mark.parametrize("text,kept", [
    ('```json\n{"digest": "", "notes": []}\n```', ""),
    ('{"digest": "", "notes": []}', ""),
    ('{"digest": "", "notes": []}\n\n[REPLY] My apologies!', "[REPLY] My apologies!"),
    ('{"digest": "", "notes": [], "reply": "[REPLY] Hi there"}', "[REPLY] Hi there"),
])
def test_internal_json_is_stripped_from_replies(text, kept):
    rest, removed = strip_internal_json(text)
    assert rest == kept and removed.startswith("{")


@pytest.mark.parametrize("text", [
    "[REPLY] hi", 'Here: {"digest": 1}', '{"name": "search_chat"}', "{not json", "",
])
def test_other_replies_are_left_alone(text):
    assert strip_internal_json(text) == (text, None)
