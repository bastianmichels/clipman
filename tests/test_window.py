"""Smoke tests for the GTK 4 + libadwaita port.

Tests skip cleanly when GTK / libadwaita aren't importable (e.g. on a
headless CI runner without the system packages installed). When they
ARE importable, the tests assert that:

- the new modules import without raising,
- ``ClipmanWindow`` boots with an in-memory DB,
- ``ClipmanPreferences`` and ``SnippetsDialog`` can be constructed,
- ``render_edge_state`` returns a widget for every one of the 16
  state ids declared in ``clipman.edge_states.STATES``.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# tests/__init__.py chooses the display (a private one, or none) and the
# GTK environment before this module is imported.

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: F401
    _HAS_GTK = True
except (ImportError, ValueError, AttributeError, RuntimeError):
    # ImportError: pygobject / gi missing on the runner.
    # ValueError: gi present but the GTK4 / Adw1 typelibs aren't.
    # AttributeError: a stub ``gi`` shim (some CI sandboxes ship one)
    # lacks ``require_version`` — same outcome: no widgets available.
    # RuntimeError: re-raised by clipman.window's own guard (kept here
    # so the import chain raises a single, predictable class).
    _HAS_GTK = False
    Adw = None  # type: ignore[assignment]
    Gio = None  # type: ignore[assignment]
    Gtk = None  # type: ignore[assignment]

_ADW_INIT_OK = False
if _HAS_GTK:
    try:
        Adw.init()
        # Adw.init() succeeds without a display too, and the first widget
        # then crashes the whole run. Without one, skip the widget tests.
        _ADW_INIT_OK = Gdk.Display.get_default() is not None
    except Exception:
        _ADW_INIT_OK = False

# CI sets ``CLIPMAN_REQUIRE_GTK4=1`` so an apt-package rename or a
# missing typelib turns into a HARD failure instead of a silent skip.
# Locally the variable stays unset, so contributors without GTK4
# installed still get the rest of the test suite passing.
if os.environ.get("CLIPMAN_REQUIRE_GTK4") == "1" and not _HAS_GTK:
    raise RuntimeError(
        "CLIPMAN_REQUIRE_GTK4=1 but GTK 4 + libadwaita are not "
        "importable in this environment. Install gir1.2-gtk-4.0, "
        "gir1.2-adw-1 and libadwaita-1-0."
    )
if os.environ.get("CLIPMAN_REQUIRE_GTK4") == "1" and not _ADW_INIT_OK:
    raise RuntimeError(
        "CLIPMAN_REQUIRE_GTK4=1 but there is no private display for the "
        "widget tests. Run them with scripts/dev.sh test, which starts "
        "one with xvfb-run (tests/__init__.py explains the rules)."
    )


# On the base class, so every widget test class skips without GTK or a
# display, including one that forgets its own decorator.
@unittest.skipUnless(_HAS_GTK and _ADW_INIT_OK,
                     "GTK 4 + libadwaita or a private display not available")
class _WidgetTestCase(unittest.TestCase):
    """Shared fixtures for the tests that build real widgets.

    Provides a temp-directory database, an application that has already
    emitted ``startup``, and a guard that stops any test starting a real
    process.
    """

    def setUp(self):
        # Nothing here may touch the developer's desktop. An unstubbed
        # edge-state action used to run xdg-open and open a file manager
        # on a temp directory the test had already deleted, and another
        # asked systemd to restart the real daemon. Patch the clipman
        # functions that reach outside the process, not subprocess
        # itself: every module shares one subprocess module object, so
        # patching Popen there also breaks check_output in keybindings.
        self._shell_guards = []
        for target in ("clipman.preferences.open_url",
                       "clipman.window.ClipmanWindow._reveal_path",
                       "clipman.window.ClipmanWindow._action_restart_daemon",
                       "clipman.window.ClipmanWindow._wl_copy"):
            patcher = patch(target)
            patcher.start()
            self._shell_guards.append(patcher)
            self.addCleanup(patcher.stop)

    def _allow_shell_out(self):
        """Drop the guard for the tests that check these two functions."""
        while self._shell_guards:
            self._shell_guards.pop().stop()

    # Registering exports the application on the session bus, and the
    # same object path cannot be exported twice in one process, so every
    # app gets its own suffix.
    _app_serial = itertools.count()

    def _make_app(self, name):
        """An Adw.Application that has emitted startup.

        Adding a window to an unregistered GApplication logs "New
        application windows must be added after the
        GApplication::startup signal has been emitted" on every
        construction.
        """
        app = Adw.Application(
            application_id=f"{name}{next(self._app_serial)}",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        app.register(None)
        self.addCleanup(app.quit)
        return app

    def _make_db(self, prefix="clipman-test-"):
        # Use a temp dir so the test never touches the real DB.
        # Mirrors the pattern in test_database.py: patch the module-level
        # paths (do NOT mutate them — that leaks across tests) and register
        # an addCleanup for each patch + the tmpdir.
        from clipman import database

        tmp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        data_dir = Path(tmp) / "clipman"
        images_dir = data_dir / "images"
        db_path = data_dir / "clipman.db"
        for target, value in (
            ("clipman.database.DATA_DIR", data_dir),
            ("clipman.database.IMAGES_DIR", images_dir),
            ("clipman.database.DB_PATH", db_path),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db = database.ClipboardDB()
        self.addCleanup(db.close)
        return db


@unittest.skipUnless(_HAS_GTK and _ADW_INIT_OK,
                     "GTK 4 + libadwaita not available")
class TestEdgeStates(_WidgetTestCase):
    """Every declared state must map to a renderable widget."""

    EXPECTED_IDS = {
        "populated", "empty", "no-snippets-yet", "no-results",
        "no-image-search", "no-pins-yet", "first-run",
        "incognito-on", "sensitive-shown", "sensitive-cleared",
        "extension-missing", "backup-failed", "restore-failed",
        "network-error", "db-locked", "paused", "paste-target-missing",
        "paste-failed", "history-too-large", "clipboard-blocked",
        "watcher-crashed", "shortcut-failed",
    }

    def test_state_id_inventory(self):
        """Lock the inventory — adding a state requires updating this set.

        Renamed from ``test_all_states_declared`` to make the intent
        (inventory lock-in, not a smoke test) explicit. Touching this
        list should be a deliberate, reviewer-visible action.
        """
        from clipman.edge_states import STATES
        self.assertEqual(set(STATES.keys()), self.EXPECTED_IDS)
        self.assertEqual(len(STATES), 22)

    def test_render_each_state_returns_widget(self):
        from clipman.edge_states import STATES, render_edge_state
        for state_id in STATES:
            with self.subTest(state_id=state_id):
                widget = render_edge_state(state_id)
                self.assertIsNotNone(widget, state_id)
                # Every rendered state carries its spec back for caller
                # introspection (used by the action-id dispatch).
                self.assertTrue(hasattr(widget, "state_spec"))
                self.assertEqual(widget.state_spec.id, state_id)
                # ``kind`` -> widget class invariant. The renderer
                # dispatches on ``spec.kind`` and the host window relies
                # on the resulting type to decide where to mount it.
                spec = widget.state_spec
                if spec.kind == "banner":
                    # Custom banner row (icon · title/desc · action · X) —
                    # Adw.Banner can't show a desc line or dismiss button.
                    self.assertTrue(widget.has_css_class("edge-banner"))
                elif spec.kind == "alertdialog":
                    self.assertIsInstance(widget, Adw.AlertDialog)
                else:
                    self.assertEqual(spec.kind, "statuspage")
                    self.assertIsInstance(widget, Adw.StatusPage)

    def test_problem_banner_offers_the_fix_and_cannot_be_dismissed(self):
        """A problem that stops recording stays until it is solved."""
        from gi.repository import Gtk

        from clipman.edge_states import STATES, render_problem_banner

        for state_id in ("first-run", "extension-missing",
                         "watcher-crashed", "clipboard-blocked"):
            with self.subTest(state_id=state_id):
                actions = []
                banner = render_problem_banner(
                    state_id, on_action=actions.append
                )
                self.assertTrue(banner.has_css_class("edge-banner"))
                self.assertEqual(banner.state_spec.id, state_id)
                buttons = []
                child = banner.get_first_child()
                while child is not None:
                    if isinstance(child, Gtk.Button):
                        buttons.append(child)
                    child = child.get_next_sibling()
                # The fix only: no dismiss X.
                self.assertEqual(len(buttons), 1)
                buttons[0].emit("clicked")
                self.assertEqual(
                    actions, [STATES[state_id].primary_action[1]]
                )

    def test_unknown_state_falls_back_to_empty(self):
        from clipman.edge_states import render_edge_state
        widget = render_edge_state("does-not-exist")
        self.assertIsNotNone(widget)
        # The fallback must be the ``empty`` spec specifically — the
        # popup relies on this so a typo in window.py doesn't leak a
        # random spec into the empty slot.
        self.assertEqual(widget.state_spec.id, "empty")

    def test_on_edge_action_covers_states_contract(self):
        """Every action_id declared by STATES must have a dispatch handler.

        Previously the renderer wired buttons to action_ids the host
        window didn't know about (9 of 15 ids in STATES were unwired),
        so every Retry / Open / Pick-another button in production fell
        through to ``logger.warning`` and silently no-op'd. This test
        is the contract that catches the next regression: introduce a
        new state in edge_states.STATES without a handler in window.py
        and CI fails here.
        """
        from clipman.edge_states import STATES
        from clipman.window import ClipmanWindow

        declared_ids: set[str] = set()
        for spec in STATES.values():
            for slot in (spec.primary_action, spec.secondary_action):
                if slot is not None:
                    declared_ids.add(slot[1])

        # Class-level frozenset is the source of truth for the
        # dispatcher; the property builds a dict with these keys.
        missing = declared_ids - ClipmanWindow._EDGE_ACTION_IDS
        self.assertFalse(
            missing,
            f"action_ids declared in STATES but not handled by "
            f"ClipmanWindow._on_edge_action: {sorted(missing)}",
        )

    def test_on_edge_action_dispatch_table_matches_declared_ids(self):
        """The runtime dispatch dict keys must equal the class-level set.

        Guards against the property and the frozenset drifting apart:
        if a maintainer adds a handler to the dict without updating the
        set (or vice versa), CI fails before the no-op regression can
        reach production.
        """
        from clipman import database
        from clipman.window import ClipmanWindow

        tmp = tempfile.mkdtemp(prefix="clipman-test-dispatch-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        data_dir = Path(tmp) / "clipman"
        for target, value in (
            ("clipman.database.DATA_DIR", data_dir),
            ("clipman.database.IMAGES_DIR", data_dir / "images"),
            ("clipman.database.DB_PATH", data_dir / "clipman.db"),
        ):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

        db = database.ClipboardDB()
        self.addCleanup(db.close)
        app = self._make_app("com.clipman.TestDispatch")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        self.assertEqual(
            set(window._edge_action_dispatch.keys()),
            set(ClipmanWindow._EDGE_ACTION_IDS),
        )


@unittest.skipUnless(_HAS_GTK and _ADW_INIT_OK,
                     "GTK 4 + libadwaita not available")
class TestWindowConstruction(_WidgetTestCase):
    """ClipmanWindow + ClipmanPreferences + SnippetsDialog all build."""

    def test_window_boots(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.Test")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        self.assertIsNotNone(window)
        # Public interface required by dbus_service stays intact.
        self.assertTrue(hasattr(window, "toggle"))
        self.assertTrue(hasattr(window, "refresh"))
        self.assertTrue(hasattr(window, "refresh_update_banner"))

    def test_incognito_toggle_syncs_monitor_button_and_pill(self):
        """set_incognito drives the monitor, header button and footer pill.

        The bulky in-list "incognito-on" banner was replaced by the footer
        status pill (Recording/Paused). set_incognito is also the launch-time
        entry point for the incognito_on_launch setting.
        """
        from unittest.mock import MagicMock

        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestIncognito")
        monitor = MagicMock()
        window = ClipmanWindow(application=app, db=db, monitor=monitor)

        window.set_incognito(True)
        self.assertTrue(window._incognito_btn.get_active())
        monitor.set_incognito.assert_called_with(True)
        self.assertEqual(window._recording_label.get_text(), "Paused")
        self.assertTrue(window._recording_pill.has_css_class("paused"))

        window.set_incognito(False)
        self.assertFalse(window._incognito_btn.get_active())
        monitor.set_incognito.assert_called_with(False)
        self.assertEqual(window._recording_label.get_text(), "Recording")
        self.assertFalse(window._recording_pill.has_css_class("paused"))

    def test_incognito_header_toggle_persists(self):
        """The header incognito toggle persists across restarts.

        Regression: only the Privacy switch persisted incognito, so a
        daemon restart silently re-enabled incognito the user had turned
        off in the header — copies were dropped with no visible cause.
        """
        from unittest.mock import MagicMock

        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestIncogPersist")
        window = ClipmanWindow(application=app, db=db, monitor=MagicMock())

        window._incognito_btn.set_active(True)
        self.assertEqual(db.get_setting("incognito_on_launch"), "true")
        window._incognito_btn.set_active(False)
        self.assertEqual(db.get_setting("incognito_on_launch"), "false")

    def test_preferences_window_constructs(self):
        from clipman.preferences import ClipmanPreferences
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.Test")
        parent = ClipmanWindow(application=app, db=db, monitor=None)
        prefs = ClipmanPreferences(db, parent, on_setting_changed=None)
        self.assertIsNotNone(prefs)

    def test_catppuccin_palette_is_optional(self):
        """use_catppuccin gates whether the forced @-token palette is applied
        (off = follow the system GNOME theme/accent)."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestCatppuccin")

        w_on = ClipmanWindow(application=app, db=db, monitor=None)
        self.assertTrue(w_on._use_catppuccin)  # default on
        self.assertIn("@define-color", w_on._catppuccin_palette_block())

        db.set_setting("use_catppuccin", "false")
        w_off = ClipmanWindow(application=app, db=db, monitor=None)
        self.assertFalse(w_off._use_catppuccin)
        # hot-reload back on without raising
        w_off._on_setting_changed("use_catppuccin", "true")
        self.assertTrue(w_off._use_catppuccin)

    def test_type_icons_resolve(self):
        """Every row type icon must exist so no broken-icon placeholder shows."""
        from gi.repository import Gdk, Gtk

        from clipman.window import ROW_TYPE_ICONS

        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for name in ROW_TYPE_ICONS.values():
            self.assertTrue(theme.has_icon(name), name)
    def test_preferences_is_in_surface_dialog(self):
        """Preferences must be an Adw.Dialog (in-surface), not a top-level.

        Regression guard: as a top-level Adw.PreferencesWindow it opened
        behind the popup on Wayland and looked unresponsive.
        """
        from gi.repository import Gtk

        from clipman.preferences import ClipmanPreferences

        db = self._make_db()
        prefs = ClipmanPreferences(db, None, on_setting_changed=None)
        self.assertIsInstance(prefs, Adw.Dialog)
        self.assertNotIsInstance(prefs, Gtk.Window)

    def test_dismiss_on_focus_loss(self):
        """notify::is-active handler hides the popup when it loses focus,
        unless an in-app child dialog is open (Win+V click-outside dismiss)."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestDismiss")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        # Headless: the window is never compositor-active, so is-active is
        # False — exactly the "lost focus" condition.
        self.assertFalse(window.get_property("is-active"))

        window.set_visible(True)
        window._child_dialog = None
        window._on_active_changed()
        self.assertFalse(window.get_visible())  # dismissed on focus loss

        # A mapped child dialog suppresses the dismiss.
        window.set_visible(True)
        child = MagicMock()
        child.get_mapped.return_value = True
        window._child_dialog = child
        window._on_active_changed()
        self.assertTrue(window.get_visible())  # child open -> stay put

    def test_moving_or_resizing_does_not_dismiss(self):
        """Dragging the header bar or an edge hands the pointer to the
        compositor, which takes the focus away until the drag ends: the
        popup vanished instead of moving."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestDrag")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.set_visible(True)
        self.addCleanup(window.set_visible, False)

        def pointer(kind):
            # As PyGObject delivers the signal: no event object, the
            # controller knows the current one.
            controller = MagicMock()
            controller.get_current_event.return_value.get_event_type.return_value = kind
            self.assertFalse(window._on_pointer_event(controller, None))

        pointer(Gdk.EventType.BUTTON_PRESS)
        window._on_active_changed()  # the drag starts: focus goes away
        self.assertTrue(window.get_visible())
        # The focus comes back when the drag ends; the release never
        # reaches the popup.
        with patch.object(window, "get_property", return_value=True):
            window._on_active_changed()
        self.assertFalse(window._button_down)
        # Now a click on another window dismisses as before.
        window._on_active_changed()
        self.assertFalse(window.get_visible())

    def test_a_finished_click_still_allows_dismiss(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestClick")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.set_visible(True)
        for kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
            event = MagicMock()
            event.get_event_type.return_value = kind
            window._on_pointer_event(MagicMock(), event)
        # No event at all (nothing current) is ignored, never an error.
        idle = MagicMock()
        idle.get_current_event.return_value = None
        self.assertFalse(window._on_pointer_event(idle, None))
        window._on_active_changed()
        self.assertFalse(window.get_visible())

    def test_hide_cancels_timer_and_closes_child(self):
        """_hide() must reset the cursor timer and force-close any child so
        the dismiss guard can't latch (hiding never fires a dialog 'closed')."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestHide")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.set_visible(True)
        window._cursor_move_id = 0
        child = MagicMock()
        child.get_mapped.return_value = True
        window._child_dialog = child
        window._hide()
        self.assertFalse(window.get_visible())
        self.assertIsNone(window._child_dialog)     # ref cleared (no latch)
        child.force_close.assert_called_once()      # stale dialog closed
        self.assertFalse(window._child_is_open())

    def test_popup_is_resizable_and_opens_at_the_saved_size(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        db.set_setting("window_width", "500")
        db.set_setting("window_height", "450")
        app = self._make_app("com.clipman.TestSize")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        self.assertTrue(window.get_resizable())
        self.assertEqual(window.get_default_size(), (500, 450))

    def test_hiding_saves_the_size(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestSaveSize")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.set_visible(True)
        with patch.object(window, "get_width", return_value=510), \
                patch.object(window, "get_height", return_value=470):
            window._hide()
        self.assertEqual(db.get_setting("window_width"), "510")
        self.assertEqual(db.get_setting("window_height"), "470")

    def test_placement_asks_the_extension_with_the_offset(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        db.set_setting("popup_offset_x", "-40")
        db.set_setting("popup_offset_y", "25")
        app = self._make_app("com.clipman.TestPlace")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.set_visible(True)
        self.addCleanup(window.set_visible, False)
        with patch.object(window, "_call_extension") as call:
            window._move_to_cursor()
        call.assert_called_once_with(
            "PlaceWindow", "ssii", ("Clipman", "pointer", -40, 25),
            on_error=window._place_with_old_extension)

    def test_an_old_extension_still_opens_at_the_pointer(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestOldExt")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.set_visible(True)
        self.addCleanup(window.set_visible, False)
        unknown = MagicMock()
        unknown.get_dbus_name.return_value = (
            "org.freedesktop.DBus.Error.UnknownMethod")
        denied = MagicMock()
        denied.get_dbus_name.return_value = (
            "org.freedesktop.DBus.Error.AccessDenied")
        with patch.object(window, "_call_extension") as call:
            window._place_with_old_extension(denied)
            call.assert_not_called()
            window._place_with_old_extension(unknown)
            call.assert_called_once_with("MoveWindowToCursor", "s", ("Clipman",))

    def test_paste_calls_to_the_extension_time_out_quickly(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestShortTimeout")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        bus = MagicMock()
        with patch("dbus.SessionBus", return_value=bus), \
                patch("dbus.Interface") as interface:
            iface = window._shell_extension_iface()
            iface.SimulatePaste("ctrl-v")
        self.assertEqual(bus.get_object.call_args.kwargs, {"introspect": False})
        interface.return_value.SimulatePaste.assert_called_once_with(
            "ctrl-v", timeout=2.0)

    def test_extension_calls_never_block(self):
        """The call goes out with call_async and a timeout, and a missing
        bus only reaches the error handler."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestAsync")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        bus = MagicMock()
        with patch("dbus.SessionBus", return_value=bus):
            window._call_extension("PlaceWindow", "ssii", ("Clipman", "pointer", 0, 0))
        args, kwargs = bus.call_async.call_args
        self.assertEqual(args[3:6], ("PlaceWindow", "ssii", ("Clipman", "pointer", 0, 0)))
        self.assertEqual(kwargs, {"timeout": 2.0})
        errors = []
        with patch("dbus.SessionBus", side_effect=RuntimeError("no bus")):
            window._call_extension("PlaceWindow", "ssii", (), on_error=errors.append)
        self.assertEqual(len(errors), 1)

    def test_a_reported_move_is_remembered(self):
        from clipman.dbus_service import ClipmanDBusService
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestReport")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        service = MagicMock(window=window)
        ClipmanDBusService.ReportWindowPosition(service, 500, 300, 500, 300, 450, 200)
        self.assertEqual(window._placement.request(), ("pointer", -50, -100))

    def test_move_to_cursor_guarded_when_hidden(self):
        """A stale _move_to_cursor timer must not re-activate a hidden popup."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestCursor")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.set_visible(False)
        # Returns False (removes source) and no-ops because it's hidden.
        self.assertFalse(window._move_to_cursor())
        self.assertEqual(window._cursor_move_id, 0)

    def test_paste_prefers_shell_injection(self):
        """On GNOME the Shell injects the keystroke (wtype can't on Mutter),
        so _dispatch_paste routes through the extension and must NOT also
        fire the wtype fallback when the Shell path succeeds."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestPasteShell")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window._paste_via_shell = lambda mode: True  # extension present

        with patch("clipman.window.GLib.timeout_add") as timeout_add:
            window._dispatch_paste()

        self.assertFalse(timeout_add.called)  # no wtype fallback scheduled

    def test_paste_falls_back_to_wtype_without_shell(self):
        """With no extension reachable, _dispatch_paste falls back to the
        wtype/ydotool keystroke (works on non-GNOME compositors)."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestPasteWtype")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window._paste_via_shell = lambda mode: False  # no extension

        with patch("clipman.window.GLib.timeout_add") as timeout_add:
            window._dispatch_paste()

        self.assertTrue(timeout_add.called)
        _delay, callback = timeout_add.call_args[0][:2]
        self.assertEqual(callback, window._simulate_paste)

    def test_paste_via_shell_restores_focus_then_injects(self):
        """The Shell path must restore focus FIRST, then inject the keystroke
        (deferred so focus can settle) — order matters or Ctrl+V misses."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestPasteOrder")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        calls = []
        fake_iface = MagicMock()
        fake_iface.RestorePreviousFocus.side_effect = (
            lambda: calls.append("focus")
        )
        fake_iface.SimulatePaste.side_effect = (
            lambda m: calls.append(("paste", m))
        )
        window._shell_extension_iface = lambda: fake_iface

        with patch("clipman.window.GLib.timeout_add") as timeout_add:
            result = window._paste_via_shell("auto")

        self.assertTrue(result)
        self.assertEqual(calls, ["focus"])  # focus restored synchronously
        # The keystroke is deferred via a timer; firing it injects via Shell.
        self.assertTrue(timeout_add.called)
        _delay, callback = timeout_add.call_args[0][:2]
        callback()
        self.assertEqual(calls, ["focus", ("paste", "auto")])

    def test_paste_via_shell_returns_false_without_extension(self):
        """No extension -> _paste_via_shell reports failure (so paste can
        fall back to wtype) and never raises."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestNoExt")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window._shell_extension_iface = lambda: None
        self.assertFalse(window._paste_via_shell("auto"))

    def test_paste_via_shell_ignores_focus_restore_failure(self):
        """A failed focus restore must not abort the paste."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestFocusFail")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        fake_iface = MagicMock()
        fake_iface.RestorePreviousFocus.side_effect = RuntimeError("no window")
        window._shell_extension_iface = lambda: fake_iface

        with patch("clipman.window.GLib.timeout_add") as timeout_add:
            self.assertTrue(window._paste_via_shell("auto"))
        timeout_add.call_args[0][1]()
        fake_iface.SimulatePaste.assert_called_once_with("auto")

    def test_paste_via_shell_retries_without_mode(self):
        """An old extension that rejects the mode gets the no-argument call."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestPasteRetry")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        calls = []

        def simulate(*args):
            calls.append(args)
            if args:
                raise RuntimeError("UnknownMethod")

        fake_iface = MagicMock()
        fake_iface.SimulatePaste.side_effect = simulate
        window._shell_extension_iface = lambda: fake_iface
        window._show_edge_state = MagicMock()

        with patch("clipman.window.GLib.timeout_add") as timeout_add:
            window._paste_via_shell("ctrl-v")
        timeout_add.call_args[0][1]()
        self.assertEqual(calls, [("ctrl-v",), ()])
        window._show_edge_state.assert_not_called()

    def test_paste_via_shell_shows_dialog_when_both_calls_fail(self):
        """No wtype fallback on GNOME: the popup comes back with a dialog."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestPasteFailed")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        fake_iface = MagicMock()
        fake_iface.SimulatePaste.side_effect = RuntimeError("refused")
        window._shell_extension_iface = lambda: fake_iface
        window._present_focused = MagicMock()
        window._show_edge_state = MagicMock()
        window._simulate_paste = MagicMock()

        with patch("clipman.window.GLib.timeout_add") as timeout_add:
            self.assertTrue(window._paste_via_shell("auto"))
        timeout_add.call_args[0][1]()
        window._present_focused.assert_called_once()
        window._show_edge_state.assert_called_once_with("paste-failed")
        window._simulate_paste.assert_not_called()

    def _problem_window(self, entries=()):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        for text in entries:
            db.add_entry("text", content_text=text)
        app = self._make_app("com.clipman.TestProblem")
        return ClipmanWindow(application=app, db=db, monitor=None)

    def test_empty_history_shows_why_nothing_is_recorded(self):
        window = self._problem_window()
        window.set_recording_problem("first-run")
        window.refresh()
        self.assertEqual(window._list_stack.get_visible_child_name(), "empty")
        self.assertEqual(
            window._current_edge_widget.state_spec.id, "first-run"
        )
        # The page says it already, and the pill would claim "Recording".
        self.assertFalse(window._recording_banner_slot.get_visible())
        self.assertFalse(window._recording_pill.get_visible())

    def test_recording_problem_stays_above_the_history(self):
        """UI-11: the next refresh (opening the popup) used to hide it."""
        window = self._problem_window(["an older clip"])
        window.set_recording_problem("watcher-crashed")
        window.refresh()
        window.refresh()
        self.assertEqual(window._list_stack.get_visible_child_name(), "list")
        self.assertTrue(window._recording_banner_slot.get_visible())
        self.assertEqual(
            window._recording_banner.state_spec.id, "watcher-crashed"
        )
        self.assertFalse(window._recording_pill.get_visible())

    def test_solved_recording_problem_goes_away(self):
        window = self._problem_window()
        window.set_recording_problem("first-run")
        window.set_recording_problem(None)
        window.refresh()
        self.assertIsNone(window._recording_banner)
        self.assertEqual(window._current_edge_widget.state_spec.id, "empty")
        self.assertTrue(window._recording_pill.get_visible())

    def test_open_popup_shows_a_new_problem_at_once(self):
        window = self._problem_window()
        window.get_visible = lambda: True
        window.refresh = MagicMock()
        window.set_recording_problem("first-run")
        window.refresh.assert_called_once_with()
        window.set_recording_problem("first-run")
        window.refresh.assert_called_once_with()  # no change, no refresh

    def _fake_paste_tools(self, **exit_codes):
        """Put only fake paste tools on PATH; return their call log."""
        bindir = Path(tempfile.mkdtemp(prefix="clipman-tools-"))
        self.addCleanup(shutil.rmtree, bindir, ignore_errors=True)
        calls = bindir / "calls"
        for tool, code in exit_codes.items():
            script = bindir / tool
            script.write_text(
                f'#!/bin/sh\necho {tool} >> "{calls}"\nexit {code}\n'
            )
            script.chmod(0o700)
        patcher = patch.dict(os.environ, {"PATH": str(bindir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _paste_with_tools(self, problem=None, **exit_codes):
        calls = self._fake_paste_tools(**exit_codes)
        window = self._problem_window()
        window.set_recording_problem(problem)
        window._present_focused = MagicMock()
        window._show_edge_state = MagicMock()
        window._simulate_paste()
        tools = calls.read_text().split() if calls.exists() else []
        return window, tools

    def test_paste_tool_that_works_needs_no_dialog(self):
        window, tools = self._paste_with_tools(wtype=0, ydotool=0)
        self.assertEqual(tools, ["wtype"])
        window._show_edge_state.assert_not_called()

    def test_paste_tries_the_next_tool_after_a_failure(self):
        window, tools = self._paste_with_tools(wtype=1, ydotool=0)
        self.assertEqual(tools, ["wtype", "ydotool"])
        window._show_edge_state.assert_not_called()

    def test_failed_paste_is_not_silent(self):
        """UI-9: wtype exits 1 on GNOME; that used to count as a paste."""
        window, tools = self._paste_with_tools(wtype=1)
        self.assertEqual(tools, ["wtype"])
        window._present_focused.assert_called_once_with()
        window._show_edge_state.assert_called_once_with(
            "paste-target-missing"
        )

    def test_failed_paste_without_the_extension_names_it(self):
        for problem in ("first-run", "extension-missing"):
            with self.subTest(problem=problem):
                window, _tools = self._paste_with_tools(problem, wtype=1)
                window._show_edge_state.assert_called_once_with(
                    "paste-failed"
                )

    def test_paste_without_any_tool_says_so(self):
        window, tools = self._paste_with_tools()
        self.assertEqual(tools, [])
        window._show_edge_state.assert_called_once_with(
            "paste-target-missing"
        )

    def test_tabs_query_the_database(self):
        """History lists pins by time; the Pinned tab filters in SQL."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestTabs")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.db = MagicMock(wraps=db)

        window.refresh()
        window.db.get_entries.assert_called_with(
            limit=200, pinned_only=False, pinned_first=False)

        window.select_tab("pinned")
        window.db.get_entries.assert_called_with(
            limit=200, pinned_only=True, pinned_first=False)

        window._search_query = "x"
        window.refresh()
        window.db.search.assert_called_with(
            "x", pinned_only=True, pinned_first=False)

    def test_history_sorts_pins_by_time(self):
        """A pin is not lifted into its own section above newer clips."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        pinned = db.add_entry("text", content_text="old pinned")
        db.toggle_pin(pinned)
        db.conn.execute("UPDATE entries SET accessed_at = 1 WHERE id = ?",
                        (pinned,))
        db.add_entry("text", content_text="new clip")
        app = self._make_app("com.clipman.TestPinOrder")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.refresh()
        texts = [window._selection.get_item(i).data["content_text"]
                 for i in range(window._selection.get_n_items())]
        self.assertEqual(texts, ["new clip", "old pinned"])

    def test_pinned_tab_without_pins_says_so(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        db.add_entry("text", content_text="not pinned")
        app = self._make_app("com.clipman.TestNoPins")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window._show_edge_state = MagicMock()
        window.select_tab("pinned")
        window._show_edge_state.assert_called_with("no-pins-yet")

    def test_ctrl_tab_cycles_the_tabs(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestCtrlTab")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        ctrl = Gdk.ModifierType.CONTROL_MASK
        shift = Gdk.ModifierType.SHIFT_MASK
        seen = []
        for _ in range(3):
            self.assertTrue(
                window._on_tab_key_pressed(None, Gdk.KEY_Tab, 0, ctrl))
            seen.append(window._active_filter)
        self.assertEqual(seen, ["pinned", "snippets", "all"])
        window._on_tab_key_pressed(None, Gdk.KEY_ISO_Left_Tab, 0, ctrl | shift)
        self.assertEqual(window._active_filter, "snippets")
        window._on_tab_key_pressed(None, Gdk.KEY_Page_Up, 0, ctrl)
        self.assertEqual(window._active_filter, "pinned")
        # Plain Tab still moves focus.
        self.assertFalse(window._on_tab_key_pressed(
            None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0)))

    def test_lowering_the_cap_prunes_at_once(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        for i in range(60):
            db.add_entry("text", content_text=f"clip {i}")
        db.set_setting("max_entries", "5000")
        app = self._make_app("com.clipman.TestCap")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        db.set_setting("max_entries", "50")
        window._on_setting_changed("max_entries", 50)
        self.assertEqual(db.count_entries(), 50)

    def test_clipboard_token_uses_newest_text_not_pinned(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        older = db.add_entry("text", content_text="older clip")
        db.toggle_pin(older)
        db.add_entry("text", content_text="newer clip")
        app = self._make_app("com.clipman.TestClipToken")
        window = ClipmanWindow(application=app, db=db, monitor=None)

        self.assertEqual(
            window._expand_snippet_tokens("> ${clipboard}"), "> newer clip"
        )

    def test_snap_notes_url_points_at_a_readme_heading(self):
        from clipman.window import ClipmanWindow

        fragment = ClipmanWindow._SNAP_NOTES_URL.rsplit("#", 1)[1]
        readme = Path(__file__).resolve().parents[1] / "README.md"
        slugs = set()
        for line in readme.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                heading = line.lstrip("#").strip().lower()
                slugs.add(re.sub(r"[^\w\- ]", "", heading).replace(" ", "-"))
        self.assertIn(fragment, slugs)

    def test_open_url_drops_non_http_links(self):
        self._allow_shell_out()
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestOpenUrl")
        window = ClipmanWindow(application=app, db=db, monitor=None)

        with patch("clipman.preferences.subprocess.Popen") as popen:
            window._open_url("file:///etc/passwd")
            popen.assert_not_called()
            window._open_url("https://example.org/")
            popen.assert_called_once()

    def test_reveal_path_opens_existing_folders_only(self):
        self._allow_shell_out()
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestReveal")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        folder = tempfile.mkdtemp(prefix="clipman-reveal-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)

        with patch("clipman.window.subprocess.Popen") as popen:
            window._reveal_path(folder)
            self.assertEqual(popen.call_args[0][0], ["xdg-open", folder])
            popen.reset_mock()
            window._reveal_path(os.path.join(folder, "missing"))
            popen.assert_not_called()

    def test_masked_row_has_no_countdown_when_autoclear_is_off(self):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestMaskedRow")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        entry = {"created_at": 0}

        self.assertTrue(window._sensitive_autoclear)
        self.assertIn("auto-clear in", window._sensitive_subtitle(entry))
        window._on_setting_changed("sensitive_autoclear", "false")
        self.assertFalse(window._sensitive_autoclear)
        self.assertEqual(window._sensitive_subtitle(entry), "Sensitive")

    def test_classify_text_code_vs_prose(self):
        """The row-type classifier catches obvious code without flagging
        prose. Regression: print('...{0}...'.format(x)) showed as plain
        text; earlier, 'cd cabinet' showed as code."""
        from clipman.window import _classify_text

        fmt_call = "print('The sum of {0} and {1} is {2}'" \
            + ".format(num1, num2, sum))"
        code = [
            fmt_call,
            "def hello():",
            "x => x * 2",
            "result.append(42)",
            "git log --oneline {",
        ]
        prose = [
            "cd cabinet",
            "from the shop earlier",
            "meet me at 5pm (maybe)",
            "The quick brown fox jumps over the lazy dog.",
        ]
        for t in code:
            self.assertEqual(_classify_text(t), "code", t)
        for t in prose:
            self.assertEqual(_classify_text(t), "text", t)
        self.assertEqual(_classify_text("https://github.com/x/y"), "link")

    def test_copy_prefers_wl_copy_on_wayland(self):
        """On Wayland the background daemon must set the clipboard via wl-copy
        — Gdk.Clipboard.set() silently fails without input focus, so the
        stale clipboard would get pasted instead of the chosen entry."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestCopyWayland")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        calls = []
        window._is_wayland = lambda: True
        window._wl_copy = lambda data, mime=None: (
            calls.append((data, mime)) or True
        )

        window._copy_to_clipboard("hello world")

        self.assertEqual(calls, [(b"hello world", None)])

    def test_copy_uses_gtk_off_wayland(self):
        """Off Wayland (X11) selections don't need focus, so the daemon uses
        GTK's clipboard and must not shell out to wl-copy."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestCopyX11")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        wl_called = []
        window._is_wayland = lambda: False
        window._wl_copy = lambda data, mime=None: wl_called.append(1) or True

        window._copy_to_clipboard("x")  # GTK path; no wl-copy on X11

        self.assertEqual(wl_called, [])

    def test_backup_restore_use_toplevel_parent(self):
        """Gtk.FileDialog wants a Gtk.Window parent; the Adw.Dialog isn't one.

        Regression guard for the PreferencesWindow->PreferencesDialog port
        that crashed Export/Restore with a TypeError: the preferences
        dialog must keep the real toplevel around for save()/open().
        """
        from gi.repository import Gtk

        from clipman.preferences import ClipmanPreferences
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestBackup")
        parent = ClipmanWindow(application=app, db=db, monitor=None)
        prefs = ClipmanPreferences(db, parent, on_setting_changed=None)
        self.assertIsInstance(prefs._parent_window, Gtk.Window)
        self.assertNotIsInstance(prefs, Gtk.Window)

    def test_snippets_dialog_constructs(self):
        from clipman.snippets_dialog import SnippetsDialog

        db = self._make_db()
        dialog = SnippetsDialog(db)
        self.assertIsNotNone(dialog)

    def test_new_snippet_starts_an_unsaved_draft(self):
        """"New" must not write a row until the user saves.

        It used to call add_snippet straight away, so cancelling left an
        empty "New snippet" behind. The editor now opens a blank draft and
        Save stays insensitive until the name is filled in.
        """
        from clipman.snippets_dialog import SnippetsDialog

        db = self._make_db()
        dialog = SnippetsDialog(db)
        self.assertIsNone(dialog._selected_id)

        dialog._on_new_clicked(None)

        self.assertEqual(db.get_snippets(), [])
        self.assertIsNone(dialog._selected_id)
        self.assertTrue(dialog._draft)
        self.assertEqual(dialog._name_row.get_text(), "")
        self.assertFalse(dialog._save_btn.get_sensitive())
        self.assertFalse(dialog._delete_btn.get_sensitive())

    def test_new_snippet_is_saved_once_it_has_a_name(self):
        from clipman.snippets_dialog import SnippetsDialog

        db = self._make_db()
        dialog = SnippetsDialog(db)
        dialog._on_new_clicked(None)

        dialog._name_row.set_text("Greeting")
        dialog._textview.get_buffer().set_text("hello")
        self.assertTrue(dialog._save_btn.get_sensitive())

        dialog._on_save_clicked(None)

        snippets = db.get_snippets()
        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0]["name"], "Greeting")
        self.assertEqual(snippets[0]["content_text"], "hello")
        self.assertFalse(dialog._draft)
        self.assertEqual(dialog._selected_id, snippets[0]["id"])
        self.assertTrue(dialog._delete_btn.get_sensitive())

    def test_cancelling_a_new_snippet_leaves_nothing_behind(self):
        from clipman.snippets_dialog import SnippetsDialog

        db = self._make_db()
        dialog = SnippetsDialog(db)
        dialog._on_new_clicked(None)
        dialog._name_row.set_text("Abandoned")

        dialog._on_cancel_clicked(None)

        self.assertEqual(db.get_snippets(), [])
        self.assertEqual(dialog._name_row.get_text(), "")

    def test_delete_asks_before_removing_a_snippet(self):
        """Delete is destructive, so it goes through a confirmation."""
        from clipman.snippets_dialog import SnippetsDialog

        db = self._make_db()
        sid = db.add_snippet("Keep me", "body")
        dialog = SnippetsDialog(db)
        dialog._load_into_form(
            next(s for s in db.get_snippets() if s["id"] == sid)
        )

        with patch("clipman.snippets_dialog.Adw.AlertDialog") as alert:
            dialog._on_delete_clicked(None)
        self.assertTrue(alert.called)
        self.assertEqual(len(db.get_snippets()), 1)

        dialog._on_delete_response(None, "cancel")
        self.assertEqual(len(db.get_snippets()), 1)

        dialog._on_delete_response(None, "delete")
        self.assertEqual(db.get_snippets(), [])
        self.assertIsNone(dialog._selected_id)

    def _snippets_dialog_on(self, db, name):
        """A snippets dialog with the snippet called ``name`` selected, the
        way a click selects it."""
        from clipman.snippets_dialog import SnippetsDialog

        dialog = SnippetsDialog(db)
        sid = next(s["id"] for s in db.get_snippets() if s["name"] == name)
        dialog._listbox.select_row(dialog._find_row_by_id(sid))
        self.assertEqual(dialog._selected_id, sid)
        return dialog, sid

    @staticmethod
    def _content(dialog):
        buf = dialog._textview.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def test_saving_a_snippet_keeps_it_in_the_editor(self):
        """UI-7: the list reload after Save removed the selected row, and
        the resulting row-selected(None) emptied the editor."""
        db = self._make_db()
        db.add_snippet("Greeting", "Hello")
        dialog, sid = self._snippets_dialog_on(db, "Greeting")
        dialog._textview.get_buffer().set_text("Hello there, ${clipboard}!")

        dialog._on_save_clicked(None)

        self.assertEqual(db.get_snippets()[0]["content_text"],
                         "Hello there, ${clipboard}!")
        self.assertEqual(dialog._selected_id, sid)
        self.assertEqual(dialog._name_row.get_text(), "Greeting")
        self.assertEqual(self._content(dialog), "Hello there, ${clipboard}!")
        self.assertEqual(dialog._title_label.get_text(), "Greeting")
        self.assertIs(dialog._listbox.get_selected_row(),
                      dialog._find_row_by_id(sid))

    def test_search_keeps_unsaved_edits(self):
        """UI-7: typing in the search box threw away the edit in progress."""
        db = self._make_db()
        db.add_snippet("Greeting", "Hello")
        db.add_snippet("Address", "1 Main St")
        dialog, sid = self._snippets_dialog_on(db, "Greeting")
        dialog._textview.get_buffer().set_text("UNSAVED EDIT")

        for query in ("Gree", "Addr", ""):
            with self.subTest(query=query):
                dialog._search.set_text(query)
                dialog._reload_list()  # search-changed fires after a delay
                self.assertEqual(dialog._selected_id, sid)
                self.assertEqual(self._content(dialog), "UNSAVED EDIT")
                self.assertTrue(dialog._save_btn.get_sensitive())
        # Back in the full list, the snippet being edited is selected.
        self.assertIs(dialog._listbox.get_selected_row(),
                      dialog._find_row_by_id(sid))

    @staticmethod
    def _label_texts(widget):
        """The text every label under ``widget`` shows. A label whose
        markup does not parse shows nothing."""
        from gi.repository import Gtk

        texts = []
        pending = [widget]
        while pending:
            widget = pending.pop()
            if isinstance(widget, Gtk.Label):
                texts.append(widget.get_text())
            child = widget.get_first_child()
            while child is not None:
                pending.append(child)
                child = child.get_next_sibling()
        return texts

    def test_snippet_names_with_markup_characters_show(self):
        """UI-8: rows parsed names as Pango markup, so '&' or '<' made the
        whole row blank."""
        from clipman.snippets_dialog import SnippetsDialog

        db = self._make_db()
        db.add_snippet("Q&A <b>notes", '<div class="x">a & b</div>')
        dialog = SnippetsDialog(db)
        texts = self._label_texts(dialog._listbox.get_row_at_index(0))
        self.assertIn("Q&A <b>notes", texts)
        self.assertIn('<div class="x">a & b</div>', texts)

    def test_database_path_with_markup_characters_shows(self):
        from clipman import database
        from clipman.preferences import ClipmanPreferences
        from clipman.window import ClipmanWindow

        db = self._make_db(prefix="clipman R&D <test> ")
        app = self._make_app("com.clipman.TestPathRow")
        parent = ClipmanWindow(application=app, db=db, monitor=None)
        prefs = ClipmanPreferences(db, parent, on_setting_changed=None)
        # An Adw.Dialog parents its content only once presented, so look
        # in the page itself.
        storage = prefs._stack.get_child_by_name("storage")
        self.assertIn(str(database.DB_PATH), self._label_texts(storage))

    def test_refresh_with_seeded_entries(self):
        """Three seeded entries -> three model items, newest first."""
        from clipman.window import ClipmanWindow

        db = self._make_db()
        # Seed in reverse chronological order — get_entries returns most
        # recent first, so we insert "old" then "mid" then "new" and
        # expect the model to hold them in (new, mid, old) order.
        for text in ("old entry", "mid entry", "new entry"):
            db.add_entry("text", content_text=text)

        app = self._make_app("com.clipman.Test")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.refresh()

        # The virtualized model should now hold three ClipItems.
        store = window._store
        self.assertEqual(store.get_n_items(), 3)
        texts = [store.get_item(i).data["content_text"] for i in range(3)]
        # get_entries returns most-recent first.
        self.assertEqual(texts, ["new entry", "mid entry", "old entry"])

    def test_on_setting_changed_fan_out(self):
        """ClipmanPreferences._save fans out (key, value) to the callback."""
        from clipman.preferences import ClipmanPreferences
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.Test")
        parent = ClipmanWindow(application=app, db=db, monitor=None)

        received: list[tuple[str, object]] = []

        def recorder(key, value):
            received.append((key, value))

        prefs = ClipmanPreferences(db, parent, on_setting_changed=recorder)
        # Simulate the SpinRow notify -> _save path the font-size row
        # uses (preferences.py wires `lambda r: self._save("font_size",
        # int(r.get_value()))`).
        prefs._save("font_size", 14)

        self.assertIn(("font_size", 14), received)
        # And the value persisted to the DB as a stringified int.
        self.assertEqual(db.get_setting("font_size"), "14")

    def test_preview_height_slider_saves_and_notifies(self):
        from clipman.preferences import ClipmanPreferences

        db = self._make_db()
        db.set_setting("thumbnail_height", "240")
        received = []
        prefs = ClipmanPreferences(
            db, None, on_setting_changed=lambda k, v: received.append((k, v)))

        scale = prefs._thumb_scale
        self.assertEqual(scale.get_value(), 240)
        adj = scale.get_adjustment()
        self.assertEqual((adj.get_lower(), adj.get_upper()), (80, 400))
        scale.set_value(300)
        self.assertIn(("thumbnail_height", 300), received)
        self.assertEqual(db.get_setting("thumbnail_height"), "300")

    def test_position_mode_and_reset(self):
        from clipman.placement import Placement
        from clipman.preferences import ClipmanPreferences

        db = self._make_db()
        prefs = ClipmanPreferences(db, None, on_setting_changed=None)
        self.assertEqual(prefs._position_mode_row.get_selected(), 0)
        prefs._position_mode_row.set_selected(1)
        self.assertEqual(db.get_setting("popup_position_mode"), "fixed")
        Placement(db).record((0, 0), (0, 0), (300, 200))
        self.assertEqual(Placement(db).request(), ("fixed", 300, 200))
        prefs._reset_position_btn.emit("clicked")
        self.assertEqual(Placement(db).request(), ("pointer", 0, 0))

    def test_preview_lines_row_saves(self):
        from clipman.preferences import ClipmanPreferences

        db = self._make_db()
        received = []
        prefs = ClipmanPreferences(
            db, None, on_setting_changed=lambda k, v: received.append((k, v)))
        self.assertEqual(prefs._lines_row.get_value(), 2)
        prefs._lines_row.set_value(3)
        self.assertIn(("preview_lines", 3), received)
        self.assertEqual(db.get_setting("preview_lines"), "3")

    def test_save_stores_bools_lowercase(self):
        """_save persists Python bools as lowercase 'true'/'false'.

        Regression: str(True) == 'True', which broke case-sensitive readers
        like app.py's `incognito_on_launch == 'true'` — the Privacy toggle
        silently did nothing.
        """
        from clipman.preferences import ClipmanPreferences

        db = self._make_db()
        prefs = ClipmanPreferences(db, None, on_setting_changed=None)
        prefs._save("incognito_on_launch", True)
        self.assertEqual(db.get_setting("incognito_on_launch"), "true")
        prefs._save("incognito_on_launch", False)
        self.assertEqual(db.get_setting("incognito_on_launch"), "false")

    def test_refresh_update_banner_revealed(self):
        """should_show_banner -> (True, version) mounts the banner row."""
        from clipman import updates
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.Test")
        window = ClipmanWindow(application=app, db=db, monitor=None)

        # No banner row at construction time. Patch should_show_banner so
        # the next refresh_update_banner call mounts the custom row with
        # the advertised version in its text.
        self.assertIsNone(window._update_banner_row)
        with patch.object(updates, "should_show_banner",
                          return_value=(True, "1.0.7")):
            window.refresh_update_banner()

        self.assertIsNotNone(window._update_banner_row)
        self.assertIn("1.0.7", window._update_banner_text)

    def test_update_banner_dismiss_persists_and_hides(self):
        """The X persists updates.dismiss() so the banner stays gone.

        Regression guard: with the old Adw.Banner there was no dismiss
        control at all — updates.dismiss()/dismissed_version were dead
        code and the banner reappeared on every launch.
        """
        from clipman import updates
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestDismiss")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        updates.set_enabled(db, True)  # independent of install-kind default

        with patch.object(updates, "latest_known",
                          return_value="9.9.9"):
            window.refresh_update_banner()  # 9.9.9 > current -> shown
        self.assertIsNotNone(window._update_banner_row)

        with patch.object(updates, "latest_known",
                          return_value="9.9.9"):
            window._on_update_banner_action("dismiss-banner")

        # Persisted for exactly the advertised version…
        self.assertEqual(updates.dismissed_version(db), "9.9.9")
        # …and the row is unmounted; a later refresh keeps it hidden.
        self.assertIsNone(window._update_banner_row)
        with patch.object(updates, "latest_known",
                          return_value="9.9.9"):
            window.refresh_update_banner()
        self.assertIsNone(window._update_banner_row)

    def test_on_edge_action_dispatches_every_states_action_id(self):
        """Runtime contract: every action_id from STATES fires its handler.

        Constructs a ClipmanWindow, monkey-patches the side-effect
        callees (xdg-open, open-prefs, refresh_update_banner,
        snippets dialog) into no-ops, then invokes _on_edge_action
        once per action_id declared in edge_states.STATES. The
        ``logger.warning`` branch is captured via assertLogs — the
        test fails if any id falls through to ``unhandled
        edge-state action_id``.
        """
        from clipman import window as window_module
        from clipman.edge_states import STATES
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestDispatch")
        window = ClipmanWindow(application=app, db=db, monitor=None)

        # Side-effect callees we never want to fire during the test:
        #   - _open_url and _reveal_path shell out to xdg-open
        #   - _on_prefs_clicked and _open_prefs open Preferences (and
        #     the backup actions its file chooser)
        #   - _on_snippets_clicked spawns Adw.Dialog
        #   - refresh_update_banner pokes the updates module
        # Replace them with recorders so we can assert the dispatch
        # actually called something rather than the warning fallback.
        called: list[str] = []
        window._open_url = lambda url: called.append(("url", url))
        window._reveal_path = lambda path: called.append(("folder", path))
        window._on_prefs_clicked = (
            lambda _b, page=None: called.append(("prefs", page))
        )
        prefs = MagicMock(name="preferences")
        window._open_prefs = (
            lambda page=None: called.append(("prefs", page)) or prefs
        )
        window._on_snippets_clicked = lambda _b, new=False: called.append(
            ("snippets", new)
        )
        window.refresh_update_banner = lambda: called.append(
            ("update-check", None)
        )

        # Collect every action_id declared in STATES.
        declared_ids: list[str] = []
        for spec in STATES.values():
            for slot in (spec.primary_action, spec.secondary_action):
                if slot is not None:
                    declared_ids.append(slot[1])

        # Drive every id through the dispatcher. assertNoLogs ensures
        # NONE of them hits the ``logger.warning("unhandled ...")``
        # branch. clear-search and close-dialog have inline behaviour
        # (no recorder hit) — they're still tested by virtue of not
        # emitting the warning.
        with self.assertLogs(window_module.logger, level="WARNING") as cm:
            # Append a deliberately-unknown id at the end so assertLogs
            # has SOMETHING to capture (it raises if zero records).
            for action_id in declared_ids:
                window._on_edge_action(action_id)
            window._on_edge_action("definitely-not-an-action")

        # Only the synthetic unknown id should have produced a warning.
        warning_messages = [r.getMessage() for r in cm.records]
        self.assertEqual(len(warning_messages), 1, warning_messages)
        self.assertIn(
            "definitely-not-an-action", warning_messages[0]
        )

    def test_search_changed_debounces_refresh(self):
        """Typing must NOT rebuild the list synchronously per keystroke.

        Regression for the input-lag bug where every ``search-changed``
        ran a ``LIKE '%q%'`` scan and rebuilt up to ~200 rows. The handler
        now updates ``_search_query`` immediately but coalesces the refresh
        behind a one-shot GLib timeout, so a burst of keystrokes schedules
        a single pending source instead of N synchronous rebuilds.

        Driven without real timing: assert the query updates and a debounce
        id is armed but ``refresh`` is untouched, then fire the debounce
        callback directly and assert exactly one refresh + a cleared id.
        """
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestDebounce")
        window = ClipmanWindow(application=app, db=db, monitor=None)

        # Count refresh() calls without actually rebuilding the list.
        refresh_calls = []
        window.refresh = lambda: refresh_calls.append(True)

        class _FakeEntry:
            def __init__(self, text):
                self._text = text

            def get_text(self):
                return self._text

        # Two quick keystrokes: query tracks the latest, refresh stays
        # untouched, and only ONE debounce source is left armed (the first
        # is cancelled by the second).
        window._on_search_changed(_FakeEntry("fo"))
        first_id = window._search_debounce_id
        self.assertNotEqual(first_id, 0)
        window._on_search_changed(_FakeEntry("foo"))
        self.assertEqual(window._search_query, "foo")
        self.assertNotEqual(window._search_debounce_id, 0)
        self.assertNotEqual(window._search_debounce_id, first_id)
        self.assertEqual(refresh_calls, [])  # NOT called synchronously

        # Remove the still-armed real GLib source so it can't fire into a
        # later test's main loop, then simulate what the timeout would run.
        from gi.repository import GLib
        GLib.source_remove(window._search_debounce_id)
        result = window._run_search_refresh()
        self.assertFalse(result)  # one-shot: returns False
        self.assertEqual(len(refresh_calls), 1)  # coalesced to a single refresh
        self.assertEqual(window._search_debounce_id, 0)  # id cleared


@unittest.skipUnless(_HAS_GTK and _ADW_INIT_OK,
                     "GTK 4 + libadwaita not available")
class TestKeyboardShortcuts(_WidgetTestCase):
    """The footer advertises ↵ Paste · ⌫ Delete · P Pin · Esc Close.

    Exercises the action helpers directly (far more robust headless than
    synthesizing real key events): select a row, call the helper, assert
    the DB / selection state changed.
    """

    def _seeded_window(self, texts=("old", "mid", "new")):
        from clipman.window import ClipmanWindow

        db = self._make_db()
        for text in texts:
            db.add_entry("text", content_text=text)
        app = self._make_app("com.clipman.TestKeys")
        window = ClipmanWindow(application=app, db=db, monitor=None)
        window.refresh()
        return db, window

    def test_selected_item_falls_back_to_first(self):
        _db, window = self._seeded_window()
        window._selection.unselect_all()
        item = window._selected_item()
        self.assertIsNotNone(item)
        # Falls back to index 0 (most-recent entry) when nothing selected.
        self.assertIs(item, window._store.get_item(0))

    def test_selected_item_prefers_selection(self):
        _db, window = self._seeded_window()
        window._selection.set_selected(1)
        self.assertIs(window._selected_item(), window._store.get_item(1))

    def test_delete_selected_removes_entry(self):
        db, window = self._seeded_window()
        window._selection.set_selected(0)
        target_id = window._store.get_item(0).data["id"]

        self.assertTrue(window._delete_selected())

        remaining = {e["id"] for e in db.get_entries(limit=200)}
        self.assertNotIn(target_id, remaining)
        self.assertEqual(len(remaining), 2)

    def test_delete_selected_empty_list_is_noop(self):
        db, window = self._seeded_window(texts=())
        # Nothing to delete -> returns False, doesn't raise.
        self.assertFalse(window._delete_selected())
        self.assertEqual(len(db.get_entries(limit=200)), 0)

    def test_pin_selected_toggles_pin(self):
        db, window = self._seeded_window()
        window._selection.set_selected(0)
        target_id = window._store.get_item(0).data["id"]
        self.assertFalse(window._store.get_item(0).data["pinned"])

        self.assertTrue(window._pin_selected())

        pinned = {e["id"] for e in db.get_entries(limit=200) if e["pinned"]}
        self.assertIn(target_id, pinned)

        # Toggling again unpins. After refresh the model is rebuilt, and
        # pinned entries sort first, so re-select index 0.
        window._selection.set_selected(0)
        self.assertTrue(window._pin_selected())
        still_pinned = {
            e["id"] for e in db.get_entries(limit=200) if e["pinned"]
        }
        self.assertNotIn(target_id, still_pinned)

    def test_pin_selected_ignores_snippet_rows(self):
        db, window = self._seeded_window(texts=())
        db.add_snippet("greeting", "hello ${date}")
        window._active_filter = "snippets"
        window.refresh()

        item = window._store.get_item(0)
        self.assertEqual(item.kind, "snippet")
        window._selection.set_selected(0)
        # Snippets have no pin — helper must decline, not crash.
        self.assertFalse(window._pin_selected())

    def test_delete_selected_ignores_snippet_rows(self):
        db, window = self._seeded_window(texts=())
        db.add_snippet("greeting", "hello")
        window._active_filter = "snippets"
        window.refresh()

        item = window._store.get_item(0)
        self.assertEqual(item.kind, "snippet")
        window._selection.set_selected(0)
        self.assertFalse(window._delete_selected())
        self.assertEqual(len(db.get_snippets()), 1)

    def test_activate_selected_pastes_entry(self):
        _db, window = self._seeded_window()
        window._selection.set_selected(1)

        pasted = []
        window._paste_entry = lambda entry: pasted.append(entry)
        self.assertTrue(window._activate_selected())
        self.assertEqual(len(pasted), 1)
        self.assertEqual(pasted[0]["content_text"], "mid")

    def test_activate_selected_falls_back_to_first_when_none_selected(self):
        _db, window = self._seeded_window()
        window._selection.unselect_all()

        pasted = []
        window._paste_entry = lambda entry: pasted.append(entry)
        self.assertTrue(window._activate_selected())
        # Index 0 is the most-recent entry ("new").
        self.assertEqual(pasted[0]["content_text"], "new")

    def test_paste_snippet_increments_use_count(self):
        """Pasting a snippet bumps use_count and the row meta shows it."""
        db, window = self._seeded_window(texts=())
        db.add_snippet("sig", "regards")
        window._active_filter = "snippets"
        window.refresh()
        window._selection.set_selected(0)
        window._copy_to_clipboard = lambda text: None
        window._dispatch_paste = lambda: None
        self.assertTrue(window._activate_selected())
        snip = db.get_snippets()[0]
        self.assertEqual(snip["use_count"], 1)

    def test_activate_selected_pastes_snippet(self):
        db, window = self._seeded_window(texts=())
        db.add_snippet("sig", "regards")
        window._active_filter = "snippets"
        window.refresh()
        window._selection.set_selected(0)

        pasted = []
        window._paste_snippet = lambda snip: pasted.append(snip)
        self.assertTrue(window._activate_selected())
        self.assertEqual(pasted[0]["name"], "sig")

    def test_activate_selected_empty_list_is_noop(self):
        _db, window = self._seeded_window(texts=())
        window._paste_entry = lambda entry: self.fail("should not paste")
        self.assertFalse(window._activate_selected())

    def test_delete_with_nothing_selected_is_noop(self):
        """Only Enter falls back to the first row. Delete with nothing
        selected used to remove the top row, even a pinned one."""
        db, window = self._seeded_window()
        window._selection.unselect_all()
        self.assertFalse(window._delete_selected())
        self.assertEqual(len(db.get_entries(limit=200)), 3)

    def test_pin_with_nothing_selected_is_noop(self):
        db, window = self._seeded_window()
        window._selection.unselect_all()
        self.assertFalse(window._pin_selected())
        self.assertFalse(any(e["pinned"] for e in db.get_entries(limit=200)))

    def test_search_focus_includes_the_inner_text(self):
        """While typing, the focus sits on the search entry's inner
        Gtk.Text, not on the entry, and that must count as the search box
        being focused."""
        _db, window = self._seeded_window()
        window.set_focus(window.search_entry.get_delegate())
        self.assertTrue(window._search_has_focus())
        window.set_focus(window.listview)
        self.assertFalse(window._search_has_focus())

    def test_down_from_search_moves_into_the_list(self):
        from gi.repository import Gdk

        _db, window = self._seeded_window()
        window._selection.unselect_all()
        window.set_focus(window.search_entry.get_delegate())
        self.assertTrue(window._on_key_pressed(None, Gdk.KEY_Down, 0, 0))
        self.assertEqual(window._selection.get_selected(), 0)


class TestPopupBehaviour(_WidgetTestCase):
    """Audit findings UI-13, UI-16, UI-17, UI-19, UI-20, UI-21, UI-27 and
    UI-28: small things the popup got wrong."""

    def _window(self, db=None):
        from clipman.window import ClipmanWindow

        app = self._make_app("com.clipman.TestPopupBehaviour")
        return ClipmanWindow(application=app, db=db or self._make_db(),
                             monitor=None)

    @staticmethod
    def _pump(ms):
        import time

        from gi.repository import GLib

        context = GLib.MainContext.default()
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)

    def test_saved_opacity_applies_at_start(self):
        """UI-13: only a change in Preferences applied it."""
        db = self._make_db()
        db.set_setting("opacity", "0.6")
        window = self._window(db)
        self.assertAlmostEqual(window.get_opacity(), 0.6, places=2)

    def test_each_open_starts_at_the_top(self):
        """UI-16: the list opened scrolled past its first section header."""
        db = self._make_db()
        pinned = db.add_entry("text", content_text="pinned note")
        db.toggle_pin(pinned)
        for i in range(12):
            db.add_entry("text", content_text=f"clip {i}")
        window = self._window(db)
        self.addCleanup(window.set_visible, False)
        adjustment = window.listview.get_vadjustment()
        for _ in range(2):
            window.toggle()
            self._pump(600)
            self.assertEqual(adjustment.get_value(), adjustment.get_lower())
            window.toggle()
            self._pump(100)

    def test_row_title_is_the_first_line_with_text(self):
        """UI-17: a clip starting with a blank line showed "(empty)"."""
        from clipman.window import _first_line

        self.assertEqual(_first_line("\n    indented block"), "indented block")
        self.assertEqual(_first_line("   \nsecond line\nthird"), "second line")
        self.assertEqual(_first_line("first\nsecond"), "first")
        self.assertEqual(_first_line("  \n\t\n "), "")
        self.assertEqual(_first_line(""), "")
        self.assertLessEqual(len(_first_line("x" * 5_000_000)), 121)

    def test_preferences_is_never_opened_twice(self):
        """UI-19: Retry on a failed backup opened a second Preferences."""
        window = self._window()
        handlers = []
        with patch("clipman.preferences.ClipmanPreferences") as prefs_class:
            prefs = prefs_class.return_value
            prefs.connect.side_effect = lambda _sig, cb: handlers.append(cb)
            window._open_prefs()
            window._action_retry_backup()
            window._action_rechoose_backup()
            window._action_rechoose_restore()
            self.assertEqual(prefs_class.call_count, 1)
            prefs.retry_backup.assert_called_once_with()
            prefs.choose_backup_file.assert_called_once_with()
            prefs.choose_restore_file.assert_called_once_with()
            prefs.show_page.assert_called_with("storage")
            # Closed: the next request opens a new one.
            for handler in handlers:
                handler(prefs)
            self.assertIsNone(window._prefs_dialog)
            window._open_prefs()
            self.assertEqual(prefs_class.call_count, 2)

    def test_retry_writes_the_same_backup_file_again(self):
        from clipman.preferences import ClipmanPreferences

        db = self._make_db()
        prefs = ClipmanPreferences(db, None, on_setting_changed=None)
        prefs._on_backup_clicked = MagicMock()
        prefs.retry_backup()  # nothing tried yet: ask for a file
        prefs._on_backup_clicked.assert_called_once_with(None)

        events = []
        prefs._emit_event = lambda name, value: events.append(name)
        target = os.path.join(tempfile.mkdtemp(), "backup.db")
        self.addCleanup(shutil.rmtree, os.path.dirname(target), True)
        with patch.object(db, "export_backup",
                          side_effect=OSError("disk full")) as export:
            prefs._write_backup(target)
            prefs.retry_backup()
        self.assertEqual([c.args for c in export.call_args_list],
                         [(target,), (target,)])
        self.assertEqual(events, ["backup_failed", "backup_failed"])

    def test_preferences_counts_as_an_open_dialog(self):
        """A failed-backup alert took the one dialog slot, and closing it
        let the popup hide from under Preferences."""
        window = self._window()
        window._prefs_dialog = MagicMock(get_mapped=MagicMock(return_value=True))
        self.assertTrue(window._child_is_open())

    def test_escape_on_an_alert_is_handled(self):
        """UI-20: Escape answered "close", which no handler knew."""
        from clipman.edge_states import STATES, render_edge_state

        window = self._window()
        for state_id, spec in STATES.items():
            if spec.kind != "alertdialog":
                continue
            with self.subTest(state_id=state_id):
                alert = render_edge_state(state_id)
                self.assertEqual(alert.get_close_response(), "close-dialog")
        with self.assertNoLogs("clipman.window", "WARNING"):
            window._on_edge_action("close-dialog")

    def test_new_snippet_entry_points_start_a_draft(self):
        """UI-21: Ctrl+N, "Add snippet" and "+" opened the editor idle."""
        from gi.repository import Gdk

        window = self._window()
        window._active_filter = "snippets"
        opens = {
            "Ctrl+N": lambda: window._on_key_pressed(
                None, Gdk.KEY_n, 0, Gdk.ModifierType.CONTROL_MASK),
            "Add snippet": lambda: window._on_edge_action(
                "open-snippets-dialog"),
            "+": lambda: window._new_snippet_btn.emit("clicked"),
        }
        with patch("clipman.snippets_dialog.SnippetsDialog.present"):
            for name, open_editor in opens.items():
                with self.subTest(entry_point=name):
                    open_editor()
                    dialog = window._child_dialog
                    self.assertTrue(dialog._draft)
                    self.assertEqual(dialog._title_label.get_text(),
                                     "New snippet")

    def test_search_hint_hides_while_typing(self):
        """UI-28: the "/" chip covered the clear button and the query."""
        window = self._window()
        self.assertTrue(window._kbd_chip.get_visible())
        window.search_entry.set_text("a long query")
        self.assertFalse(window._kbd_chip.get_visible())
        window.search_entry.set_text("")
        self.assertTrue(window._kbd_chip.get_visible())


class _FakeListItem:
    """Stands in for the Gtk.ListItem the list factory passes, and keeps
    what the row tells a screen reader."""

    def __init__(self, item=None):
        self.item = item
        self.child = None
        self.label = None
        self.description = None

    def set_child(self, child):
        self.child = child

    def get_child(self):
        return self.child

    def get_item(self):
        return self.item

    def set_accessible_label(self, label):
        self.label = label

    def set_accessible_description(self, description):
        self.description = description


class _RowTestCase(_WidgetTestCase):
    """Builds list rows the way the list factory does."""

    def _png(self, w, h):
        from gi.repository import GdkPixbuf

        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
        pixbuf.fill(0x3366FFFF)
        ok, data = pixbuf.save_to_bufferv("png", [], [])
        self.assertTrue(ok)
        return bytes(data)

    def _row_for(self, window, entry):
        from clipman.window import ClipItem

        list_item = _FakeListItem(ClipItem(entry, "entry"))
        window._row_setup(None, list_item)
        window._row_bind(None, list_item)
        return list_item.child

    def _wait_for(self, predicate, timeout=5.0):
        import time

        from gi.repository import GLib

        context = GLib.MainContext.default()
        end = time.monotonic() + timeout
        while time.monotonic() < end and not predicate():
            context.iteration(False)
            time.sleep(0.005)
        return predicate()

    @staticmethod
    def _height(thumb):
        """(minimum, natural) height without the margin above it."""
        lo, nat = thumb.measure(Gtk.Orientation.VERTICAL, -1)[:2]
        return lo - thumb.get_margin_top(), nat - thumb.get_margin_top()

    def _window(self, db):
        from clipman.window import ClipmanWindow

        app = self._make_app("com.clipman.TestPreviews")
        return ClipmanWindow(application=app, db=db, monitor=None)



class TestImagePreviews(_RowTestCase):
    """Image rows show the picture itself, at the height from Preferences."""

    def test_image_row_shows_a_preview_at_the_chosen_height(self):
        db = self._make_db()
        db.set_setting("thumbnail_height", "200")
        db.add_entry("image", image_data=self._png(1600, 1200))
        window = self._window(db)
        row = self._row_for(window, db.get_entries()[0])
        thumb = row._clip_thumb
        self.assertTrue(thumb.get_visible())
        # The height is held while the picture decodes: no jump.
        self.assertEqual(thumb.height, 200)
        self.assertEqual(self._height(thumb), (200, 200))
        self.assertTrue(self._wait_for(lambda: thumb.get_paintable() is not None))
        texture = thumb.get_paintable()
        # Decoded at the target size, never at full resolution.
        scale = max(1, window.get_scale_factor())
        self.assertEqual(texture.get_height(), 200 * scale)
        self.assertAlmostEqual(texture.get_width(), 200 * scale * 4 / 3, delta=1)
        # As wide as the aspect ratio asks (±1 px rounding), still exactly
        # 200 px high.
        self.assertAlmostEqual(
            thumb.measure(Gtk.Orientation.HORIZONTAL, -1)[1], 200 * 4 / 3, delta=1)
        self.assertEqual(self._height(thumb), (200, 200))

    def test_text_rows_have_no_preview(self):
        db = self._make_db()
        db.add_entry("text", content_text="hello")
        window = self._window(db)
        row = self._row_for(window, db.get_entries()[0])
        self.assertFalse(row._clip_thumb.get_visible())

    def test_sensitive_image_shows_no_preview(self):
        db = self._make_db()
        db.add_entry("image", image_data=self._png(10, 10), sensitive=True)
        window = self._window(db)
        row = self._row_for(window, db.get_entries()[0])
        self.assertFalse(row._clip_thumb.get_visible())

    def test_new_height_applies_at_once(self):
        db = self._make_db()
        db.add_entry("image", image_data=self._png(40, 30))
        window = self._window(db)
        window.refresh()
        self.assertEqual(window._thumb_height, 120)
        db.set_setting("thumbnail_height", "300")
        window._on_setting_changed("thumbnail_height", 300)
        self.assertEqual(window._thumb_height, 300)
        # Rebinding is debounced; it runs once the slider rests.
        self.assertTrue(self._wait_for(lambda: window._thumb_rebind_id == 0))
        row = self._row_for(window, db.get_entries()[0])
        self.assertEqual(row._clip_thumb.height, 300)

    def test_out_of_range_heights_are_clamped(self):
        from clipman import thumbnails

        self.assertEqual(thumbnails.clamp_height("10"), 80)
        self.assertEqual(thumbnails.clamp_height(9999), 400)
        self.assertEqual(thumbnails.clamp_height("junk"), thumbnails.DEFAULT_HEIGHT)


class TestPreviewLines(_RowTestCase):
    """Text rows show 1 to 3 lines, set in Preferences."""

    def test_preview_text_takes_the_first_non_blank_lines(self):
        from clipman.window import _preview_text

        text = "\n  first  \n\n second\nthird\nfourth"
        self.assertEqual(_preview_text(text, 1), "first")
        self.assertEqual(_preview_text(text, 2), "first\nsecond")
        self.assertEqual(_preview_text(text, 3), "first\nsecond\nthird")
        self.assertEqual(_preview_text(" \n\t", 3), "")
        self.assertEqual(_preview_text("x" * 500, 2), "x" * 120)
        # Only the start of a huge clip is read.
        huge = "line\n" * 2_000_000
        self.assertEqual(_preview_text(huge, 3), "line\nline\nline")

    def test_line_count_is_clamped(self):
        from clipman.window import DEFAULT_PREVIEW_LINES, _clamp_preview_lines

        self.assertEqual(_clamp_preview_lines("0"), 1)
        self.assertEqual(_clamp_preview_lines(7), 3)
        self.assertEqual(_clamp_preview_lines("junk"), DEFAULT_PREVIEW_LINES)

    def test_rows_use_the_setting(self):
        db = self._make_db()
        db.set_setting("preview_lines", "3")
        db.add_entry("text", content_text="a\nb\nc\nd")
        window = self._window(db)
        row = self._row_for(window, db.get_entries()[0])
        self.assertEqual(row._clip_title.get_lines(), 3)
        self.assertEqual(row._clip_title.get_text(), "a\nb\nc")

        db.set_setting("preview_lines", "1")
        window._on_setting_changed("preview_lines", 1)
        self.assertTrue(self._wait_for(lambda: window._thumb_rebind_id == 0))
        row = self._row_for(window, db.get_entries()[0])
        self.assertEqual(row._clip_title.get_lines(), 1)
        self.assertEqual(row._clip_title.get_text(), "a")

    def test_sensitive_and_image_rows_stay_one_line(self):
        db = self._make_db()
        db.add_entry("text", content_text="secret\nmore", sensitive=True)
        db.add_entry("image", image_data=self._png(10, 10))
        window = self._window(db)
        for entry in db.get_entries():
            row = self._row_for(window, entry)
            self.assertEqual(row._clip_title.get_lines(), 1)

    def test_long_clip_does_not_widen_the_popup(self):
        db = self._make_db()
        db.add_entry("text", content_text="word " * 400)
        window = self._window(db)
        row = self._row_for(window, db.get_entries()[0])
        natural = row._clip_title.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        self.assertLess(natural, 100)


class TestAccessibleNames(_WidgetTestCase):
    """UI-15: key controls had no accessible name, so a screen reader
    could not tell the filters, rows or colour buttons apart."""

    def _window(self, db):
        from clipman.window import ClipmanWindow

        app = self._make_app("com.clipman.TestA11y")
        return ClipmanWindow(application=app, db=db, monitor=None)

    @staticmethod
    def _named(widget):
        from gi.repository import Gtk

        return Gtk.test_accessible_has_property(
            widget, Gtk.AccessibleProperty.LABEL
        )

    def test_popup_controls_are_named(self):
        window = self._window(self._make_db())
        self.assertTrue(self._named(window.search_entry))
        for fid, button in window._filter_buttons.items():
            with self.subTest(filter=fid):
                self.assertTrue(self._named(button))

    def test_preferences_controls_are_named(self):
        from clipman.preferences import ClipmanPreferences

        db = self._make_db()
        prefs = ClipmanPreferences(db, self._window(db),
                                   on_setting_changed=None)
        rows = []
        row = prefs._sidebar.get_first_child()
        while row is not None:
            rows.append(row)
            row = row.get_next_sibling()
        self.assertEqual(len(rows), 6)
        for row in rows:
            with self.subTest(page=row._page_id):
                self.assertTrue(self._named(row))
        self.assertTrue(self._named(prefs._accent_btn))
        self.assertTrue(self._named(prefs._font_color_btn))

    def _bound(self, window, item, list_item=None):
        list_item = list_item or _FakeListItem()
        if list_item.child is None:
            window._row_setup(None, list_item)
        list_item.item = item
        window._row_bind(None, list_item)
        return list_item

    def test_clip_rows_are_named_without_secrets(self):
        from clipman.window import ClipItem

        db = self._make_db()
        db.add_entry("text", content_text="hello world")
        pinned = db.add_entry("text", content_text="\n  pinned note")
        db.toggle_pin(pinned)
        db.add_entry("text", content_text="private words 7Q",
                     sensitive=True)
        window = self._window(db)
        rows = {e["content_text"]: e for e in db.get_entries()}

        plain = self._bound(window, ClipItem(rows["hello world"], "entry"))
        self.assertEqual(plain.label, "hello world")
        self.assertTrue(plain.description)

        star = self._bound(window, ClipItem(rows["\n  pinned note"], "entry"))
        self.assertEqual(star.label, "pinned note")
        self.assertTrue(star.description.startswith("Pinned · "))

        # The row code must hide the text of a clip marked sensitive,
        # whatever it looks like.
        hidden = self._bound(
            window, ClipItem(rows["private words 7Q"], "entry"))
        self.assertEqual(hidden.label, "Sensitive clip")
        self.assertNotIn("7Q", hidden.label + hidden.description)

    def test_row_reused_for_a_snippet_drops_the_sensitive_look(self):
        """A row last bound to a sensitive clip kept its masked styling,
        and would have read "Sensitive clip", when reused for a snippet."""
        from clipman.window import ClipItem

        db = self._make_db()
        db.add_entry("text", content_text="private words 7Q",
                     sensitive=True)
        db.add_snippet("Signature", "Best regards")
        window = self._window(db)
        list_item = self._bound(
            window, ClipItem(db.get_entries()[0], "entry"))
        self._bound(window, ClipItem(db.get_snippets()[0], "snippet"),
                    list_item)
        self.assertFalse(list_item.child._clip_title.has_css_class("masked"))
        self.assertFalse(
            list_item.child._clip_subtitle.has_css_class("warning"))
        self.assertEqual(list_item.label, "Signature")


class TestPresentFocused(_WidgetTestCase):
    """The deferred search-box focus after a show must run exactly once."""

    def _window(self):
        from clipman.window import ClipmanWindow

        app = self._make_app("com.clipman.TestPresentFocused")
        window = ClipmanWindow(application=app, db=self._make_db(), monitor=None)
        # Keep the window unmapped; only the focus logic is under test.
        window.set_visible = MagicMock()
        window.present = MagicMock()
        calls = []
        # Like the real one, the stand-in returns True, which would keep a
        # plain idle callback alive.
        window.search_entry.grab_focus = lambda: calls.append(1) or True
        return window, calls

    @staticmethod
    def _drain(iterations=50):
        from gi.repository import GLib

        context = GLib.MainContext.default()
        for _ in range(iterations):
            context.iteration(False)

    def test_focus_idle_runs_once(self):
        window, calls = self._window()
        window._present_focused()
        self._drain()
        self.assertEqual(len(calls), 1)
        self.assertEqual(window._focus_idle_id, 0)

    def test_repeated_shows_do_not_stack_focus_idles(self):
        window, calls = self._window()
        window._present_focused()
        window._present_focused()
        self._drain()
        self.assertEqual(len(calls), 1)

    def test_hide_cancels_the_pending_focus(self):
        window, calls = self._window()
        window._present_focused()
        window._hide()
        self._drain()
        self.assertEqual(calls, [])
        self.assertEqual(window._focus_idle_id, 0)


@unittest.skipUnless(_HAS_GTK and _ADW_INIT_OK,
                     "GTK 4 + libadwaita not available")
class TestRestoreFlow(_WidgetTestCase):
    """The Preferences restore handler, end to end on a temp database."""

    def _prefs(self):
        from clipman.preferences import ClipmanPreferences
        from clipman.window import ClipmanWindow

        db = self._make_db()
        app = self._make_app("com.clipman.TestRestore")
        parent = ClipmanWindow(application=app, db=db, monitor=None)
        events = []
        prefs = ClipmanPreferences(
            db, parent, on_setting_changed=lambda k, v: events.append(k))
        return db, prefs, events

    @staticmethod
    def _texts(db):
        return [e["content_text"] for e in db.get_entries(limit=100)]

    def test_a_restore_can_be_undone_with_its_safety_copy(self):
        """Restoring the ``.bak`` used to overwrite it first, so the one
        way back lost the original history for good."""
        import sqlite3

        from clipman import database

        db, prefs, events = self._prefs()
        db.add_entry("text", content_text="original history")
        # A "wrong" backup: the same history with different text.
        wrong = str(database.DB_PATH.parent / "wrong.db")
        db.export_backup(wrong)
        conn = sqlite3.connect(wrong)
        conn.execute("UPDATE entries SET content_text = 'wrong history'")
        conn.commit()
        conn.close()

        prefs._on_restore_confirmed(None, "restore", wrong)
        self.assertEqual(self._texts(db), ["wrong history"])
        safety = sorted(database.DB_PATH.parent.glob("clipman.db.*.bak"))
        self.assertEqual(len(safety), 1)

        prefs._on_restore_confirmed(None, "restore", str(safety[0]))
        self.assertEqual(self._texts(db), ["original history"])
        self.assertEqual(events, ["restore_succeeded", "restore_succeeded"])

    def test_a_failed_restore_changes_nothing(self):
        from clipman import database

        db, prefs, events = self._prefs()
        db.add_entry("text", content_text="keep me")
        junk = database.DB_PATH.parent / "junk.db"
        junk.write_bytes(b"not a database at all" * 100)
        prefs._on_restore_confirmed(None, "restore", str(junk))
        self.assertEqual(events, ["restore_failed"])
        self.assertEqual(self._texts(db), ["keep me"])
        self.assertGreater(db.add_entry("text", content_text="still recording"), 0)


class TestEdgeStateDeclaration(unittest.TestCase):
    """Module-level invariants of edge_states.py that don't need GTK.

    These tests intentionally avoid importing ``render_edge_state``
    (which pulls in Adw) so they run even on a stock CI image.
    """

    def test_module_importable_without_widgets(self):
        # ``edge_states`` lazy-imports GTK inside ``render_edge_state``
        # so the module itself must import on a stock CI runner with
        # no system GTK installed. If this raises we've regressed the
        # import-policy invariant — fail loudly instead of skipping.
        from clipman import edge_states

        # The lazy-import invariant: the inventory dict exists at
        # module scope, but Adw must NOT have been pulled into the
        # module namespace by the import — that would defeat the
        # whole point of the lazy import inside render_edge_state.
        self.assertTrue(hasattr(edge_states, "STATES"))
        self.assertFalse(
            hasattr(edge_states, "Adw"),
            "edge_states must not eagerly import Adw at module scope",
        )

    def test_state_specs_have_required_fields(self):
        from clipman.edge_states import STATES

        # Sanity: the dict is the one the renderer dispatches on.
        self.assertTrue(STATES, "edge_states.STATES must not be empty")
        for state_id, spec in STATES.items():
            with self.subTest(state_id=state_id):
                self.assertEqual(spec.id, state_id)
                self.assertIn(spec.kind,
                              ("statuspage", "banner", "alertdialog"))
                self.assertIn(spec.tone,
                              ("info", "warning", "privacy", "error",
                               "neutral"))
                self.assertTrue(spec.title)
                self.assertTrue(spec.body)
                self.assertTrue(spec.icon_name)
                # Adwaita StatusPage / Banner artwork is the symbolic
                # variant — anything else looks chunky and out of place
                # next to the rest of GNOME's UI.
                self.assertTrue(
                    spec.icon_name.endswith("-symbolic"),
                    f"{state_id} icon {spec.icon_name!r} must be symbolic",
                )
                # Action specs (when present) are ``(label, action_id)``
                # tuples — the renderer indexes both fields and the host
                # window dispatches on ``action_id``.
                for slot in ("primary_action", "secondary_action"):
                    action = getattr(spec, slot)
                    if action is None:
                        continue
                    self.assertIsInstance(action, tuple)
                    self.assertEqual(len(action), 2)
                    label, action_id = action
                    self.assertTrue(label)
                    self.assertTrue(action_id)
                    self.assertIsInstance(action_id, str)


if __name__ == "__main__":
    unittest.main()
