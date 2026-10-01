"""Where the popup opens, and how big it is.

A Wayland client cannot see or set its own position, so the GNOME Shell
extension places the window and reports where it ended up. The
extension keeps no settings: every value lives here, in the daemon's
settings table.

Two modes:

- ``pointer`` (default): open at the pointer plus a remembered offset.
  Moving the popup and closing it remembers the new offset.
- ``fixed``: open at a remembered screen position. Moving the popup and
  closing it remembers the new position.

The extension keeps the window fully on screen. That correction is not
a choice the user made, so it is never remembered: only a move after
the placement changes the stored values.
"""

MODES = ("pointer", "fixed")
DEFAULT_MODE = "pointer"

# Offsets and positions beyond this are junk (a hand-edited setting or a
# confused caller); a monitor wall is still far smaller.
MAX_COORD = 32768

DEFAULT_WIDTH = 420
MIN_WIDTH, MIN_HEIGHT = 320, 280

_KEYS = {
    "mode": "popup_position_mode",
    "dx": "popup_offset_x",
    "dy": "popup_offset_y",
    "fx": "popup_fixed_x",
    "fy": "popup_fixed_y",
    "width": "window_width",
    "height": "window_height",
}


def _int(value, default=None):
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return number if -MAX_COORD <= number <= MAX_COORD else default


class Placement:
    def __init__(self, db):
        self.db = db

    # -- position ----------------------------------------------------------

    def mode(self):
        mode = self.db.get_setting(_KEYS["mode"], DEFAULT_MODE)
        return mode if mode in MODES else DEFAULT_MODE

    def set_mode(self, mode):
        self.db.set_setting(_KEYS["mode"], mode if mode in MODES else DEFAULT_MODE)

    def offset(self):
        return (_int(self.db.get_setting(_KEYS["dx"]), 0),
                _int(self.db.get_setting(_KEYS["dy"]), 0))

    def fixed(self):
        """The remembered position for fixed mode, or None."""
        x = _int(self.db.get_setting(_KEYS["fx"]))
        y = _int(self.db.get_setting(_KEYS["fy"]))
        return None if x is None or y is None else (x, y)

    def request(self):
        """``(mode, x, y)`` for the extension's PlaceWindow: an offset
        from the pointer, or a screen position. Fixed mode without a
        remembered position opens at the pointer, like the first time."""
        if self.mode() == "fixed":
            fixed = self.fixed()
            if fixed is not None:
                return ("fixed", *fixed)
        return ("pointer", *self.offset())

    def record(self, pointer, placed, final):
        """Remember a move the user made: ``pointer`` is where the pointer
        was when the popup opened, ``placed`` where the extension put the
        window, ``final`` where it was when it closed. Returns True when
        something was stored."""
        values = [_int(v) for pair in (pointer, placed, final) for v in pair]
        if None in values:
            return False
        px, py, ax, ay, fx, fy = values
        if (fx, fy) == (ax, ay):
            # Not moved: keep the offset the user chose, not the one the
            # screen-edge correction produced.
            return False
        dx, dy = fx - px, fy - py
        if _int(dx) is None or _int(dy) is None:
            return False
        self.db.set_setting(_KEYS["dx"], str(dx))
        self.db.set_setting(_KEYS["dy"], str(dy))
        self.db.set_setting(_KEYS["fx"], str(fx))
        self.db.set_setting(_KEYS["fy"], str(fy))
        return True

    def reset(self):
        """Open at the pointer again (offset 0/0); forget the fixed spot."""
        self.db.set_setting(_KEYS["dx"], "0")
        self.db.set_setting(_KEYS["dy"], "0")
        self.db.set_setting(_KEYS["fx"], "")
        self.db.set_setting(_KEYS["fy"], "")

    # -- size --------------------------------------------------------------

    def size(self, default_height, max_size=None):
        """The remembered popup size, or the default. ``max_size`` (the
        monitor's ``(width, height)``) keeps it on a smaller screen."""
        width = _int(self.db.get_setting(_KEYS["width"]), DEFAULT_WIDTH)
        height = _int(self.db.get_setting(_KEYS["height"]), default_height)
        width, height = max(width, MIN_WIDTH), max(height, MIN_HEIGHT)
        if max_size is not None:
            width = min(width, max(max_size[0], MIN_WIDTH))
            height = min(height, max(max_size[1], MIN_HEIGHT))
        return width, height

    def save_size(self, width, height):
        """Store a size the user dragged to. Zero (an unrealized window)
        and junk are ignored."""
        width, height = _int(width), _int(height)
        if not width or not height or width < 1 or height < 1:
            return False
        stored = (_int(self.db.get_setting(_KEYS["width"])),
                  _int(self.db.get_setting(_KEYS["height"])))
        if (width, height) == stored:
            return False
        self.db.set_setting(_KEYS["width"], str(width))
        self.db.set_setting(_KEYS["height"], str(height))
        return True
