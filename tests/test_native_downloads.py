"""Pinned native input downloads survive clean without trusting corrupt cache files."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("prepare_tensorflow_native", SCRIPTS / "prepare-tensorflow-native.py")
NATIVE = importlib.util.module_from_spec(SPEC)
with mock.patch.object(sys, "path", [str(SCRIPTS)] + sys.path):
    SPEC.loader.exec_module(NATIVE)


class NativeDownloadTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        self.payload = b"small, pinned native build input\n"
        self.record = self.record_for(self.payload)

    @staticmethod
    def record_for(payload):
        return {"url": "https://example.invalid/native-input", "sha256": hashlib.sha256(payload).hexdigest()}

    def cache(self, record=None, filename="input.whl"):
        return NATIVE.cached_download(record or self.record, self.repository, filename)

    def assert_no_partials(self):
        self.assertEqual(list(self.repository.rglob("*.part-*")), [])

    def test_clean_keeps_raw_cache_and_restores_staged_helper_without_network(self):
        target = self.root / "target"
        helper = target / "tensorflow-native/upstream-native/tfj_graph.h"
        with mock.patch.object(NATIVE.urllib.request, "urlopen", return_value=io.BytesIO(self.payload)) as network:
            cached = self.cache(filename=helper.name)
            NATIVE.copy_changed(cached, helper)
            network.assert_called_once_with(self.record["url"], timeout=120)
        self.assertEqual(helper.read_bytes(), self.payload)
        shutil.rmtree(target)
        with mock.patch.object(NATIVE.urllib.request, "urlopen", side_effect=AssertionError("unexpected network")):
            self.assertEqual(self.cache(filename=helper.name), cached)
            NATIVE.copy_changed(cached, helper)
        self.assertEqual(helper.read_bytes(), self.payload)
        self.assertEqual(cached.read_bytes(), self.payload)
        self.assert_no_partials()

    def test_corrupted_cache_fails_without_network_or_overwriting_it(self):
        with mock.patch.object(NATIVE.urllib.request, "urlopen", return_value=io.BytesIO(self.payload)):
            cached = self.cache()
        cached.write_bytes(b"corrupt")
        with mock.patch.object(NATIVE.urllib.request, "urlopen", side_effect=AssertionError("unexpected network")):
            with self.assertRaisesRegex(ValueError, "Cached native input SHA-256 mismatch"):
                self.cache()
        self.assertEqual(cached.read_bytes(), b"corrupt")
        self.assert_no_partials()

    def test_same_filename_with_different_hashes_keeps_both_inputs(self):
        other = b"new pinned input"
        with mock.patch.object(NATIVE.urllib.request, "urlopen", side_effect=[io.BytesIO(self.payload), io.BytesIO(other)]):
            first = self.cache()
            second = self.cache(self.record_for(other))
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent.name, self.record["sha256"])
        self.assertEqual(first.read_bytes(), self.payload)
        self.assertEqual(second.read_bytes(), other)

    def test_checksum_failure_publishes_nothing_and_cleans_only_own_partial(self):
        directory = self.repository / ".tensorflow-compat-inputs" / self.record["sha256"]
        directory.mkdir(parents=True)
        other_partial = directory / "input.whl.part-other-download"
        other_partial.write_bytes(b"another writer")
        with mock.patch.object(NATIVE.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong bytes")):
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch downloading"):
                self.cache()
        self.assertEqual(list(directory.iterdir()), [other_partial])
        self.assertEqual(other_partial.read_bytes(), b"another writer")

    def test_network_failure_retries_and_cleans_unpublished_partial(self):
        class InterruptedInput(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("network interrupted")
                return super().read(size)

        with mock.patch.object(NATIVE.urllib.request, "urlopen",
                               side_effect=lambda *args, **kwargs: InterruptedInput(b"partial response")) as network:
            with mock.patch.object(NATIVE.time, "sleep") as sleep:
                with self.assertRaisesRegex(OSError, "network interrupted"):
                    self.cache()
        self.assertEqual(network.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assert_no_partials()
        self.assertEqual([path for path in self.repository.rglob("*") if path.is_file()], [])

    def test_competing_corrupt_publication_is_never_replaced(self):
        cached = self.repository / ".tensorflow-compat-inputs" / self.record["sha256"] / "input.whl"

        def response(*args, **kwargs):
            cached.write_bytes(b"competing corrupt input")
            return io.BytesIO(self.payload)

        with mock.patch.object(NATIVE.urllib.request, "urlopen", side_effect=response):
            with self.assertRaisesRegex(ValueError, "Cached native input SHA-256 mismatch"):
                self.cache()
        self.assertEqual(cached.read_bytes(), b"competing corrupt input")
        self.assert_no_partials()

    def test_concurrent_downloads_publish_verified_input_with_unique_partials(self):
        writers = 4
        barrier = threading.Barrier(writers)
        make_temporary = tempfile.mkstemp
        temporary_names = []

        def temporary_file(*args, **kwargs):
            descriptor, name = make_temporary(*args, **kwargs)
            temporary_names.append(name)
            return descriptor, name

        def response(*args, **kwargs):
            # Ensure all calls observe a cache miss before any one publishes.
            barrier.wait(timeout=10)
            return io.BytesIO(self.payload)

        with mock.patch.object(NATIVE.urllib.request, "urlopen", side_effect=response):
            with mock.patch.object(NATIVE.tempfile, "mkstemp", side_effect=temporary_file):
                with ThreadPoolExecutor(max_workers=writers) as pool:
                    paths = list(pool.map(lambda _: self.cache(), range(writers)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(len(set(temporary_names)), writers)
        self.assertEqual(paths[0].read_bytes(), self.payload)
        self.assert_no_partials()


if __name__ == "__main__":
    unittest.main()
