"""Exercise publishing against a fake GitHub API; no network or remote writes."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
with mock.patch.object(sys, "path", [str(SCRIPTS)] + sys.path):
    SPEC = importlib.util.spec_from_file_location("publish_release", SCRIPTS / "publish_release.py")
    PUBLISH = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(PUBLISH)


class PublishingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.repo = "fredbill1/tensorflow-java8-glibc-compat"
        self.tag = "v" + PUBLISH.VERSION
        (self.root / "pom.xml").write_text(
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
            '<artifactId>{}</artifactId><version>{}</version></project>'.format(PUBLISH.ARTIFACT, PUBLISH.VERSION),
            encoding="utf-8")
        self.payloads = {name: (name + " content\n").encode() for name in (
            PUBLISH.JAR_NAME, PUBLISH.release.SOURCES_NAME, PUBLISH.release.SOURCE_ARCHIVE,
            PUBLISH.release.POM_NAME, "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
            "third-party-licenses.zip", "verification.json")}
        for name, data in self.payloads.items():
            (self.dist / name).write_bytes(data)
        self.write_checksums()
        self.assets = {}
        self.uploads = []
        self.model = {"id": 41, "tag_name": self.tag, "draft": False, "immutable": False}
        self.fail_upload_after = None
        self.silent_upload_failure = False

    def write_checksums(self):
        contents = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n"
                           for name, data in sorted(self.payloads.items()))
        (self.dist / "SHA256SUMS").write_text(contents, encoding="utf-8")

    def asset(self, name, data=None):
        data = (self.dist / name).read_bytes() if data is None else data
        return {"id": len(self.assets) + 100, "name": name, "state": "uploaded", "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest()}

    def populate(self, names=None):
        for name in names or PUBLISH.release_files(self.dist):
            self.assets[name] = self.asset(name)

    def gh(self, *arguments):
        if arguments[:1] == ("api",) and "--slurp" in arguments:
            # Separate pages ensure the implementation doesn't only inspect page 1.
            values = list(self.assets.values())
            return json.dumps([values[:3], values[3:]])
        if arguments[:1] == ("api",):
            return json.dumps(self.model)
        self.assertEqual(arguments[:3], ("release", "upload", self.tag))
        self.assertEqual(arguments[-2:], ("--repo", self.repo))
        paths = [Path(p) for p in arguments[3:-2]]
        self.uploads.append([p.name for p in paths])
        if [p.name for p in paths] == ["SHA256SUMS"]:
            self.assertTrue(set(self.payloads).issubset(self.assets))
        for index, path in enumerate(paths):
            if self.fail_upload_after == index:
                raise RuntimeError("simulated upload failure")
            if not self.silent_upload_failure:
                self.assets[path.name] = self.asset(path.name, path.read_bytes())
        return ""

    def run_upload(self):
        with mock.patch.object(PUBLISH, "gh", side_effect=self.gh), redirect_stdout(io.StringIO()):
            PUBLISH.upload(self.repo, self.tag, self.dist, self.root)

    def test_tag_formats_and_all_script_versions_are_checked(self):
        self.assertEqual(PUBLISH.validate_version(self.tag, self.root), PUBLISH.VERSION)
        self.assertEqual(PUBLISH.validate_version(PUBLISH.VERSION, self.root), PUBLISH.VERSION)
        with self.assertRaisesRegex(ValueError, "versions must match"):
            PUBLISH.validate_version("v2.0.0", self.root)
        with mock.patch.object(PUBLISH.build, "JAR", "wrong.jar"):
            with self.assertRaisesRegex(ValueError, "versions must match"):
                PUBLISH.validate_version(self.tag, self.root)
        with mock.patch.object(PUBLISH.release, "release_pom", return_value=(
                '<project xmlns="http://maven.apache.org/POM/4.0.0"><version>wrong</version></project>')):
            with self.assertRaisesRegex(ValueError, "versions must match"):
                PUBLISH.validate_version(self.tag, self.root)

    def test_mismatched_tag_never_contacts_github(self):
        with mock.patch.object(PUBLISH, "gh") as api:
            with self.assertRaises(ValueError):
                PUBLISH.preflight(self.repo, "wrong", self.root)
            api.assert_not_called()

    def test_immutable_or_draft_release_fails_before_upload(self):
        for field in ("immutable", "draft"):
            with self.subTest(field=field):
                self.model[field] = True
                with self.assertRaises(ValueError):
                    self.run_upload()
                self.assertEqual(self.uploads, [])
                self.model[field] = False

    def test_first_upload_sends_exact_allowlist_and_checksum_last(self):
        (self.dist / "stale-previous-version.jar").write_bytes(b"do not publish")
        self.run_upload()
        self.assertEqual(len(self.uploads), 2)
        self.assertEqual(set(self.uploads[0]), set(self.payloads))
        self.assertEqual(self.uploads[1], ["SHA256SUMS"])

    def test_identical_rerun_is_a_noop(self):
        self.populate()
        self.run_upload()
        self.assertEqual(self.uploads, [])

    def test_partial_rerun_only_sends_missing_assets(self):
        self.populate(["LICENSE", PUBLISH.JAR_NAME, "SHA256SUMS"])
        self.run_upload()
        self.assertFalse(set(self.uploads[0]) & {"LICENSE", PUBLISH.JAR_NAME, "SHA256SUMS"})
        self.assertEqual(len(self.uploads[0]), 7)

    def test_conflict_is_detected_before_any_missing_asset_is_uploaded(self):
        for name in (PUBLISH.JAR_NAME, "SHA256SUMS"):
            with self.subTest(name=name):
                self.assets = {name: self.asset(name)}
                self.assets[name]["digest"] = "sha256:" + "0" * 64
                with self.assertRaisesRegex(ValueError, "refusing to replace"):
                    self.run_upload()
                self.assertEqual(self.uploads, [])

    def test_incomplete_asset_is_not_deleted_or_overwritten(self):
        self.assets["LICENSE"] = self.asset("LICENSE")
        self.assets["LICENSE"]["state"] = "starter"
        with self.assertRaisesRegex(ValueError, "Incomplete existing asset"):
            self.run_upload()
        self.assertEqual(self.uploads, [])

    def test_missing_api_digest_is_verified_by_streamed_download(self):
        self.populate()
        self.assets["LICENSE"]["digest"] = None
        expected = hashlib.sha256(self.payloads["LICENSE"]).hexdigest()
        with mock.patch.object(PUBLISH, "downloaded_digest", return_value=expected) as downloaded:
            self.run_upload()
        downloaded.assert_called_once_with(self.repo, self.assets["LICENSE"]["id"])
        self.assertEqual(self.uploads, [])

    def test_checksum_corruption_or_unsafe_manifest_fails_before_upload(self):
        (self.dist / "NOTICE").write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.run_upload()
        self.assertEqual(self.uploads, [])
        for name in ("../escaped", "unlisted-secret.txt"):
            (self.dist / "SHA256SUMS").write_text("0" * 64 + "  " + name + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.run_upload()
        self.assertEqual(self.uploads, [])

    def test_failed_upload_can_be_resumed_without_replacing_successful_assets(self):
        self.fail_upload_after = 2
        with self.assertRaisesRegex(RuntimeError, "simulated upload failure"):
            self.run_upload()
        completed = set(self.assets)
        self.assertEqual(len(completed), 2)
        self.fail_upload_after = None
        self.run_upload()
        self.assertFalse(completed & set(self.uploads[-1]))
        self.assertEqual(len(self.assets), 10)

    def test_success_exit_without_assets_is_not_reported_as_complete(self):
        self.silent_upload_failure = True
        with self.assertRaisesRegex(RuntimeError, "without all expected assets"):
            self.run_upload()
        self.assertNotIn("SHA256SUMS", self.uploads[0])

    def test_streamed_digest_checks_download_status(self):
        data = b"downloaded binary\x00data"
        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.BytesIO(data)
        process.wait.return_value = 0
        with mock.patch.object(PUBLISH.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(PUBLISH.downloaded_digest(self.repo, 45), hashlib.sha256(data).hexdigest())
        self.assertIn("Accept: application/octet-stream", popen.call_args.args[0])
        process.stdout = io.BytesIO(b"truncated")
        process.wait.return_value = 1
        with mock.patch.object(PUBLISH.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                PUBLISH.downloaded_digest(self.repo, 45)

    def test_gh_cli_errors_propagate(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 403")
        with mock.patch.object(PUBLISH.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                PUBLISH.gh("api", "test")


if __name__ == "__main__":
    unittest.main()
