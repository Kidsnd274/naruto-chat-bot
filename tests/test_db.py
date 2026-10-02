"""Storage layer: migrations, chats, roster, messages and search."""

import pytest

from naruto.db import open_database
from naruto.db.board import BoardRepository, format_lines, parse_lines
from naruto.db.chats import ChatRepository, infer_chat_type
from naruto.db.members import MemberRepository
from naruto.db.people import PeopleRepository
from naruto.db.messages import IMPORT, LIVE, MessageRepository, NewMessage, fts_query
from naruto.db.migrations import MIGRATIONS

BASIC = -4012345678
SUPER = -1004012345678


@pytest.fixture
def chats(db):
    return ChatRepository(db)


@pytest.fixture
def members(db):
    return MemberRepository(db, PeopleRepository(db))


@pytest.fixture
def messages(db):
    return MessageRepository(db)


def live(chat_id, message_id, text="hi", *, date=1_000, sender_id=7, name="Alice", **kw):
    return NewMessage(
        chat_id=chat_id,
        origin_chat_id=kw.pop("origin_chat_id", chat_id),
        source=LIVE,
        message_id=message_id,
        sender_id=sender_id,
        sender_name=name,
        date=date,
        text=text,
        **kw,
    )


# ------------------------------------------------------------------ schema

def test_migrations_set_user_version_and_are_idempotent(db):
    assert db.schema_version == len(MIGRATIONS)
    db.migrate()
    assert db.schema_version == len(MIGRATIONS)


def test_meta_round_trip(db):
    assert db.get_meta("x") is None
    db.set_meta("x", "1")
    db.set_meta("x", "2")
    assert db.get_meta("x") == "2"


def test_nested_transaction_rolls_back_outer(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.set_meta("a", "1")
            with db.transaction():
                db.set_meta("b", "1")
            raise RuntimeError
    assert db.get_meta("a") is None
    assert db.get_meta("b") is None


# ------------------------------------------------------------------- chats

@pytest.mark.parametrize("chat_id,expected", [
    (-1001234567890, "supergroup"),
    (-123456789, "group"),
    (123456, "private"),
])
def test_infer_chat_type(chat_id, expected):
    assert infer_chat_type(chat_id) == expected


def test_new_chat_is_pending_and_title_refreshes(chats):
    chat, created = chats.upsert_seen(BASIC, title="Old name", chat_type="group")
    assert created and chat.status == "pending" and chat.type == "group"

    chat, created = chats.upsert_seen(BASIC, title="New name")
    assert not created
    assert chat.title == "New name"


def test_set_status_validates(chats):
    chats.upsert_seen(BASIC)
    assert chats.set_status(BASIC, "enabled").enabled
    with pytest.raises(ValueError):
        chats.set_status(BASIC, "bogus")
    assert chats.set_status(999, "enabled") is None


def test_missing_rights_lists_pin_when_unknown_or_false(chats):
    chats.upsert_seen(BASIC)
    assert chats.get(BASIC).missing_rights() == ["pin messages"]
    chats.set_rights(BASIC, can_pin=True, can_delete=False)
    chat = chats.get(BASIC)
    assert chat.can_pin is True and chat.can_delete is False
    assert chat.missing_rights() == []


def test_migrate_moves_chat_messages_roster_and_keeps_alias(chats, members, messages):
    chats.upsert_seen(BASIC, title="BBQ crew")
    chats.set_status(BASIC, "enabled")
    members.upsert_live(BASIC, 7, "Alice", "alice")
    members.add_alias(BASIC, 7, "Ali")
    messages.insert_live(live(BASIC, 10))

    chat = chats.migrate(BASIC, SUPER)

    assert chat.chat_id == SUPER and chat.type == "supergroup" and chat.enabled
    assert chats.get(BASIC).chat_id == SUPER  # resolves through the alias
    assert chats.get(BASIC, resolve=False) is None
    assert chats.aliases_for(SUPER) == [BASIC]
    assert members.get(SUPER, 7).aliases == ["Ali"]
    moved = messages.get_live(BASIC, 10)  # origin ID space is unchanged
    assert moved.chat_id == SUPER


def test_migrate_merges_into_pending_supergroup_seen_first(chats, members, messages):
    chats.upsert_seen(BASIC, title="BBQ crew")
    chats.set_status(BASIC, "enabled")
    members.upsert_live(BASIC, 7, "Alice", "alice")
    messages.insert_live(live(BASIC, 10))
    # The bot saw the new supergroup before the migration message arrived.
    chats.upsert_seen(SUPER)
    members.upsert_live(SUPER, 7, "Alice", "alice")
    messages.insert_live(live(SUPER, 10, text="same id, other chat", date=2_000))

    chat = chats.migrate(BASIC, SUPER)

    assert chat.status == "enabled"  # explicit decision beats pending
    assert chat.title == "BBQ crew"
    assert messages.count(SUPER) == 2
    assert len(members.list(SUPER)) == 1


def test_migrate_twice_is_harmless(chats):
    chats.upsert_seen(BASIC)
    chats.migrate(BASIC, SUPER)
    chats.migrate(BASIC, SUPER)
    assert chats.resolve(BASIC) == SUPER
    assert chats.get(SUPER) is not None


def test_upsert_seen_on_old_id_updates_new_chat(chats):
    chats.upsert_seen(BASIC)
    chats.migrate(BASIC, SUPER)
    chat, created = chats.upsert_seen(BASIC, title="Renamed")
    assert not created and chat.chat_id == SUPER and chat.title == "Renamed"


def test_summaries_count_live_and_imported(chats, messages):
    chats.upsert_seen(BASIC)
    messages.insert_live(live(BASIC, 1))
    messages.insert_imported([NewMessage(
        chat_id=BASIC, origin_chat_id=BASIC, source=IMPORT, message_id=5,
        sender_name="Bob", date=500, import_id=1,
    )])
    summary = chats.summaries()[0]
    assert (summary.live_messages, summary.imported_messages) == (1, 1)


# ------------------------------------------------------------------ roster

def test_live_upsert_keeps_aliases_and_updates_names(members):
    members.upsert_live(BASIC, 7, "Alice", "alice")
    members.add_alias(BASIC, 7, "Ali")
    members.upsert_live(BASIC, 7, "Alice Tan", "alice_t")
    member = members.get(BASIC, 7)
    assert (member.display_name, member.username, member.aliases) == ("Alice Tan", "alice_t", ["Ali"])


def test_imported_name_never_overrides_live_name(members):
    members.upsert_live(BASIC, 7, "Alice", "alice")
    members.upsert_imported(BASIC, 7, "Contact name for Alice", 100)
    members.upsert_imported(BASIC, 8, "Bob", 100)
    assert members.get(BASIC, 7).display_name == "Alice"
    assert members.get(BASIC, 7).first_seen_at == 100
    assert members.get(BASIC, 8).source == "import"


def test_alias_commands(members):
    assert members.add_alias(BASIC, 7, "Ali") is False  # not in roster
    members.upsert_live(BASIC, 7, "Alice", "Alice_X")
    assert members.find_user_id_by_username(BASIC, "@alice_x") == 7
    assert members.add_alias(BASIC, 7, "Ali")
    assert members.add_alias(BASIC, 7, "Ali")  # duplicate is fine
    assert members.aliases(BASIC, 7) == ["Ali"]
    assert members.remove_alias(BASIC, 7, "nope") is False
    assert members.remove_alias(BASIC, 7, "Ali")
    members.add_alias(BASIC, 7, "A")
    assert members.clear_aliases(BASIC) == 1


# ---------------------------------------------------------------- messages

def test_insert_live_resolves_reply_and_ignores_duplicates(messages):
    first = messages.insert_live(live(BASIC, 10, "when is the bbq?"))
    reply = messages.insert_live(live(BASIC, 11, "saturday", reply_to_message_id=10))
    orphan = messages.insert_live(live(BASIC, 12, "re", reply_to_message_id=3,
                                       reply_to_snippet="Bob: old"))
    again = messages.insert_live(live(BASIC, 10, "changed?"))

    assert reply.reply_to_row_id == first.id
    assert orphan.reply_to_row_id is None and orphan.reply_to_snippet == "Bob: old"
    assert again.id == first.id and again.text == "when is the bbq?"


def test_media_meta_round_trips(messages):
    stored = messages.insert_live(live(BASIC, 1, "", media_kind="sticker",
                                       media_meta={"emoji": "😂"}))
    assert stored.media_meta == {"emoji": "😂"}


def test_edit_updates_text_and_search_index(messages):
    messages.insert_live(live(BASIC, 1, "meet at noon"))
    assert messages.apply_edit(BASIC, 1, text="meet at seven", edit_date=2_000)
    assert messages.apply_edit(BASIC, 99, text="x", edit_date=2_000) is False
    assert messages.search(BASIC, "noon") == []
    assert [m.text for m in messages.search(BASIC, "seven")] == ["meet at seven"]
    assert messages.get_live(BASIC, 1).edit_date == 2_000


def test_recent_window_moves_in_steps(messages):
    stored = [messages.insert_live(live(BASIC, i, f"m{i}", date=1_000 + i)) for i in range(1, 31)]

    def window_ids(anchor_index):
        anchor = stored[anchor_index]
        return [m.message_id for m in messages.recent_window(BASIC, anchor, window=10, step=5)]

    # 9 messages before the anchor: all of them.
    assert window_ids(9) == list(range(1, 10))
    # 14 before: start = ((14-10)//5)*5 = 0 -> all 14.
    assert window_ids(14) == list(range(1, 15))
    # 15 before: start jumps to 5.
    assert window_ids(15) == list(range(6, 16))
    # 19 before: same start, so the prefix is unchanged.
    assert window_ids(19)[:10] == window_ids(15)
    assert all(10 <= len(window_ids(i)) < 15 for i in range(15, 30))


def test_recent_window_orders_imports_before_live(messages):
    anchor = messages.insert_live(live(BASIC, 50, "now", date=5_000))
    messages.insert_imported([
        NewMessage(chat_id=BASIC, origin_chat_id=BASIC, source=IMPORT, message_id=i,
                   sender_name="Bob", date=1_000 + i, text=f"old {i}", import_id=1)
        for i in (2, 1)
    ])
    window = messages.recent_window(BASIC, anchor, window=10, step=5)
    assert [m.text for m in window] == ["old 1", "old 2"]


def test_browse_filters_and_pages(messages):
    for i in range(1, 6):
        messages.insert_live(live(BASIC, i, f"bbq plan {i}", date=1_000 + i,
                                  sender_id=7 if i % 2 else 8, name="Alice" if i % 2 else "Bob"))
    page = messages.browse(BASIC, limit=2)
    assert page.total == 5 and [m.message_id for m in page.messages] == [5, 4]
    assert page.has_more
    assert messages.browse(BASIC, sender_id=8).total == 2
    assert messages.browse(BASIC, query="plan 3").total == 1
    assert messages.browse(BASIC, query="!!!").total == 0
    assert messages.browse(BASIC, since=1_004).total == 2
    assert messages.browse(BASIC, source="import").total == 0


def test_search_uses_prefixes_and_diacritics(messages):
    messages.insert_live(live(BASIC, 1, "Café on Saturday"))
    assert len(messages.search(BASIC, "cafe satur")) == 1
    assert messages.search(BASIC, '"') == []


def test_fts_query_quotes_words():
    assert fts_query('bbq "east coast"') == '"bbq"* "east"* "coast"*'
    assert fts_query("   ") is None


def test_delete_for_chat_before_date_clears_dangling_replies(messages):
    old = messages.insert_live(live(BASIC, 1, "old", date=100))
    messages.insert_live(live(BASIC, 2, "new", date=200, reply_to_message_id=1))
    assert old.id
    assert messages.count_for_delete(BASIC, before=150) == 1
    assert messages.delete_for_chat(BASIC, before=150) == 1
    remaining = messages.get_live(BASIC, 2)
    assert remaining.reply_to_row_id is None
    assert messages.search(BASIC, "old") == []


def test_senders_uses_latest_name(messages):
    messages.insert_live(live(BASIC, 1, date=100, name="Old Name"))
    messages.insert_live(live(BASIC, 2, date=200, name="New Name"))
    assert messages.senders(BASIC) == [(7, "New Name", 2)]


def test_upgrade_from_version_1_keeps_data(tmp_path):
    from naruto.db.database import Database
    from naruto.db.migrations import MIGRATIONS

    path = str(tmp_path / "old.db")
    old = Database(path)
    old._conn.executescript(f"BEGIN; {MIGRATIONS[0]} PRAGMA user_version = 1; COMMIT;")
    ChatRepository(old).upsert_seen(BASIC, title="kept")
    old.close()

    upgraded = open_database(path)
    assert upgraded.schema_version == len(MIGRATIONS)
    assert ChatRepository(upgraded).get(BASIC).title == "kept"
    assert upgraded.scalar("SELECT COUNT(*) FROM imports") == 0
    upgraded.close()


def test_upgrade_to_13_keeps_one_summary_per_period_and_every_edit(tmp_path):
    from naruto.db.database import Database

    path = str(tmp_path / "v12.db")
    old = Database(path)
    for version, script in enumerate(MIGRATIONS[:12], start=1):
        old._conn.executescript(f"BEGIN; {script} PRAGMA user_version = {version}; COMMIT;")
    digest = ("INSERT INTO history_digests (id, chat_id, status, source, grouping, timezone, "
              "period_start, period_end, period_id, fingerprint, text, edited, created_at, "
              "updated_at, updated_by) VALUES (?, 1, 'active', 'live', 'month', 'UTC', 0, 10, "
              "?, 'f', ?, ?, 0, 0, 't')")
    with old.transaction():
        # A crash between saving the summary and finishing the period, then
        # two retries: three summaries of period 1, the period points at 11.
        for digest_id, text, edited in ((10, "first try", 0), (11, "kept", 0),
                                        (12, "owner fixed this", 1)):
            old.execute(digest, (digest_id, 1, text, edited))
        old.execute(digest, (20, 2, "only one", 0))  # not pointed at, but alone
        old.execute("INSERT INTO history_periods (id, chat_id, source, grouping, timezone, "
                    "period_start, period_end, status, digest_id, created_at, updated_at) "
                    "VALUES (1, 1, 'live', 'month', 'UTC', 0, 10, 'done', 11, 0, 0)")
        old.execute("INSERT INTO imports (status, file_name, distill_status, notes_added, "
                    "created_at) VALUES ('paused', 'r.json', 'paused', 3, 0)")
    old.close()

    upgraded = open_database(path)
    rows = upgraded.query("SELECT id, period_id, text FROM history_digests ORDER BY id")
    assert [tuple(row) for row in rows] == [(11, 1, "kept"), (12, None, "owner fixed this"),
                                            (20, 2, "only one")]
    with pytest.raises(Exception, match="UNIQUE"):
        upgraded.execute(digest.replace("?, 1, 'active'", "?, 1, 'staged'"), (30, 1, "x", 0))
    columns = {row["name"] for row in upgraded.query("PRAGMA table_info(imports)")}
    assert "distill_status" not in columns and "skipped_retention" not in columns
    period_columns = {row["name"] for row in upgraded.query("PRAGMA table_info(history_periods)")}
    assert {"settings_hash", "source_hash"} <= period_columns
    upgraded.close()


# ------------------------------------------------------------------- board

def test_board_folds_old_decisions_into_a_plan(db):
    sections = ('{"plans": [{"text": "BBQ"}], "decided": [{"text": "Splitwise"}, '
                '{"text": "Sam drives"}], "questions": [{"text": "Grill?"}]}')
    db.execute("INSERT INTO boards (chat_id, sections, updated_at, updated_by) "
               "VALUES (?, ?, 0, 't')", (BASIC, sections))
    board = BoardRepository(db).get(BASIC)
    assert [(i.text, i.done, i.details) for i in board.items("plans")] == [
        ("BBQ", False, []), ("Decided", True, ["Splitwise", "Sam drives"])]
    assert [i.text for i in board.items("questions")] == ["Grill?"]


def test_board_title_is_dropped_when_the_plans_change_without_one(db):
    boards = BoardRepository(db)
    board = boards.set_section(BASIC, "plans", ["Fri dinner", "Sat BBQ"], actor="t",
                               title="  📌 Dinner +  BBQ ")
    assert board.title == "Dinner + BBQ" and board.display_title == "Dinner + BBQ"
    assert boards.set_section(BASIC, "questions", ["Venue?"], actor="t").title == "Dinner + BBQ"
    # Same plans (a confirmation moves one to the end): the title still fits.
    board = boards.add_item(BASIC, "plans", "Fri dinner", done=True, details=["7pm"], actor="t")
    assert board.title == "Dinner + BBQ" and board.items("plans")[1].details == ["7pm"]
    board = boards.add_item(BASIC, "plans", "Sun brunch", actor="t")
    assert board.title is None and board.display_title == "Sat BBQ · Fri dinner · Sun brunch"
    long = boards.set_section(BASIC, "plans", [f"Plan number {i}" for i in range(9)], actor="t")
    assert long.display_title.endswith("…") and len(long.display_title) <= 61


def test_board_form_lines_round_trip():
    plans = parse_lines("[x] BBQ Sat\n  - 6pm\n  • East Coast\n- Book the pit\n\n", "plans")
    assert [(i.text, i.done, i.details) for i in plans] == [
        ("BBQ Sat", True, ["6pm", "East Coast"]), ("Book the pit", False, [])]
    assert format_lines(plans, "plans") == "[x] BBQ Sat\n  - 6pm\n  - East Coast\nBook the pit"
    questions = parse_lines("@Sam Tan: driving?\nGrill?", "questions")
    assert [(i.text, i.for_name) for i in questions] == [("driving?", "Sam Tan"), ("Grill?", None)]
    assert format_lines(questions, "questions") == "@Sam Tan: driving?\nGrill?"
