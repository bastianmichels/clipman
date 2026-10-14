"""The Vocalinux tab's data source (clipman/vocalinux.py)."""

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from gi.repository import GLib

from clipman import vocalinux


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _line(ident, text, ts, **extra):
    return json.dumps({"id": ident, "text": text, "timestamp": _iso(ts),
                       "duration": 2.5, **extra})


def _write_atomic(path, lines):
    """Write like Vocalinux does: a temporary file, then os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _spin_until(condition, timeout=3.0):
    """Run the GLib main loop until ``condition()`` or the timeout."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        ctx.iteration(False)
        time.sleep(0.01)
    return condition()


class TestParsing(unittest.TestCase):

    def test_normalize(self):
        self.assertEqual(vocalinux.normalize("  Hallo \n\t Welt  "),
                         "Hallo Welt")

    def test_timestamps_compare_in_utc(self):
        self.assertEqual(vocalinux.parse_timestamp("2026-10-14T19:58:26+00:00"),
                         vocalinux.parse_timestamp("2026-10-14T21:58:26+02:00"))
        self.assertEqual(vocalinux.parse_timestamp("2026-10-14T19:58:26Z"),
                         vocalinux.parse_timestamp("2026-10-14T19:58:26"))
        for bad in (None, "", "yesterday", 17):
            self.assertIsNone(vocalinux.parse_timestamp(bad))

    def test_odd_lines_are_skipped(self):
        for line in ("not json", "[1, 2]", '{"id": 1}', '{"text": 5}',
                     '{"text": "   "}', "[" * 100000):
            self.assertIsNone(vocalinux.parse_line(line), line[:20])

    def test_unknown_and_wrong_fields_are_tolerated(self):
        d = vocalinux.parse_line(json.dumps({
            "id": {"x": 1}, "text": "Hallo", "timestamp": 3,
            "duration": "long", "speaker": "me"}))
        self.assertEqual(d["text"], "Hallo")
        self.assertIsNone(d["ts"])
        self.assertIsNone(d["duration"])


class TestLoad(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="clipman-vocalinux-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "history.jsonl"

    def test_missing_file(self):
        self.assertIsNone(vocalinux.load(self.path))

    def test_unreadable_file(self):
        self.path.mkdir()  # reading a folder fails with an OSError
        self.assertIsNone(vocalinux.load(self.path))

    def test_newest_first_deduplicated_broken_lines_skipped(self):
        now = time.time()
        self.path.write_text("\n".join([
            _line(1, "eins", now - 30),
            "{broken",
            _line(2, "zwei", now - 20),
            _line(1, "eins, korrigiert", now - 10),
            "",
            b"\xff\xfe".decode("latin-1"),
        ]), encoding="utf-8")
        texts = [d["text"] for d in vocalinux.load(self.path)]
        self.assertEqual(texts, ["eins, korrigiert", "zwei"])

    def test_large_file_reads_its_end(self):
        now = time.time()
        with patch.object(vocalinux, "MAX_FILE_SIZE", 200):
            self.path.write_text("\n".join(
                _line(i, f"Diktat {i}", now + i) for i in range(20)) + "\n")
            texts = [d["text"] for d in vocalinux.load(self.path)]
        self.assertIn("Diktat 19", texts)
        self.assertNotIn("Diktat 0", texts)

    def test_find_match(self):
        now = time.time()
        self.path.write_text(_line(1, "Okay, dann  probieren wir das.",
                                   now - 5))
        dictations = vocalinux.load(self.path)
        self.assertIsNotNone(vocalinux.find_match(
            dictations, " Okay, dann probieren\nwir das. ", now, 15))
        self.assertIsNone(vocalinux.find_match(
            dictations, "Okay, dann probieren wir das.", now + 20, 15))
        self.assertIsNone(vocalinux.find_match(dictations, "anders", now, 15))


class TestSource(unittest.TestCase):
    """VocalinuxSource with a real database and a real folder monitor."""

    def setUp(self):
        from clipman import database

        tmp = Path(tempfile.mkdtemp(prefix="clipman-vocalinux-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        data_dir = tmp / "clipman"
        for target, value in (
            ("clipman.database.DATA_DIR", data_dir),
            ("clipman.database.IMAGES_DIR", data_dir / "images"),
            ("clipman.database.DB_PATH", data_dir / "clipman.db"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = database.ClipboardDB()
        self.addCleanup(self.db.close)
        self.vdir = tmp / "vocalinux"
        self.vdir.mkdir()
        self.path = self.vdir / "history.jsonl"
        self.source = vocalinux.VocalinuxSource(self.db, self.path)
        self.addCleanup(self.source.stop)

        from clipman.clipboard_monitor import ClipboardMonitor
        self.monitor = ClipboardMonitor(self.db)
        self.monitor.text_filter = self.source.matches_recent
        self.monitor.on_text_stored = self.source.note_text_stored

    def _history(self):
        return [e["content_text"] for e in self.db.get_entries(limit=-1)]

    def test_missing_file_is_not_available(self):
        self.source.start()
        self.assertTrue(self.source.enabled)
        self.assertFalse(self.source.available)
        self.assertEqual(self.source.dictations, [])
        self.monitor.handle_new_text("ganz normal")
        self.assertEqual(self._history(), ["ganz normal"])

    def test_disabled_in_settings(self):
        self.db.set_setting("vocalinux_enabled", "false")
        _write_atomic(self.path, [_line(1, "Diktat", time.time())])
        self.source.start()
        self.assertFalse(self.source.enabled)
        self.monitor.handle_new_text("Diktat")
        self.assertEqual(self._history(), ["Diktat"])

    def test_atomic_rewrites_keep_arriving(self):
        """The folder is watched, so each os.replace is seen, not only
        the first."""
        self.source.start()
        now = time.time()
        lines = []
        for i in range(3):
            lines.append(_line(i, f"Diktat {i}", now + i))
            _write_atomic(self.path, lines)
            self.assertTrue(_spin_until(
                lambda n=i + 1: len(self.source.dictations) == n),
                f"update {i} not seen")
        self.assertTrue(self.source.available)
        self.assertEqual(self.source.dictations[0]["text"], "Diktat 2")

    def test_dictation_copy_stays_out_of_the_history(self):
        _write_atomic(self.path, [_line(1, "Hallo  Welt", time.time() - 2)])
        self.source.start()
        self.monitor.handle_new_text(" Hallo Welt\n")
        self.monitor.handle_new_text("etwas anderes")
        self.assertEqual(self._history(), ["etwas anderes"])

    def test_old_dictation_does_not_match(self):
        self.db.set_setting("vocalinux_window", "15")
        _write_atomic(self.path, [_line(1, "Hallo Welt", time.time() - 60)])
        self.source.start()
        self.monitor.handle_new_text("Hallo Welt")
        self.assertEqual(self._history(), ["Hallo Welt"])

    def test_copy_before_the_file_update_is_moved_out(self):
        """The clipboard event can come before Vocalinux writes its file."""
        self.source.start()
        changed = []
        self.source.listeners.append(lambda: changed.append(True))
        self.monitor.handle_new_text("Spät geschrieben")
        self.assertEqual(self._history(), ["Spät geschrieben"])
        _write_atomic(self.path, [_line(7, "Spät geschrieben", time.time())])
        self.assertTrue(_spin_until(lambda: self._history() == []))
        self.assertTrue(changed)
        self.assertEqual(self.source.dictations[0]["text"],
                         "Spät geschrieben")

    def test_late_match_spares_pins_and_older_entries(self):
        pinned = self.db.add_entry("text", content_text="angepinnt")
        self.db.toggle_pin(pinned)
        older = self.db.add_entry("text", content_text="schon da")
        self.db.conn.execute(
            "UPDATE entries SET created_at = 1 WHERE id = ?", (older,))
        self.db.conn.commit()
        self.source.start()
        self.monitor.handle_new_text("angepinnt")
        self.monitor.handle_new_text("schon da")
        now = time.time()
        _write_atomic(self.path, [_line(1, "angepinnt", now),
                                  _line(2, "schon da", now)])
        self.assertTrue(_spin_until(lambda: len(self.source.dictations) == 2))
        self.assertCountEqual(self._history(), ["angepinnt", "schon da"])

    def test_late_match_gives_up_after_a_while(self):
        self.source.start()
        self.monitor.handle_new_text("zu spät")
        with patch.object(vocalinux, "LATE_MATCH_SECONDS", 0):
            self.assertTrue(_spin_until(
                lambda: not self.source._pending_poll_id))
        _write_atomic(self.path, [_line(1, "zu spät", time.time())])
        self.assertTrue(_spin_until(lambda: self.source.dictations))
        self.assertEqual(self._history(), ["zu spät"])

    def test_broken_filter_never_loses_a_copy(self):
        def broken(_text):
            raise RuntimeError("boom")
        self.monitor.text_filter = broken
        self.monitor.on_text_stored = broken
        with self.assertLogs("clipman.clipboard_monitor", "WARNING"):
            self.monitor.handle_new_text("trotzdem")
        self.assertEqual(self._history(), ["trotzdem"])

    def test_switching_off_stops_everything(self):
        _write_atomic(self.path, [_line(1, "Diktat", time.time())])
        self.source.start()
        self.assertEqual(len(self.source.dictations), 1)
        self.source.set_enabled(False)
        self.assertEqual(self.source.dictations, [])
        self.assertIsNone(self.source._monitor)
        self.monitor.handle_new_text("Diktat")
        self.assertEqual(self._history(), ["Diktat"])

    def test_window_setting_is_clamped(self):
        for raw, want in (("5", 5), ("0", 1), ("9999", 120), ("x", 15)):
            self.db.set_setting("vocalinux_window", raw)
            self.assertEqual(self.source.window(), want)


if __name__ == "__main__":
    unittest.main()
