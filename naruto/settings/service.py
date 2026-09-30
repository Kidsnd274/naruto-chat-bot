"""Settings backed by the database, cached in memory.

Reads come from the cache, so they are cheap enough to do on every message.
Every save refreshes the cache and notifies listeners, so changes apply
without a restart.
"""

from dataclasses import dataclass
import json
import logging
from typing import Any, Callable

from naruto.db.database import Database, now_ts
from naruto.settings.registry import REGISTRY, Setting, SettingError

logger = logging.getLogger(__name__)

Listener = Callable[[str, Any], None]


@dataclass
class HistoryEntry:
    id: int
    key: str
    old_value: Any  # None when the old value was the default
    new_value: Any  # None when reset to default
    old_is_default: bool
    new_is_default: bool
    changed_at: int
    changed_by: str


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SettingsService:
    def __init__(self, db: Database, registry: dict[str, Setting] | None = None):
        self.db = db
        self.registry = registry or REGISTRY
        self._values: dict[str, Any] = {}
        self._overridden: set[str] = set()
        self._listeners: list[Listener] = []
        self.reload()

    # ----------------------------------------------------------------- read

    def reload(self) -> None:
        values = {key: setting.default for key, setting in self.registry.items()}
        overridden = set()
        for row in self.db.query("SELECT key, value FROM settings"):
            setting = self.registry.get(row["key"])
            if setting is None:
                continue  # a setting removed from the code; kept for history
            try:
                values[row["key"]] = setting.validate(json.loads(row["value"]))
                overridden.add(row["key"])
            except (SettingError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Stored value for %s is invalid (%s); using the default.",
                    row["key"], exc,
                )
        self._values = values
        self._overridden = overridden

    def get(self, key: str) -> Any:
        try:
            return self._values[key]
        except KeyError:
            raise KeyError(f"unknown setting {key!r}") from None

    __getitem__ = get

    def is_default(self, key: str) -> bool:
        return key not in self._overridden

    def definition(self, key: str) -> Setting:
        try:
            return self.registry[key]
        except KeyError:
            raise SettingError(f"Unknown setting {key}.") from None

    # ---------------------------------------------------------------- write

    def set(self, key: str, value: Any, *, actor: str) -> Any:
        """Validate and store a value. Storing the default removes the
        override, so later default changes apply."""
        setting = self.definition(key)
        value = setting.validate(value)
        if value == setting.default:
            self._write(key, actor, reset=True)
        else:
            self._write(key, actor, value=value)
        return value

    def set_from_form(self, key: str, raw: str | None, *, actor: str) -> Any:
        return self.set(key, self.definition(key).parse_form(raw), actor=actor)

    def reset(self, key: str, *, actor: str) -> None:
        self.definition(key)
        self._write(key, actor, reset=True)

    def revert(self, key: str, *, actor: str) -> bool:
        """Go back to the value before the latest change. Returns False when
        there is no history to revert."""
        history = self.history(key, limit=1)
        if not history:
            return False
        last = history[0]
        if last.old_is_default:
            self.reset(key, actor=actor)
        else:
            self.set(key, last.old_value, actor=actor)
        return True

    def _write(self, key: str, actor: str, *, value: Any = None, reset: bool = False) -> None:
        """Store ``value`` (which may be None for nullable settings), or
        delete the override when ``reset``."""
        old_row = self.db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        old_json = old_row["value"] if old_row else None
        new_json = None if reset else _dump(value)
        if old_json == new_json:
            return
        ts = now_ts()
        with self.db.transaction():
            if new_json is None:
                self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self.db.execute(
                    "INSERT INTO settings (key, value, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, updated_at = excluded.updated_at, "
                    "updated_by = excluded.updated_by",
                    (key, new_json, ts, actor),
                )
            self.db.execute(
                "INSERT INTO settings_history (key, old_value, new_value, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, old_json, new_json, ts, actor),
            )
        self.reload()
        logger.info("Setting %s changed by %s", key, actor)
        new_value = self.get(key)
        for listener in list(self._listeners):
            try:
                listener(key, new_value)
            except Exception:
                logger.exception("Settings listener failed for %s", key)

    # -------------------------------------------------------------- history

    def history(self, key: str | None = None, *, limit: int = 20) -> list[HistoryEntry]:
        sql = "SELECT * FROM settings_history"
        params: tuple = ()
        if key is not None:
            sql += " WHERE key = ?"
            params = (key,)
        sql += " ORDER BY id DESC LIMIT ?"
        entries = []
        for row in self.db.query(sql, (*params, limit)):
            setting = self.registry.get(row["key"])
            default = setting.default if setting else None
            entries.append(HistoryEntry(
                id=row["id"],
                key=row["key"],
                old_value=default if row["old_value"] is None else json.loads(row["old_value"]),
                new_value=default if row["new_value"] is None else json.loads(row["new_value"]),
                old_is_default=row["old_value"] is None,
                new_is_default=row["new_value"] is None,
                changed_at=row["changed_at"],
                changed_by=row["changed_by"],
            ))
        return entries

    def last_changed(self, key: str) -> HistoryEntry | None:
        entries = self.history(key, limit=1)
        return entries[0] if entries else None

    # ------------------------------------------------------------ listeners

    def on_change(self, listener: Listener) -> None:
        self._listeners.append(listener)
