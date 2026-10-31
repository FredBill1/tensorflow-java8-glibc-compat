"""Docker build orchestration, using a tiny filesystem-backed container model."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("container_build", Path(__file__).resolve().parents[1] / "scripts/build.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class FakeDocker:
    """Execute only the helper's small Python commands against an isolated volume."""
    def __init__(self, volume):
        self.volume = volume
        self.volume.mkdir()
        self.calls = []
        self.commands = []
        self.outputs = {}
        self.maven_result = 0
        self.interrupt = False
        self.collision = False
        self.start_failure = False
        self.failed_copy = None

    def write(self, relative, contents):
        destination = self.volume / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)

    def docker(self, *arguments, check=True):
        self.calls.append((arguments, check))
        result, output, error = 0, "", ""
        if arguments[:2] == ("image", "inspect"):
            pass
        elif arguments[0] == "create":
            if self.collision:
                result, error = 125, "container name is already in use"
        elif arguments[:2] == ("container", "inspect"):
            result = 0 if self.collision else 1
        elif arguments[0] == "start":
            if self.start_failure:
                result, error = 1, "container could not start"
        elif arguments[0] == "exec":
            if arguments[2:4] != ("python3", "-c"):
                raise AssertionError("unexpected Docker command: " + repr(arguments))
            code = arguments[4].replace("'/build/target'", repr(str(self.volume)))
            if "/build/target" in code:
                raise AssertionError("unmapped container path")
            captured = io.StringIO()
            with redirect_stdout(captured):
                exec(compile(code, "<container helper>", "exec"), {})
            output = captured.getvalue()
        elif arguments[0] == "cp":
            relative = arguments[1].split(":" + BUILD.BUILD + "/", 1)[1]
            source, destination = self.volume / relative, Path(arguments[2])
            if relative == self.failed_copy:
                destination.write_bytes(b"incomplete copy")
                result, error = 1, "simulated copy failure"
            elif source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copyfile(source, destination)
        elif arguments[0] not in ("stop", "rm"):
            raise AssertionError("unexpected Docker command: " + repr(arguments))
        if check and result:
            raise subprocess.CalledProcessError(result, ["docker", *arguments], output=output, stderr=error)
        return subprocess.CompletedProcess(["docker", *arguments], result, stdout=output, stderr=error)

    def stream_maven(self, command, log_path):
        self.commands.append(command)
        log_path.write_text("simulated Maven output\n", encoding="utf-8")
        for relative, contents in self.outputs.items():
            self.write(relative, contents)
        if BUILD.JAR in self.outputs:
            self.write(BUILD.SOURCES_JAR, b"patched sources jar")
        if self.interrupt:
            raise KeyboardInterrupt
        return self.maven_result


class ContainerBuildTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "checkout with spaces"
        self.root.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.jar = self.target / BUILD.JAR
        self.jar.write_bytes(b"last successful host Jar")
        self.fake = FakeDocker(self.base / "linux volume")

    def run_build(self, arguments=None):
        self.output, self.errors = io.StringIO(), io.StringIO()
        with mock.patch.object(BUILD, "docker", side_effect=self.fake.docker):
            with mock.patch.object(BUILD, "stream_maven", side_effect=self.fake.stream_maven):
                with redirect_stdout(self.output), redirect_stderr(self.errors):
                    return BUILD.build(arguments or [], self.root)

    def calls(self, command):
        return [arguments for arguments, _ in self.fake.calls if arguments[0] == command]

    def test_default_verify_uses_linux_volume_and_exports_jar(self):
        self.fake.outputs = {BUILD.JAR: b"new Jar", "surefire-reports/TEST-result.xml": b"<testsuite/>"}
        self.assertEqual(self.run_build(), 0)
        container, volume = BUILD.build_names(self.root)
        creation = self.calls("create")[0]
        self.assertIn(str(self.root.resolve()) + ":/workspace:ro", creation)
        self.assertIn(volume + ":/build", creation)
        self.assertIn("tensorflow-compat-maven:/root/.m2", creation)
        self.assertIn("linux/amd64", creation)
        self.assertEqual(self.fake.commands, [["docker", "exec", container, "mvn", "-B", "verify",
                                               "-Dtensorflow.compat.build.directory=/build/target"]])
        self.assertEqual(self.jar.read_bytes(), b"new Jar")
        self.assertEqual((self.target / "surefire-reports/TEST-result.xml").read_bytes(), b"<testsuite/>")
        self.assertEqual(self.calls("rm"), [("rm", "-f", container)])
        self.assertEqual(list(self.target.glob(".build-export-*")), [])

    def test_forwards_maven_arguments_without_splitting_values_with_spaces(self):
        arguments = ["clean", "verify", "-DskipTests", "-Dcustom=value with spaces"]
        self.fake.outputs = {BUILD.JAR: b"new Jar"}
        self.assertEqual(self.run_build(arguments), 0)
        self.assertEqual(self.fake.commands[0][5:-1], arguments)
        self.assertEqual(self.fake.commands[0][-1], "-Dtensorflow.compat.build.directory=/build/target")

    def test_maven_failure_preserves_jar_and_exports_fresh_diagnostics(self):
        reports = self.target / "surefire-reports"
        reports.mkdir()
        (reports / "stale.xml").write_bytes(b"old report")
        self.fake.maven_result = 17
        self.fake.outputs = {BUILD.JAR: b"unsuccessful Jar", "surefire-reports/current.xml": b"failure report",
                             "tensorflow-native/linux-x86_64/build.log": b"JNI diagnostics"}
        self.assertEqual(self.run_build(["clean", "verify"]), 17)
        self.assertEqual(self.jar.read_bytes(), b"last successful host Jar")
        self.assertEqual([path.name for path in reports.iterdir()], ["current.xml"])
        self.assertEqual((reports / "current.xml").read_bytes(), b"failure report")
        self.assertEqual((self.target / "tensorflow-native/linux-x86_64/build.log").read_bytes(), b"JNI diagnostics")
        self.assertFalse(any(arguments[1].endswith("/" + BUILD.JAR) for arguments in self.calls("cp")))
        self.assertEqual(len(self.calls("rm")), 1)

    def test_nonpackaging_goals_cannot_publish_an_old_volume_jar(self):
        self.fake.write(BUILD.JAR, b"previous cached Jar")
        self.fake.write("surefire-reports/old.xml", b"previous cached report")
        self.fake.outputs = {"surefire-reports/current.xml": b"current report"}
        self.assertEqual(self.run_build(["test"]), 0)
        self.assertFalse((self.fake.volume / BUILD.JAR).exists())
        self.assertEqual(self.jar.read_bytes(), b"last successful host Jar")
        self.assertEqual([path.name for path in (self.target / "surefire-reports").iterdir()], ["current.xml"])
        self.assertIn("No Jar generated", self.output.getvalue())

    def test_failed_jar_copy_preserves_host_jar_and_returns_failure(self):
        self.fake.outputs = {BUILD.JAR: b"new Jar"}
        self.fake.failed_copy = BUILD.JAR
        self.assertEqual(self.run_build(), 1)
        self.assertEqual(self.jar.read_bytes(), b"last successful host Jar")
        self.assertEqual(list(self.target.glob(".build-export-*")), [])
        self.assertIn("Output export failed", self.errors.getvalue())
        self.assertEqual(len(self.calls("rm")), 1)

    def test_failed_diagnostic_copy_preserves_jar_and_previous_reports(self):
        reports = self.target / "surefire-reports"
        reports.mkdir()
        (reports / "previous.xml").write_bytes(b"previous report")
        self.fake.outputs = {BUILD.JAR: b"new Jar", "surefire-reports/current.xml": b"current report"}
        self.fake.failed_copy = "surefire-reports"
        self.assertEqual(self.run_build(), 1)
        self.assertEqual(self.jar.read_bytes(), b"last successful host Jar")
        self.assertEqual([path.name for path in reports.iterdir()], ["previous.xml"])
        self.assertEqual((reports / "previous.xml").read_bytes(), b"previous report")
        self.assertEqual(list(self.target.glob(".build-export-*")), [])
        self.assertEqual(len(self.calls("rm")), 1)

    def test_same_checkout_collision_does_not_remove_the_other_container(self):
        self.fake.collision = True
        with self.assertRaisesRegex(ValueError, "Another build owns container"):
            self.run_build()
        self.assertEqual(self.calls("rm"), [])
        self.assertEqual(self.calls("start"), [])
        self.assertEqual(self.fake.commands, [])
        self.assertEqual(self.jar.read_bytes(), b"last successful host Jar")

    def test_interrupt_stops_and_removes_owned_container_and_returns_130(self):
        self.fake.interrupt = True
        self.fake.outputs = {BUILD.JAR: b"unfinished Jar"}
        self.assertEqual(self.run_build(), 130)
        container, _ = BUILD.build_names(self.root)
        self.assertEqual(self.calls("stop"), [("stop", "--time", "1", container)])
        self.assertEqual(self.calls("rm"), [("rm", "-f", container)])
        self.assertEqual(self.calls("cp"), [])
        self.assertEqual(self.jar.read_bytes(), b"last successful host Jar")

    def test_start_failure_still_removes_owned_container(self):
        self.fake.start_failure = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_build()
        container, _ = BUILD.build_names(self.root)
        self.assertEqual(self.calls("rm"), [("rm", "-f", container)])
        self.assertEqual(self.fake.commands, [])
        self.assertEqual(self.jar.read_bytes(), b"last successful host Jar")

    def test_stream_maven_records_merged_output_and_real_exit_status(self):
        log = self.target / "subprocess.log"
        captured = io.StringIO()
        command = [sys.executable, "-c", "import sys; print('stdout', flush=True); "
                   "print('stderr', file=sys.stderr, flush=True); sys.exit(7)"]
        with redirect_stdout(captured):
            result = BUILD.stream_maven(command, log)
        self.assertEqual(result, 7)
        self.assertEqual(log.read_text(encoding="utf-8"), "stdout\nstderr\n")
        self.assertEqual(captured.getvalue(), log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
