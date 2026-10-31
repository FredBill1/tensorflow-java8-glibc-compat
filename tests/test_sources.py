"""The source attachment follows patched inputs and published protobuf names."""
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from create_sources import create_sources, RELOCATED


class SourcesTest(unittest.TestCase):
    def test_sources_are_relocated_and_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "target/generated-sources/tensorflow/org/tensorflow/SavedModelBundle.java"
            source.parent.mkdir(parents=True)
            source.write_text("package org.tensorflow; import com.google.protobuf.Message;", encoding="utf-8")
            for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
                (root / name).write_text(name, encoding="utf-8")
            output = root / "target/sources.jar"
            create_sources(root, root / "target", root / "target/classes", output)
            first = output.read_bytes()
            create_sources(root, root / "target", root / "target/classes", output)
            self.assertEqual(first, output.read_bytes())
            with zipfile.ZipFile(output) as archive:
                text = archive.read("org/tensorflow/SavedModelBundle.java").decode()
                self.assertIn(RELOCATED + ".Message", text)
                self.assertIn("META-INF/UPSTREAM-SOURCES.md", archive.namelist())


if __name__ == "__main__":
    unittest.main()
