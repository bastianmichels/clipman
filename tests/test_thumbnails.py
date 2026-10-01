"""The thumbnail cache: background decoding, per-size disk cache, cleanup."""

import shutil
import tempfile
import time
import unittest
from pathlib import Path

try:
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib

    from clipman import thumbnails
except (ImportError, ValueError):
    thumbnails = None


def _png(path, w, h):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
    pixbuf.fill(0xCC3300FF)
    pixbuf.savev(str(path), "png", [], [])


@unittest.skipIf(thumbnails is None, "needs GdkPixbuf")
class TestThumbnailCache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="clipman-thumbs-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.images = Path(tmp) / "images"
        self.images.mkdir()
        self.root = Path(tmp) / "cache"
        self.cache = thumbnails.ThumbnailCache(root=self.root)
        self.addCleanup(self.cache.shutdown)

    def _image(self, name="a" * 64, w=800, h=400):
        path = self.images / f"{name}.png"
        _png(path, w, h)
        return str(path)

    def _wait(self, predicate, timeout=5.0):
        context = GLib.MainContext.default()
        end = time.monotonic() + timeout
        while time.monotonic() < end and not predicate():
            context.iteration(False)
            time.sleep(0.005)
        return predicate()

    def _fetch(self, path, px):
        got = []
        texture = self.cache.lookup(path, px, got.append)
        if texture is not None:
            return texture
        self.assertTrue(self._wait(lambda: got), "no answer from the worker")
        return got[0]

    def test_decodes_in_the_background_then_answers_from_memory(self):
        path = self._image()
        calls = []
        self.assertIsNone(self.cache.lookup(path, 100, calls.append))
        self.assertTrue(self._wait(lambda: calls))
        texture = calls[0]
        self.assertEqual((texture.get_width(), texture.get_height()), (200, 100))
        # Now cached: answered at once, the callback is not used.
        self.assertIs(self.cache.lookup(path, 100, self.fail), texture)

    def test_one_decode_for_concurrent_requests(self):
        path = self._image()
        first, second = [], []
        self.cache.lookup(path, 100, first.append)
        self.cache.lookup(path, 100, second.append)
        self.assertTrue(self._wait(lambda: first and second))
        self.assertIs(first[0], second[0])

    def test_writes_a_private_disk_cache(self):
        path = self._image()
        self._fetch(path, 100)
        cached = self.root / "100" / ("a" * 64 + ".png")
        self.assertTrue(self._wait(cached.is_file))
        self.assertEqual(cached.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "100").stat().st_mode & 0o777, 0o700)
        # A new cache (a restart) reads the small copy, not the image.
        Path(path).unlink()
        fresh = thumbnails.ThumbnailCache(root=self.root)
        self.addCleanup(fresh.shutdown)
        self.cache = fresh
        self.assertIsNotNone(self._fetch(path, 100))

    def test_caps_a_panorama(self):
        path = self._image(w=4000, h=100)
        texture = self._fetch(path, 100)
        self.assertEqual(texture.get_width(), 100 * thumbnails.MAX_ASPECT)
        self.assertLessEqual(texture.get_height(), 100)

    def test_unreadable_image_answers_none(self):
        bad = self.images / ("b" * 64 + ".png")
        bad.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
        self.assertIsNone(self._fetch(str(bad), 100))
        self.assertIsNone(self._fetch(str(self.images / "missing.png"), 100))

    def test_new_height_drops_the_other_sizes(self):
        path = self._image()
        self._fetch(path, 100)
        self._fetch(path, 200)
        self.assertTrue(self._wait((self.root / "100").is_dir))
        self.cache.set_height(200)
        self.assertTrue(self._wait(lambda: not (self.root / "100").exists()))
        self.assertTrue((self.root / "200").is_dir())
        self.assertNotIn((path, 100), self.cache._memory)
        self.assertIn((path, 200), self.cache._memory)

    def test_reap_deletes_thumbnails_of_deleted_images(self):
        keep = self._image("k" * 64)
        gone = self._image("g" * 64)
        self._fetch(keep, 100)
        self._fetch(gone, 100)
        folder = self.root / "100"
        self.assertTrue(self._wait(lambda: len(list(folder.glob("*.png"))) == 2))
        Path(gone).unlink()
        self.cache.reap(self.images)
        self.assertTrue(self._wait(
            lambda: [p.name for p in folder.iterdir()] == ["k" * 64 + ".png"]))

    def test_reap_without_the_images_folder_deletes_nothing(self):
        path = self._image()
        self._fetch(path, 100)
        folder = self.root / "100"
        self.assertTrue(self._wait(lambda: any(folder.iterdir())))
        shutil.rmtree(self.images)
        self.cache.reap(self.images)
        # Let the queued reap run, then check it kept everything.
        self.cache._executor.submit(lambda: None).result(timeout=5)
        self.cache._executor.shutdown(wait=True)
        self.assertTrue(any(folder.iterdir()))

    def test_memory_stays_within_budget(self):
        paths = [self._image(str(i) * 64, w=400, h=400) for i in range(3)]
        budget = 2 * 400 * 400 * 4
        orig = thumbnails.MEMORY_BUDGET_BYTES
        thumbnails.MEMORY_BUDGET_BYTES = budget
        self.addCleanup(setattr, thumbnails, "MEMORY_BUDGET_BYTES", orig)
        for path in paths:
            self._fetch(path, 400)
        self.assertLessEqual(self.cache._memory_bytes, budget)
        # The oldest went first.
        self.assertNotIn((paths[0], 400), self.cache._memory)
        self.assertIn((paths[2], 400), self.cache._memory)


if __name__ == "__main__":
    unittest.main()
