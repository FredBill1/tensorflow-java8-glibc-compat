"""Exercise the real Ant preparation task in an isolated JDK 8 Maven container.

Run explicitly with ``python tests/run_build_preparation.py``. This does not compile
TensorFlow or modify the project's target directory; only tiny source JARs are used.
"""

import difflib
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "maven:3.9.9-eclipse-temurin-8"
FIXTURE_HOME = "/tmp/tensorflow-compat-preparation-home"
API_SOURCE = 'package org.tensorflow; class Example { void run() { var value = "ok"; } }\n'


class PreparationFixture:
    def __init__(self, root):
        self.root = root
        self.version = "1.0.0"
        self.patched_type = "String"
        self.container = None
        self.helper = root / "scripts/prepare-tensorflow-sources.xml"
        self.helper.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/prepare-tensorflow-sources.xml", self.helper)
        self.sources = root / "target/generated-sources/tensorflow"
        self.state = root / "target/tensorflow-sources.properties"
        self.files = {
            "core-api-sources.jar": {"org/tensorflow/Example.java": API_SOURCE,
                                     "module-info.java": "module ignored {}\n"},
            "core-native-sources.jar": {"org/tensorflow/Native.java": "package org.tensorflow; class Native {}\n"},
            "ndarray-sources.jar": {"org/tensorflow/ndarray/Array.java": "package org.tensorflow.ndarray; class Array {}\n"},
        }
        self.write_inputs()

    @property
    def jars(self):
        return self.root / "target/tensorflow-source-jars" / self.version

    @property
    def patch(self):
        return self.root / "patches/tensorflow" / self.version / "0001-java8-local-types.patch"

    def write_inputs(self):
        self.jars.mkdir(parents=True, exist_ok=True)
        for name, files in self.files.items():
            with zipfile.ZipFile(str(self.jars / name), "w") as archive:
                for path, content in files.items():
                    archive.writestr(path, content)
        self.write_patch()
        self.write_pom()

    def write_patch(self):
        self.patch.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(difflib.unified_diff(
            API_SOURCE.splitlines(True),
            API_SOURCE.replace("var value", self.patched_type + " value").splitlines(True),
            fromfile="a/org/tensorflow/Example.java", tofile="b/org/tensorflow/Example.java"))
        self.patch.write_bytes(text.encode("utf-8"))

    def write_pom(self):
        hashes = {name: hashlib.sha256((self.jars / name).read_bytes()).hexdigest() for name in self.files}
        properties = {
            "tensorflow.java.version": self.version,
            "tensorflow.sources.directory": "${project.build.directory}/generated-sources/tensorflow",
            "tensorflow.source.jars.directory": "${project.build.directory}/tensorflow-source-jars/${tensorflow.java.version}",
            "tensorflow.sources.api.sha256": hashes["core-api-sources.jar"],
            "tensorflow.sources.native.sha256": hashes["core-native-sources.jar"],
            "tensorflow.sources.ndarray.sha256": hashes["ndarray-sources.jar"],
        }
        bindings = ["project.build.directory", "project.build.outputDirectory", "project.build.testOutputDirectory"] + list(properties)
        xml = """<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion><groupId>test</groupId><artifactId>preparation</artifactId><version>1</version>
<properties>{properties}</properties><build><plugins><plugin>
<groupId>org.apache.maven.plugins</groupId><artifactId>maven-antrun-plugin</artifactId><version>3.1.0</version>
<executions><execution><phase>generate-sources</phase><goals><goal>run</goal></goals><configuration><target>
<ant antfile="${{project.basedir}}/scripts/prepare-tensorflow-sources.xml" dir="${{project.basedir}}" target="prepare">
{bindings}</ant></target></configuration></execution></executions>
</plugin></plugins></build></project>
""".format(
            properties="".join("<{0}>{1}</{0}>".format(k, v) for k, v in properties.items()),
            bindings="".join('<property name="{0}" value="${{{0}}}"/>'.format(k) for k in bindings))
        (self.root / "pom.xml").write_text(xml, encoding="utf-8")

    def start(self):
        arguments = ["docker", "run", "--rm", "-d"]
        if os.name == "posix":
            # The host edits and deletes files created by Maven during the cases
            # below. Use its identity so bind-mounted output stays host-writable.
            arguments.extend(["--user", "{}:{}".format(os.getuid(), os.getgid())])
        arguments.extend([
            "-v", str(self.root) + ":/workspace", "-w", "/workspace",
            "-e", "HOME=" + FIXTURE_HOME,
            "-e", "MAVEN_CONFIG=" + FIXTURE_HOME + "/.m2",
            "-e", "MAVEN_OPTS=-Duser.home=" + FIXTURE_HOME,
            IMAGE, "sleep", "infinity",
        ])
        # The fixture owns its cache inside this disposable container. Never
        # change ownership or permissions of the real build's Maven volume.
        self.container = subprocess.check_output(arguments, text=True).strip()

    def stop(self):
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], check=True, stdout=subprocess.DEVNULL)

    def run(self, expected="rebuild", failure=None):
        result = subprocess.run(["docker", "exec", self.container, "mvn", "-B",
                                 "-Dmaven.repo.local=" + FIXTURE_HOME + "/.m2/repository", "generate-sources"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if failure:
            assert result.returncode != 0 and failure in result.stdout, result.stdout
            return
        assert result.returncode == 0, result.stdout
        message = "reusing verified patched sources" if expected == "reuse" else "rebuilding patched sources"
        assert message in result.stdout, result.stdout
        assert self.state.is_file()
        if os.name == "posix":
            assert self.state.stat().st_uid == os.getuid(), "Prepared files must belong to the calling host user"
        assert "var value" not in (self.sources / "org/tensorflow/Example.java").read_text(encoding="utf-8")
        assert not (self.sources / "module-info.java").exists()
        assert not list(self.sources.rglob("*.SHA-256"))

    def seed_compiled_output(self):
        paths = ["target/classes/org/tensorflow/Removed.class", "target/test-classes/OldTest.class",
                 "target/analysis/compile", "target/maven-status/maven-compiler-plugin/compile/stale"]
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"old compiled state")
        return [self.root / relative for relative in paths]

    def snapshot(self):
        paths = list(self.sources.rglob("*")) + [self.state]
        for relative in ["target/classes", "target/test-classes", "target/analysis", "target/maven-status"]:
            paths.extend((self.root / relative).rglob("*"))
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in paths if path.is_file()}


def exercise(fixture):
    passed = []

    def case(name, action):
        action()
        passed.append(name)
        print("PASS " + name, flush=True)

    def initial():
        old_files = fixture.seed_compiled_output()
        fixture.run()
        assert all(not path.exists() for path in old_files)
    case("first preparation removes old TensorFlow classes and compiler state", initial)

    compiled = fixture.seed_compiled_output()
    previous = fixture.snapshot()

    def unchanged():
        fixture.run("reuse")
        assert previous == fixture.snapshot()
        assert all(path.exists() for path in compiled)
    case("unchanged build preserves all source, state and compiler mtimes", unchanged)

    def application_only():
        app = fixture.root / "src/main/java/Application.java"
        app.parent.mkdir(parents=True)
        app.write_text("class Application {}\n", encoding="utf-8")
        fixture.run("reuse")
        assert previous == fixture.snapshot()
    case("application changes retain prepared sources and compiler state", application_only)

    def jar_input():
        fixture.files["ndarray-sources.jar"]["org/tensorflow/ndarray/Added.java"] = "package org.tensorflow.ndarray; class Added {}\n"
        fixture.write_inputs()
        fixture.run()
        assert (fixture.sources / "org/tensorflow/ndarray/Added.java").exists()
        assert all(not path.exists() for path in compiled)
    case("changed verified source JAR invalidates the cache", jar_input)

    def helper_input():
        with fixture.helper.open("a", encoding="utf-8") as stream:
            stream.write("\n<!-- regression: changed preparation implementation -->\n")
        fixture.run()
    case("preparation implementation changes invalidate the cache", helper_input)

    def patch_input():
        fixture.patched_type = "Object"
        fixture.write_patch()
        fixture.run()
        assert "Object value" in (fixture.sources / "org/tensorflow/Example.java").read_text(encoding="utf-8")
    case("patch changes invalidate the cache", patch_input)

    def version_input():
        fixture.version = "1.0.1"
        fixture.write_inputs()
        fixture.run()
    case("version changes invalidate the cache", version_input)

    def modified():
        path = fixture.sources / "org/tensorflow/Example.java"
        before = path.stat()
        path.write_text(path.read_text(encoding="utf-8").replace("Object value", "String value"), encoding="utf-8")
        os.utime(str(path), ns=(before.st_atime_ns, before.st_mtime_ns))
        fixture.run()
        assert "Object value" in path.read_text(encoding="utf-8")
    case("changed source content is detected even with the original mtime", modified)

    def removed():
        (fixture.sources / "org/tensorflow/Native.java").unlink()
        fixture.run()
        assert (fixture.sources / "org/tensorflow/Native.java").exists()
    case("missing generated files are restored", removed)

    def added():
        extra = fixture.sources / "Extra.java"
        extra.write_text("class Extra {}\n", encoding="utf-8")
        fixture.run()
        assert not extra.exists()
    case("unexpected generated files are removed", added)

    def missing_directory():
        shutil.rmtree(str(fixture.sources))
        fixture.run()
    case("missing generated directory is restored", missing_directory)

    def empty_directory():
        shutil.rmtree(str(fixture.sources))
        fixture.sources.mkdir()
        fixture.run()
    case("empty generated directory is restored", empty_directory)

    def invalid_archive():
        jar = fixture.jars / "core-native-sources.jar"
        original = jar.read_bytes()
        snapshot = fixture.snapshot()
        jar.write_bytes(original + b"tampered")
        fixture.run(failure="TensorFlow source SHA-256 mismatch")
        assert snapshot == fixture.snapshot()
        jar.write_bytes(original)
    case("source hash mismatch fails without publishing or modifying prepared state", invalid_archive)
    case("restoring verified source input reuses the valid cache", lambda: fixture.run("reuse"))

    def invalid_patch():
        fixture.patch.write_text("invalid patch\n", encoding="utf-8")
        fixture.run(failure="No valid patches in input")
        assert not fixture.state.exists()
    case("failed patch never publishes prepared state", invalid_patch)

    def restored_patch():
        fixture.write_patch()
        fixture.run()
    case("valid patch recovers after a failed preparation", restored_patch)

    def missing_state():
        fixture.state.unlink()
        fixture.run()
    case("missing state forces a clean preparation", missing_state)
    print("Build preparation: {} cases passed".format(len(passed)), flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix="tensorflow-compat-preparation-") as temporary:
        fixture = PreparationFixture(Path(temporary))
        try:
            fixture.start()
            exercise(fixture)
        finally:
            fixture.stop()


if __name__ == "__main__":
    main()
