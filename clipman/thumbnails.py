"""Image-row thumbnails: decoded off the main thread, cached per size.

Stored images are content-addressed (``images/<sha256>.png``), so a
thumbnail never goes stale; it only becomes useless when its image is
deleted or the user picks another size. Both cases are cleaned up here:

- ``set_height`` drops every other size, in memory and on disk;
- ``reap`` deletes thumbnails whose image no longer exists.

The disk cache lives in ``$XDG_CACHE_HOME/clipman/thumbnails/<px>/``,
where ``px`` is the decoded height in device pixels. Thumbnails show
clipboard content, so the folders are 0700 and the files 0600, like the
images they come from.
"""

import contextlib
import logging
import os
import shutil
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

logger = logging.getLogger(__name__)

# The range Preferences offers for the thumbnail height, in logical px.
HEIGHT_RANGE = (80, 400)
DEFAULT_HEIGHT = 120

# A panorama stays a strip: the decoded width is capped at this many
# times the height.
MAX_ASPECT = 4

# Decoded textures kept in memory. 400 px tall at scale 2 and 4:3 is
# about 2.5 MB, so this holds a screenful or two of large previews.
MEMORY_BUDGET_BYTES = 64 * 1024 * 1024


def clamp_height(value):
    """Return ``value`` as an int inside HEIGHT_RANGE; DEFAULT_HEIGHT if
    it is not a number."""
    try:
        height = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_HEIGHT
    low, high = HEIGHT_RANGE
    return min(max(height, low), high)


def cache_root():
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "clipman" / "thumbnails"


def _texture_from_pixbuf(pixbuf):
    # Gdk.Texture.new_for_pixbuf is deprecated since GTK 4.12.
    fmt = (Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha()
           else Gdk.MemoryFormat.R8G8B8)
    return Gdk.MemoryTexture.new(
        pixbuf.get_width(), pixbuf.get_height(), fmt,
        pixbuf.read_pixel_bytes(), pixbuf.get_rowstride(),
    )


def _texture_bytes(texture):
    return texture.get_width() * texture.get_height() * 4


class ThumbnailCache:
    """Hands out thumbnail textures for stored images.

    ``lookup`` answers at once from memory, or returns None and decodes
    in the background; ``callback(texture_or_None)`` then runs on the
    main loop. Only the main thread may call the public methods.
    """

    def __init__(self, root=None, workers=2):
        self._root = Path(root) if root is not None else cache_root()
        self._memory = OrderedDict()  # (path, px) -> texture, LRU order
        self._memory_bytes = 0
        self._pending = {}  # (path, px) -> [callbacks]
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="clipman-thumb")
        self._lock = threading.Lock()  # guards folder creation/removal

    # -- public, main thread ---------------------------------------------

    def lookup(self, image_path, px, callback):
        """Return the texture for ``image_path`` decoded ``px`` device
        pixels high, or None and call ``callback`` once it is ready."""
        key = (image_path, px)
        texture = self._memory.get(key)
        if texture is not None:
            self._memory.move_to_end(key)
            return texture
        waiting = self._pending.get(key)
        if waiting is not None:
            waiting.append(callback)
            return None
        self._pending[key] = [callback]
        try:
            self._executor.submit(self._work, image_path, px)
        except RuntimeError:
            # Shut down: nothing will ever answer.
            self._pending.pop(key, None)
        return None

    def set_height(self, px):
        """Keep only thumbnails ``px`` device pixels high: drop the other
        sizes from memory now, and from disk in the background."""
        for key in [k for k in self._memory if k[1] != px]:
            self._forget(key)
        self._submit(self._purge_other_sizes, px)

    def reap(self, images_dir):
        """Delete, in the background, thumbnails whose image is gone."""
        self._submit(self._reap, Path(images_dir))

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- internals ---------------------------------------------------------

    def _submit(self, fn, *args):
        try:
            self._executor.submit(fn, *args)
        except RuntimeError:
            logger.debug("thumbnail executor is shut down")

    def _forget(self, key):
        texture = self._memory.pop(key, None)
        if texture is not None:
            self._memory_bytes -= _texture_bytes(texture)

    def _remember(self, key, texture):
        self._forget(key)
        self._memory[key] = texture
        self._memory_bytes += _texture_bytes(texture)
        while self._memory_bytes > MEMORY_BUDGET_BYTES and len(self._memory) > 1:
            self._forget(next(iter(self._memory)))

    def _deliver(self, key, texture):
        if texture is not None:
            self._remember(key, texture)
        for callback in self._pending.pop(key, []):
            try:
                callback(texture)
            except Exception:
                logger.exception("thumbnail callback failed")
        return GLib.SOURCE_REMOVE

    def _size_dir(self, px):
        return self._root / str(px)

    def _work(self, image_path, px):
        """Worker thread: load the cached thumbnail, or decode the image
        and cache it. GdkTexture is immutable and may be built here."""
        texture = None
        try:
            texture = _texture_from_pixbuf(self._load_or_decode(image_path, px))
        except Exception:
            # Missing, corrupt or unsupported image: the row shows no
            # preview, the popup carries on.
            logger.debug("thumbnail failed for %r", image_path, exc_info=True)
        GLib.idle_add(self._deliver, (image_path, px), texture)

    def _load_or_decode(self, image_path, px):
        name = Path(image_path).stem + ".png"
        cached = self._size_dir(px) / name
        if cached.is_file():
            try:
                return GdkPixbuf.Pixbuf.new_from_file(str(cached))
            except GLib.Error:
                logger.debug("bad cached thumbnail %s", cached, exc_info=True)
                with contextlib.suppress(OSError):
                    cached.unlink()
        # Decode straight to the target size, never at full resolution.
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            image_path, px * MAX_ASPECT, px, True)
        self._store(cached, pixbuf)
        return pixbuf

    def _store(self, path, pixbuf):
        """Write ``pixbuf`` to ``path`` as a 0600 PNG, via a temp file so
        a reader never sees half a file. Best effort."""
        tmp = path.with_name(path.name + f".{threading.get_ident()}.tmp")
        try:
            with self._lock:
                self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.parent.mkdir(exist_ok=True, mode=0o700)
            ok, data = pixbuf.save_to_bufferv("png", [], [])
            if not ok:
                return
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except (OSError, GLib.Error):
            logger.debug("could not cache thumbnail %s", path, exc_info=True)
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _purge_other_sizes(self, px):
        with self._lock:
            try:
                folders = list(self._root.iterdir())
            except OSError:
                return
            for folder in folders:
                if folder.name != str(px) and folder.is_dir() \
                        and not folder.is_symlink():
                    shutil.rmtree(folder, ignore_errors=True)

    def _reap(self, images_dir):
        try:
            alive = {p.stem for p in images_dir.iterdir()}
        except OSError:
            # Without the image list nothing can be judged an orphan.
            return
        with self._lock:
            try:
                folders = [f for f in self._root.iterdir()
                           if f.is_dir() and not f.is_symlink()]
            except OSError:
                return
            for folder in folders:
                for thumb in folder.iterdir():
                    stem = thumb.name.split(".", 1)[0]
                    if stem not in alive:
                        with contextlib.suppress(OSError):
                            thumb.unlink()
