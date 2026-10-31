"""Release boundaries: dependency isolation, provenance, and failure preservation."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
import zipfile


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import release
import verify_jar as audit


class ArtifactTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "target").mkdir()
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
            (self.root / name).write_text(name + "\n", encoding="utf-8")
        self.jar = self.root / "target" / audit.JAR_NAME
        self.library = b"synthetic native bytes used only for packaging tests"
        self.native_hash = __import__("hashlib").sha256(self.library).hexdigest()
        self.make_jar()
        with zipfile.ZipFile(self.jar.with_name(release.SOURCES_NAME), "w") as sources:
            sources.writestr("org/tensorflow/SavedModelBundle.java", "package org.tensorflow;")
        patch = mock.patch.object(audit, "validate_library", return_value={"max_versions": dict(audit.LIMITS)})
        patch.start()
        self.addCleanup(patch.stop)

    def make_jar(self, missing=None, extra=None, corrupt_metadata=False, bundled_pom=False):
        with zipfile.ZipFile(self.jar, "w") as jar:
            for name in audit.REQUIRED:
                if name == missing or name == "META-INF/tensorflow-native-build.json":
                    continue
                data = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 52) if name.endswith(".class") else b"{}"
                if name.endswith("/pom.xml"):
                    data = release.release_pom().encode()
                    if bundled_pom:
                        data = data.replace(b"</project>", b"<dependencies><dependency><groupId>org.tensorflow</groupId></dependency></dependencies></project>")
                jar.writestr(name, data)
            manifest = ("Manifest-Version: 1.0\nTensorFlow-Java-Version: 1.2.0\n"
                        "TensorFlow-Runtime-Version: 2.21.0\nImplementation-Version: 1.2.0-compat.1\n"
                        "Implementation-Title: tensorflow-java8-glibc-compat\n")
            jar.writestr("META-INF/MANIFEST.MF", manifest)
            metadata = {"tensorflow_java": "1.2.0", "tensorflow_runtime": "2.21.0", "javacpp": "1.5.12",
                        "limits": dict(audit.LIMITS), "platforms": {}}
            for platform in audit.PLATFORMS:
                metadata["platforms"][platform] = {"jni_sha256": "wrong" if corrupt_metadata else self.native_hash,
                    "core_sha256": {name: self.native_hash for name in audit.LIBRARIES[1:]}}
                for name in audit.LIBRARIES:
                    resource = audit.NATIVE_BASE + platform + "/" + name
                    if resource != missing:
                        jar.writestr(resource, self.library)
            if missing != "META-INF/tensorflow-native-build.json":
                jar.writestr("META-INF/tensorflow-native-build.json", json.dumps(metadata))
            if extra:
                jar.writestr(*extra)

    def test_release_is_self_contained_and_pom_has_no_dependencies(self):
        dist = release.release(self.root)
        pom = ET.parse(dist / release.POM_NAME).getroot()
        self.assertIsNone(pom.find("{http://maven.apache.org/POM/4.0.0}dependencies"))
        self.assertEqual((dist / audit.JAR_NAME).read_bytes(), self.jar.read_bytes())
        self.assertIn(audit.JAR_NAME, (dist / "SHA256SUMS").read_text())
        self.assertIn(release.POM_NAME, (dist / "SHA256SUMS").read_text())
        self.assertIn(release.SOURCES_NAME, (dist / "SHA256SUMS").read_text())
        self.assertTrue((dist / release.SOURCE_ARCHIVE).is_file())

    def test_release_failure_preserves_previous_outputs(self):
        dist = release.release(self.root)
        before = {path.name: path.read_bytes() for path in dist.iterdir()}
        self.make_jar(corrupt_metadata=True)
        with self.assertRaisesRegex(ValueError, "provenance"):
            release.release(self.root)
        self.assertEqual(before, {path.name: path.read_bytes() for path in dist.iterdir()})

    def test_missing_native_resource_is_rejected(self):
        self.make_jar(missing=audit.NATIVE_BASE + "linux-arm64/libtensorflow_cc.so.2")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            audit.verify(self.jar)

    def test_unrelocated_protobuf_is_rejected(self):
        self.make_jar(extra=("com/google/protobuf/Message.class", b"unused"))
        with self.assertRaisesRegex(ValueError, "unrelocated"):
            audit.verify(self.jar)

    def test_java9_base_class_is_rejected(self):
        self.make_jar(extra=("example/Bad.class", b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 53)))
        with self.assertRaisesRegex(ValueError, "Java 8"):
            audit.verify(self.jar)

    def test_embedded_pom_must_not_reintroduce_bundled_dependencies(self):
        self.make_jar(bundled_pom=True)
        with self.assertRaisesRegex(ValueError, "Embedded consumer POM"):
            audit.verify(self.jar)

    def test_project_coordinates_match_release_contract(self):
        pom = ET.parse(SCRIPTS.parent / "pom.xml").getroot()
        namespace = {"m": "http://maven.apache.org/POM/4.0.0"}
        self.assertEqual(pom.findtext("m:groupId", namespaces=namespace), "io.github.fredbill1")
        self.assertEqual(pom.findtext("m:artifactId", namespaces=namespace), audit.ARTIFACT)
        self.assertEqual(pom.findtext("m:version", namespaces=namespace), audit.VERSION)


if __name__ == "__main__":
    unittest.main()
