"""People shared across chats: accounts, names, aliases, merge and split,
and the migration from per-chat rosters."""

import pytest

from naruto.db import open_database
from naruto.db.database import Database
from naruto.db.members import MemberRepository
from naruto.db.migrations import MIGRATIONS
from naruto.db.people import PeopleRepository

A, B = -4001, -4002


@pytest.fixture
def people(db):
    return PeopleRepository(db)


@pytest.fixture
def members(db, people):
    return MemberRepository(db, people)


def test_accounts_create_people_and_names_resolve(people):
    alice = people.touch_live(7, "Alice Tan", "alice", seen_at=100)
    assert people.touch_live(7, "Alice T", "alice_t", seen_at=200) == alice
    person = people.get(alice)
    assert person.display_name == "Alice T" and person.username == "alice_t"
    assert person.accounts[0].first_seen_at == 100 and person.accounts[0].last_seen_at == 200

    people.touch_imported(7, "Ally (my contact)", seen_at=50)
    person = people.get(alice)
    assert person.accounts[0].export_name == "Ally (my contact)"
    assert person.accounts[0].first_seen_at == 50
    assert person.display_name == "Alice T"  # Telegram name until a name is chosen

    people.set_name(alice, "  Ally  ")
    assert people.get(alice).display_name == "Ally"
    people.set_name(alice, "")
    assert people.get(alice).name is None


def test_import_only_account_shows_export_name(people):
    person_id = people.touch_imported(9, "Wei from work", seen_at=10)
    assert people.get(person_id).display_name == "Wei from work"
    assert people.display_names([9, 12345]) == {9: "Wei from work"}


def test_aliases_are_shared_by_every_chat(members, people):
    members.upsert_live(A, 7, "Alice", "alice")
    members.upsert_live(B, 7, "Alice", "alice")
    assert members.add_alias(A, 7, "Ali")
    assert members.get(B, 7).aliases == ["Ali"]
    assert members.add_alias(B, 99, "nobody") is False  # not in that chat
    assert members.clear_aliases(B) == 1
    assert members.get(A, 7).aliases == []


def test_merge_moves_accounts_aliases_and_keeps_a_name(people):
    main = people.touch_live(7, "Alice", "alice", seen_at=300)
    second = people.touch_live(70, "Alice (work)", "alice_work", seen_at=100)
    people.add_alias(second, "Ali")
    people.set_name(second, "Alice Tan")

    merged = people.merge(second, main)
    assert merged.id == main and people.get(second) is None
    assert {a.user_id for a in merged.accounts} == {7, 70}
    assert merged.aliases == ["Ali"]
    assert merged.name == "Alice Tan"  # target had no name, so it takes the source's
    assert people.display_names([7, 70]) == {7: "Alice Tan", 70: "Alice Tan"}
    with pytest.raises(ValueError):
        people.merge(main, main)


def test_merging_people_keeps_their_memory_notes(services):
    people, notes = services.people, services.notes
    main = people.touch_live(7, "Alice", "alice")
    second = people.touch_live(70, "Alice (work)", "alice_work")
    kept = notes.add(A, "Alice is vegetarian", category="preference", person_id=main,
                     created_by="member", actor="t")
    moved = notes.add(A, "Alice's birthday is 3 March", category="date", person_id=second,
                      source_row_ids=[5], created_by="bot", actor="t")
    notes.set_locked(moved.id, True, actor="owner")
    before = notes.get(moved.id)

    people.merge(second, main)

    about_alice = notes.for_chat(A, person_id=main)
    assert {n.id for n in about_alice} == {kept.id, moved.id}
    after = notes.get(moved.id)
    assert (after.content, after.category, after.locked, after.source_row_ids,
            after.created_by, after.updated_at) == (before.content, before.category, True,
                                                     [5], "bot", before.updated_at)
    orphans = services.db.scalar(
        "SELECT COUNT(*) FROM memory_notes WHERE person_id = ?", (second,))
    history = services.db.scalar(
        "SELECT COUNT(*) FROM memory_note_history WHERE person_id = ?", (second,))
    assert orphans == 0 and history == 0


def test_moving_someones_last_account_moves_their_notes(services):
    people, notes = services.people, services.notes
    alice = people.touch_live(7, "Alice", "alice")
    bob = people.touch_live(8, "Bob", None)
    note = notes.add(A, "Bob is always late", category="running_joke", person_id=bob,
                     created_by="bot", actor="t")
    people.move_account(8, alice)  # Bob's only account: Bob is merged into Alice
    assert notes.get(note.id).person_id == alice
    assert [n.id for n in notes.for_chat(A, person_id=alice)] == [note.id]


def test_split_and_move_account(people):
    main = people.touch_live(7, "Alice", "alice")
    other = people.touch_live(70, "Alice work", None)
    people.merge(other, main)

    split = people.split(70)
    assert split.id != main and [a.user_id for a in split.accounts] == [70]
    with pytest.raises(ValueError, match="already a person of its own"):
        people.split(70)

    bob = people.touch_live(8, "Bob", None)
    moved = people.move_account(70, bob)
    assert {a.user_id for a in moved.accounts} == {8, 70}
    assert people.get(split.id) is None  # left with no accounts, so merged away


def test_find_by_username_and_listing(people, members, db):
    members.upsert_live(A, 7, "Alice", "Alice_X")
    members.upsert_live(B, 7, "Alice", "Alice_X")
    members.upsert_live(A, 8, "Bob", None)
    assert people.find_by_username("@alice_x").user_id == 7
    assert people.find_by_username("") is None
    db.execute("INSERT INTO messages (chat_id, origin_chat_id, source, message_id, sender_id, "
               "sender_name, date, created_at) VALUES (?, ?, 'live', 1, 7, 'Alice', 1, 1)", (A, A))
    listing = {s.person.display_name: s for s in people.all()}
    assert listing["Alice"].chat_count == 2 and listing["Alice"].message_count == 1
    assert [s.person.display_name for s in people.all("bob")] == ["Bob"]
    assert [s.person.display_name for s in people.all("alice_x")] == ["Alice"]
    alice = people.for_user(7)
    assert sorted(people.chats_of(alice.id)) == sorted([(A, 1), (B, 0)])


def test_roster_names_come_from_the_person(members, people):
    members.upsert_live(A, 7, "Alice", "alice", seen_at=100)
    members.upsert_imported(A, 7, "Ally (contact)", 50)
    members.upsert_imported(A, 9, "Wei", 60)
    member = members.get(A, 7)
    assert (member.display_name, member.export_name, member.source) == ("Alice", "Ally (contact)", "live")
    assert member.first_seen_at == 50
    people.set_name(member.person_id, "Ally")
    assert [m.display_name for m in members.list(A)] == ["Ally", "Wei"]
    assert members.find_user_id_by_username(A, "ALICE") == 7


def test_migration_from_per_chat_rosters(tmp_path):
    path = str(tmp_path / "v3.db")
    old = Database(path)
    old._conn.executescript("BEGIN; " + "\n".join(MIGRATIONS[:3]) + " PRAGMA user_version = 3; COMMIT;")
    old._conn.executescript(f"""
        INSERT INTO members VALUES ({A}, 7, 'Alice', 'alice', 0, 'live', 10, 300);
        INSERT INTO members VALUES ({B}, 7, 'Alice', 'alice', 0, 'live', 20, 200);
        INSERT INTO members VALUES ({A}, 9, 'Wei (contact)', NULL, 0, 'import', 5, 5);
        INSERT INTO member_aliases VALUES ({A}, 7, 'Ali', 1);
        INSERT INTO member_aliases VALUES ({B}, 7, 'Ali', 2);
        INSERT INTO member_aliases VALUES ({B}, 7, 'Tan', 3);
        INSERT INTO messages (chat_id, origin_chat_id, source, message_id, import_id, sender_id,
            sender_name, date, created_at) VALUES ({A}, {A}, 'import', 1, 1, 7, 'Ally (contact)', 1, 1);
    """)
    old.close()

    db = open_database(path)
    people = PeopleRepository(db)
    alice = people.for_user(7)
    assert alice.display_name == "Alice" and alice.username == "alice"
    assert alice.accounts[0].export_name == "Ally (contact)"
    assert sorted(alice.aliases) == ["Ali", "Tan"]
    assert alice.accounts[0].first_seen_at == 10
    wei = people.for_user(9)
    assert wei.display_name == "Wei (contact)" and wei.accounts[0].telegram_name is None
    columns = {row[1] for row in db.query("PRAGMA table_info(members)")}
    assert "display_name" not in columns
    assert db.scalar("SELECT name FROM sqlite_master WHERE name = 'member_aliases'") is None
    new_person = people.touch_live(55, "New", None)
    assert new_person not in (7, 9)
    db.close()
