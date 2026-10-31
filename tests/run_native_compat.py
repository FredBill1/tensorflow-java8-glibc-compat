"""Check the final compatibility Jar on Java 8, two CPUs, and old/new Linux runtimes.

Requires Docker (with ARM64 execution support on x86 hosts), a built final Jar,
and current fixtures from tests/export_models.py. No native compilation occurs.
"""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_abi", ROOT / "scripts/native_abi.py")
ABI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ABI)
PLATFORMS = {"linux/amd64": "linux-x86_64", "linux/arm64": "linux-arm64"}
ENVIRONMENTS = {
    "baseline": {"base": "ubuntu:18.04@sha256:152dc042452c496007f07ca9127571cb9c29697f42acbfad72324b2bb2e43c98", "glibc": "2.27"},
    "modern": {"base": "ubuntu:22.04@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11", "glibc": "2.35"},
}
LIBRARIES = ("libjnitensorflow.so", "libtensorflow_cc.so.2", "libtensorflow_framework.so.2")
MARKER = "NATIVE_CHECK_JSON="


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def execute(command, log, timeout=1200):
    started = time.monotonic()
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding="utf-8", errors="replace", timeout=timeout)
    Path(log).write_text(json.dumps(command) + "\n" + result.stdout, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("Command exited {} (see {}): {}".format(result.returncode, log, result.stdout[-2500:]))
    return result.stdout, round(time.monotonic() - started, 2)


class HttpRange(io.RawIOBase):
    """Seek an immutable Maven classifier without downloading its large TF libs."""
    def __init__(self, url):
        self.url, self.position = url, 0
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as response:
            self.length = int(response.headers["Content-Length"])

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset + (0, self.position, self.length)[whence]
        return self.position

    def read(self, size=-1):
        if size < 0:
            size = self.length - self.position
        if size <= 0:
            return b""
        last = min(self.position + size, self.length) - 1
        request = urllib.request.Request(self.url, headers={"Range": "bytes={}-{}".format(self.position, last)})
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 206:
                raise RuntimeError("Maven server did not honor the HTTP Range request")
            data = response.read()
        self.position += len(data)
        return data


def audit_jar(jar, temporary, api_version, runtime_version):
    report = {"jar_sha256": sha256(jar), "native": {}, "classes": {}}
    with zipfile.ZipFile(jar) as archive:
        manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8").replace("\r\n", "\n").replace("\n ", "")
        attributes = dict(line.split(": ", 1) for line in manifest.splitlines() if ": " in line)
        for name, expected in (("TensorFlow-Java-Version", api_version), ("TensorFlow-Runtime-Version", runtime_version)):
            if attributes.get(name) != expected:
                raise ValueError("Jar Manifest {} must be {}".format(name, expected))
        report["manifest"] = {key: attributes[key] for key in ("TensorFlow-Java-Version", "TensorFlow-Runtime-Version")}
        major_counts, violations = {}, []
        for name in archive.namelist():
            if not name.endswith(".class") or name.startswith("META-INF/versions/"):
                continue
            with archive.open(name) as stream:
                header = stream.read(8)
            if len(header) != 8 or header[:4] != b"\xca\xfe\xba\xbe":
                raise ValueError("Invalid class header: " + name)
            major = struct.unpack(">H", header[6:8])[0]
            major_counts[major] = major_counts.get(major, 0) + 1
            if major > 52:
                violations.append({"class": name, "major": major})
        if violations:
            raise ValueError("Non-Java-8 base classes: " + json.dumps(violations))
        report["classes"] = {"count": sum(major_counts.values()), "max_major": max(major_counts), "major_counts": major_counts}
        for platform in PLATFORMS.values():
            platform_report = {}
            official_url = ("https://repo.maven.apache.org/maven2/org/tensorflow/tensorflow-core-native/{0}/"
                            "tensorflow-core-native-{0}-{1}.jar").format(api_version, platform)
            with zipfile.ZipFile(HttpRange(official_url)) as official:
                official_jni = temporary / (platform + "-official-jni.so")
                official_jni.write_bytes(official.read("org/tensorflow/internal/c_api/" + platform + "/libjnitensorflow.so"))
            for library in LIBRARIES:
                member = "org/tensorflow/internal/c_api/" + platform + "/" + library
                if archive.namelist().count(member) != 1:
                    raise ValueError("Expected exactly one native resource: " + member)
                extracted = temporary / (platform + "-" + library)
                with archive.open(member) as source, extracted.open("wb") as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                inspected = ABI.validate_library(extracted, platform)
                inspected.pop("file", None)
                inspected["sha256"] = sha256(extracted)
                if library == "libjnitensorflow.so":
                    inspected["official_exports"] = ABI.compare_jni_exports(extracted, official_jni)
                    inspected["official_source"] = official_url
                    inspected["official_jni_sha256"] = sha256(official_jni)
                inspected["jni_export_count"] = len(inspected.pop("jni_exports"))
                platform_report[library] = inspected
                extracted.unlink()
            report["native"][platform] = platform_report
    return report


def mount(source, target, readonly=True):
    value = "type=bind,source={},target={}".format(Path(source).resolve(), target)
    return ["--mount", value + (",readonly" if readonly else "")]


def system_facts(platform, image, output):
    triple = "x86_64-linux-gnu" if platform == "linux/amd64" else "aarch64-linux-gnu"
    script = "getconf GNU_LIBC_VERSION\nuname -m\nreadelf --version-info /usr/lib/{}/libstdc++.so.6\n".format(triple)
    text, _ = execute(["docker", "run", "--rm", "--platform", platform, image, "sh", "-ec", script], output)
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].startswith("glibc "):
        raise RuntimeError("Cannot identify test system libraries")
    maximum = {}
    for family in ("GLIBCXX", "CXXABI"):
        versions = set(re.findall(r"\bName: " + family + r"_(\d+(?:\.\d+)*)\b", text))
        maximum[family] = max(versions, key=lambda value: tuple(map(int, value.split("."))))
    return {"glibc": lines[0].split()[1], "uname_machine": lines[1], "libstdcxx_available": maximum}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, default=ROOT / "target/tensorflow-java8-glibc-compat-1.2.0-compat.1.jar")
    parser.add_argument("--fixtures", type=Path, default=ROOT / "tests/fixtures")
    parser.add_argument("--output", type=Path, default=ROOT / "test-artifacts/native-compat")
    parser.add_argument("--platform", choices=sorted(PLATFORMS), action="append")
    parser.add_argument("--environment", choices=sorted(ENVIRONMENTS), action="append")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--image-prefix", default="tensorflow-compat-native-check")
    parser.add_argument("--compiler-image", default="maven:3.9.9-eclipse-temurin-8")
    parser.add_argument("--java-api-version", default="1.2.0")
    parser.add_argument("--runtime-version", default="2.21.0")
    args = parser.parse_args()
    args.jar, args.fixtures, args.output = args.jar.resolve(), args.fixtures.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {"status": "running", "matrix": [], "jar": str(args.jar)}

    def publish():
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    try:
        if not args.jar.is_file() or not (args.fixtures / "encoded.json").is_file():
            raise ValueError("Build the final Jar and run tests/export_models.py before native compatibility checks")
        producer = json.loads((args.fixtures / "producer.json").read_text(encoding="utf-8"))
        if producer.get("tensorflow") != args.runtime_version:
            raise ValueError("Fixtures must be produced by TensorFlow " + args.runtime_version)
        host = subprocess.check_output(["docker", "info", "--format", "{{.Architecture}}"], encoding="utf-8").strip()
        host_arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(host)
        if host_arch is None:
            raise ValueError("Unsupported Docker host architecture: " + host)
        summary["docker_host_arch"] = host_arch
        platforms = args.platform or list(PLATFORMS)
        environments = args.environment or list(ENVIRONMENTS)
        with tempfile.TemporaryDirectory(prefix=".native-check-", dir=str(args.output)) as directory:
            temporary = Path(directory)
            summary["artifact_audit"] = audit_jar(args.jar, temporary, args.java_api_version, args.runtime_version)
            publish()
            classes = temporary / "classes"
            classes.mkdir()
            compile_command = ["docker", "run", "--rm", "--platform", "linux/" + host_arch]
            compile_command += mount(args.jar, "/artifact/app.jar")
            compile_command += mount(ROOT / "tests/native", "/source")
            compile_command += mount(classes, "/classes", readonly=False)
            compile_command += [args.compiler_image, "javac", "-encoding", "UTF-8", "-source", "8", "-target", "8",
                                "-cp", "/artifact/app.jar", "-d", "/classes", "/source/NativeJarCheck.java"]
            execute(compile_command, args.output / "compile-harness.log")
            header = (classes / "NativeJarCheck.class").read_bytes()[:8]
            if struct.unpack(">H", header[6:])[0] != 52:
                raise RuntimeError("The standalone harness must be Java 8 bytecode")
            summary["harness_sha256"] = sha256(classes / "NativeJarCheck.class")
            summary["harness_compiled_on"] = "linux/" + host_arch
            for platform in platforms:
                for environment in environments:
                    architecture = platform.split("/")[1]
                    name = environment + "-" + architecture
                    image = args.image_prefix + ":" + name
                    case = {"platform": platform, "environment": environment,
                            "emulated": architecture != host_arch, "image": image,
                            "base_image": ENVIRONMENTS[environment]["base"], "status": "running"}
                    summary["matrix"].append(case)
                    publish()
                    if not args.skip_build:
                        execute(["docker", "build", "--platform", platform, "--build-arg",
                                 "BASE_IMAGE=" + ENVIRONMENTS[environment]["base"], "-t", image,
                                 "-f", str(ROOT / "tests/native/Dockerfile"), str(ROOT / "tests/native")],
                                args.output / (name + "-build.log"), timeout=1800)
                    case["image_id"] = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], encoding="utf-8").strip()
                    case["system"] = system_facts(platform, image, args.output / (name + "-system.log"))
                    if case["system"]["glibc"] != ENVIRONMENTS[environment]["glibc"]:
                        raise RuntimeError("Unexpected glibc baseline for " + name)
                    command = ["docker", "run", "--rm", "--platform", platform]
                    command += mount(args.jar, "/artifact/app.jar")
                    command += mount(args.fixtures, "/fixtures")
                    command += mount(classes, "/classes")
                    command += [image, "java", "-Xmx1024m", "-cp", "/classes:/artifact/app.jar", "NativeJarCheck",
                                "/fixtures", args.runtime_version, args.java_api_version]
                    text, seconds = execute(command, args.output / (name + "-inference.log"), timeout=1200)
                    reports = [json.loads(line[len(MARKER):]) for line in text.splitlines() if line.startswith(MARKER)]
                    if len(reports) != 1 or reports[0].get("status") != "passed":
                        raise RuntimeError("Missing standalone success report for " + name)
                    case["inference"], case["seconds"] = reports[0], seconds
                    if case["inference"]["jar_sha256"] != summary["artifact_audit"]["jar_sha256"]:
                        raise RuntimeError("The container loaded a different Jar")
                    expected_arch = {"amd64", "x86_64"} if architecture == "amd64" else {"aarch64", "arm64"}
                    if case["inference"]["os_arch"] not in expected_arch:
                        raise RuntimeError("The JVM ran on the wrong CPU architecture")
                    case["status"] = "passed"
                    publish()
                    print("PASS {} {} (emulated={}, {}s)".format(platform, environment, case["emulated"], seconds), flush=True)
        summary["status"] = "passed"
        publish()
        print("Native compatibility report: " + str(args.output / "summary.json"))
        return 0
    except Exception as error:
        summary["status"], summary["error"] = "failed", str(error)
        if summary["matrix"] and summary["matrix"][-1]["status"] == "running":
            summary["matrix"][-1]["status"] = "failed"
        publish()
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
