"""Keep notices from different upstream artifacts without overwriting names."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collect_licenses


class LicenseTest(unittest.TestCase):
    def test_same_notice_name_is_preserved_per_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build, classes, repository = root / "target", root / "classes", root / "m2"
            for artifact, relative in collect_licenses.ARTIFACTS:
                path = (build / "tensorflow-source-jars/1.2.0" / relative.split("/", 1)[1]
                        if relative.startswith("sources/") else repository / relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(path, "w") as jar:
                    jar.writestr("META-INF/LICENSE", artifact)
                    jar.writestr("META-INF/NOTICE", "notice " + artifact)
                    jar.writestr("other/Ordinary.class", "ignore")
            collect_licenses.collect(root, build, classes, repository)
            for artifact, _ in collect_licenses.ARTIFACTS:
                self.assertEqual((classes / "META-INF/licenses" / artifact / "META-INF/LICENSE").read_text(), artifact)
            self.assertTrue((classes / "META-INF/licenses/index.json").is_file())

    def test_vendored_license_hash_is_checked(self):
        project = Path(__file__).resolve().parents[1]
        manifest = project / "third_party/licenses/inputs.json"
        for item in json.loads(manifest.read_text(encoding="utf-8")):
            data = (manifest.parent / item["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])


if __name__ == "__main__":
    unittest.main()
