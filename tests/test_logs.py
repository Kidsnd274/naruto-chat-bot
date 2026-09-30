"""Logging: token redaction, the database handler and chat tagging."""

import logging

import pytest

from naruto.db.logs import LogRepository
from naruto.logs import DatabaseLogHandler, current_chat_id, redact


@pytest.fixture
def handler():
    handler = DatabaseLogHandler(logging.DEBUG)
    logger = logging.getLogger("test.naruto.logs")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    yield handler, logger
    logger.removeHandler(handler)


def test_redact_hides_bot_tokens():
    url = "POST https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getUpdates"
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in redact(url)
    assert "bot<bot-token>/getUpdates" in redact(url)
    assert redact("message 123: fine") == "message 123: fine"


def test_records_are_buffered_then_stored_with_chat_ids(db, handler):
    handler, logger = handler
    repo = LogRepository(db)
    logger.info("plain")
    token = current_chat_id.set(-4001)
    try:
        logger.warning("in a chat")
    finally:
        current_chat_id.reset(token)
    logger.info("explicit", extra={"chat_id": -5})
    assert repo.query() == []  # nothing written until flushed

    handler.flush_to(repo)
    rows = repo.query()
    assert [(r.message, r.chat_id) for r in rows] == [
        ("explicit", -5), ("in a chat", -4001), ("plain", None)]
    assert rows[1].level_name == "WARNING"
    assert repo.query(chat_id=-4001)[0].message == "in a chat"
    assert repo.query(min_level=logging.WARNING)[0].message == "in a chat"
    assert repo.query(logger_prefix="test.naruto")[0].logger == "test.naruto.logs"
    assert repo.query(after_id=rows[1].id)[0].message == "explicit"
    assert repo.chat_ids() == [-4001, -5]


def test_exceptions_and_tokens_are_stored_safely(db, handler):
    handler, logger = handler
    repo = LogRepository(db)
    try:
        raise ValueError("bad token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    except ValueError:
        logger.exception("failed")
    handler.flush_to(repo)
    row = repo.recent_errors()[0]
    assert "ValueError" in row.message and "Traceback" in row.message
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in row.message


def test_old_records_are_deleted(db):
    repo = LogRepository(db)
    repo.insert_many([(100.0, logging.INFO, "x", None, "old"), (300.0, logging.INFO, "x", None, "new")])
    assert repo.delete_older_than(200.0) == 1
    assert [r.message for r in repo.query()] == ["new"]
