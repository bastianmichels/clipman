"""Dictations from Vocalinux (a speech-to-text app) in their own tab.

Vocalinux can keep its transcription history on disk, one JSON object per
line in ``~/.local/share/vocalinux/history.jsonl``::

    {"id": 12, "text": "...", "timestamp": "2026-10-14T19:58:26+00:00",
     "duration": 4.09, "language": "auto", "model": "..."}

``VocalinuxSource`` reads that file and keeps it current. Vocalinux
rewrites it atomically (a temporary file, then ``os.replace``), which
gives the path a new inode each time, so the source watches the folder,
not the file: a monitor on the file would go quiet after the first
update. A cheap stat check backs the monitor up.

When Vocalinux pastes a dictation through the clipboard, the copy should
not land in the history: the dictation is in the Vocalinux tab already.
A new clipboard text that, after normalising its whitespace, equals the
text of a dictation from the last few seconds is dropped
(``matches_recent``). The clipboard event can come before Vocalinux has
written the file, so each stored text is kept in mind for a moment
(``note_text_stored``); when a matching dictation shows up, the entry is
taken out of the history again.

Nothing here may take the daemon down: a missing, unreadable or odd file
only means an empty tab.
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 15  # seconds between a dictation and its clipboard copy
WINDOW_RANGE = (1, 120)
# How long a stored text waits for its dictation to appear in the file.
LATE_MATCH_SECONDS = 3.0
# A history larger than this is read from its end only.
MAX_FILE_SIZE = 8 * 1024 * 1024
_RELOAD_DELAY_MS = 100   # events come in bursts; reload once they rest
_PENDING_POLL_MS = 250   # stat checks while a stored text waits
_POLL_SECONDS = 30       # stat check in case the monitor misses something

_RELOAD_EVENTS = (
    Gio.FileMonitorEvent.CHANGED,
    Gio.FileMonitorEvent.CHANGES_DONE_HINT,
    Gio.FileMonitorEvent.CREATED,
    Gio.FileMonitorEvent.DELETED,
    Gio.FileMonitorEvent.RENAMED,
    Gio.FileMonitorEvent.MOVED_IN,
    Gio.FileMonitorEvent.MOVED_OUT,
)


def history_path():
    """Where Vocalinux keeps its transcription history."""
    return Path(GLib.get_user_data_dir()) / "vocalinux" / "history.jsonl"


def normalize(text):
    """``text`` trimmed, with each run of whitespace made one space."""
    return " ".join(str(text).split())


def parse_timestamp(value):
    """Seconds since the epoch for an ISO 8601 time, or None.

    A time without a zone is taken as UTC, like the ones Vocalinux writes.
    """
    if not isinstance(value, str) or not value:
        return None
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"  # fromisoformat takes Z from 3.11 on
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_line(line):
    """One dictation from a line of the history, or None if it is not one.

    The dictation is a dict with ``key`` (for de-duplication), ``text``,
    ``norm`` (normalised text), ``ts`` (epoch seconds or None) and
    ``duration`` (seconds or None). Unknown fields are ignored.
    """
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    text = obj.get("text")
    if not isinstance(text, str):
        return None
    norm = normalize(text)
    if not norm:
        return None
    ts = parse_timestamp(obj.get("timestamp"))
    duration = obj.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration < 0):
        duration = None
    ident = obj.get("id")
    if isinstance(ident, bool) or not isinstance(ident, (int, str)):
        key = ("text", ts, text)
    else:
        key = ("id", ident)
    return {"key": key, "text": text, "norm": norm, "ts": ts,
            "duration": duration}


def load(path):
    """The dictations in ``path``, newest first; None if it is missing or
    cannot be read.

    Broken lines are skipped. When an id occurs more than once, the last
    line wins.
    """
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > MAX_FILE_SIZE:
                logger.warning("The Vocalinux history is over %d bytes; "
                               "only its end is read", MAX_FILE_SIZE)
                f.seek(size - MAX_FILE_SIZE)
                raw = f.read(MAX_FILE_SIZE)
                raw = raw.split(b"\n", 1)[1] if b"\n" in raw else b""
            else:
                raw = f.read(MAX_FILE_SIZE)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("The Vocalinux history cannot be read: %s", exc)
        return None
    by_key = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        dictation = parse_line(line)
        if dictation is None:
            continue
        by_key.pop(dictation["key"], None)  # the later line wins
        by_key[dictation["key"]] = dictation
    # Newest first; the file order breaks ties (and orders undated ones).
    dictations = list(by_key.values())
    dictations.reverse()
    dictations.sort(key=lambda d: d["ts"] if d["ts"] is not None else -math.inf,
                    reverse=True)
    return dictations


def find_match(dictations, text, at, window):
    """The dictation whose text equals ``text`` (normalised) and whose
    time lies within ``window`` seconds of ``at``, or None."""
    norm = normalize(text)
    if not norm:
        return None
    for dictation in dictations:
        ts = dictation["ts"]
        if ts is not None and abs(at - ts) <= window \
                and dictation["norm"] == norm:
            return dictation
    return None


def _file_signature(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


class VocalinuxSource:
    """The Vocalinux dictations, kept up to date while enabled.

    ``listeners`` are called (on the main loop, without arguments) after
    the dictations changed or an entry was taken out of the history.
    """

    def __init__(self, db, path=None):
        self.db = db
        self.path = Path(path) if path is not None else history_path()
        self.dictations = []
        # False while the file is missing or unreadable (the tab says how
        # to turn it on).
        self.available = False
        self.listeners = []
        self._enabled = False
        self._signature = None
        self._monitor = None
        self._reload_id = 0
        self._poll_id = 0
        self._pending_poll_id = 0
        # Stored texts waiting for their dictation:
        # (entry id, text, normalised text, time.time(), time.monotonic()).
        self._pending = []

    # -- settings ------------------------------------------------------

    def enabled_in_settings(self):
        raw = self.db.get_setting("vocalinux_enabled", "true") or ""
        return raw.strip().lower() != "false"

    def window(self):
        """Seconds a clipboard text may lie from its dictation."""
        try:
            value = int(float(self.db.get_setting(
                "vocalinux_window", str(DEFAULT_WINDOW))))
        except (TypeError, ValueError):
            return DEFAULT_WINDOW
        low, high = WINDOW_RANGE
        return max(low, min(high, value))

    @property
    def enabled(self):
        return self._enabled

    # -- lifetime ------------------------------------------------------

    def start(self):
        """Start when the settings have it on."""
        self.set_enabled(self.enabled_in_settings())

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            self._reload()
            self._watch()
            self._poll_id = GLib.timeout_add_seconds(_POLL_SECONDS,
                                                     self._on_poll)
        else:
            self.stop()
        self._notify()

    def stop(self):
        self._enabled = False
        if self._monitor is not None:
            try:
                self._monitor.cancel()
            except Exception:
                logger.debug("cancelling the Vocalinux monitor failed",
                             exc_info=True)
            self._monitor = None
        for attr in ("_reload_id", "_poll_id", "_pending_poll_id"):
            source_id = getattr(self, attr)
            if source_id:
                GLib.source_remove(source_id)
                setattr(self, attr, 0)
        self._pending = []
        self.dictations = []
        self.available = False
        self._signature = None

    def _watch(self):
        """Watch the folder (see the module docstring for why)."""
        folder = Gio.File.new_for_path(str(self.path.parent))
        try:
            monitor = folder.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error as exc:
            # The periodic stat check still notices changes.
            logger.info("Cannot watch %s: %s", self.path.parent, exc.message)
            return
        monitor.connect("changed", self._on_folder_changed)
        self._monitor = monitor

    def _on_folder_changed(self, _monitor, file, other, event):
        try:
            if event not in _RELOAD_EVENTS:
                return
            names = {f.get_basename() for f in (file, other) if f is not None}
            if self.path.name in names and not self._reload_id:
                self._reload_id = GLib.timeout_add(_RELOAD_DELAY_MS,
                                                   self._on_reload_timeout)
        except Exception:
            logger.warning("Handling a Vocalinux file event failed",
                           exc_info=True)

    def _on_reload_timeout(self):
        self._reload_id = 0
        self._reload()
        return GLib.SOURCE_REMOVE

    def _on_poll(self):
        self.reload_if_changed()
        return GLib.SOURCE_CONTINUE

    # -- reading -------------------------------------------------------

    def reload_if_changed(self):
        """Read the file again if it changed since the last read."""
        if self._enabled and _file_signature(self.path) != self._signature:
            self._reload()

    def _reload(self):
        if not self._enabled:
            return
        try:
            self._signature = _file_signature(self.path)
            dictations = load(self.path)
            self.available = dictations is not None
            self.dictations = dictations or []
            self._match_pending()
        except Exception:
            logger.warning("Reading the Vocalinux history failed",
                           exc_info=True)
        self._notify()

    def _notify(self):
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                logger.warning("A Vocalinux listener failed", exc_info=True)

    # -- clipboard -----------------------------------------------------

    def matches_recent(self, text):
        """True when ``text`` is a dictation Vocalinux just made: the
        clipboard copy is not recorded."""
        if not self._enabled:
            return False
        # Vocalinux may have written the file a moment ago, before the
        # monitor event came through.
        self.reload_if_changed()
        return find_match(self.dictations, text, time.time(),
                          self.window()) is not None

    def note_text_stored(self, entry_id, text):
        """A clipboard text went into the history as ``entry_id``. If its
        dictation shows up in the next moments, it is taken out again."""
        if not self._enabled or not isinstance(entry_id, int) or entry_id <= 0:
            return
        norm = normalize(text)
        if not norm:
            return
        self._pending.append(
            (entry_id, text, norm, time.time(), time.monotonic()))
        if not self._pending_poll_id:
            self._pending_poll_id = GLib.timeout_add(_PENDING_POLL_MS,
                                                     self._on_pending_poll)

    def _on_pending_poll(self):
        self.reload_if_changed()
        self._drop_stale_pending()
        if self._pending and self._enabled:
            return GLib.SOURCE_CONTINUE
        self._pending_poll_id = 0
        return GLib.SOURCE_REMOVE

    def _drop_stale_pending(self):
        now = time.monotonic()
        self._pending = [p for p in self._pending
                         if now - p[4] <= LATE_MATCH_SECONDS]

    def _match_pending(self):
        """Take stored texts whose dictation has appeared out of the
        history."""
        self._drop_stale_pending()
        if not self._pending:
            return
        window = self.window()
        waiting = []
        for pending in self._pending:
            entry_id, text, norm, stored_at, _mono = pending
            if find_match(self.dictations, norm, stored_at, window) is None:
                waiting.append(pending)
                continue
            self._remove_entry(entry_id, text, stored_at)
        self._pending = waiting

    def _remove_entry(self, entry_id, text, stored_at):
        """Delete the entry the clipboard copy made, but only that: not a
        pin, and not an older entry the copy merely moved to the top."""
        try:
            entry = self.db.get_entry(entry_id)
            if (entry is None or entry.get("pinned")
                    or entry.get("content_text") != text
                    or (entry.get("created_at") or 0) < stored_at - 2.0):
                return
            self.db.delete_entry(entry_id)
        except Exception:
            logger.warning("Moving a dictation out of the history failed",
                           exc_info=True)
