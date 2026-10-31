"""Audit the self-contained compatibility artifact without loading native code."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from native_abi import LIMITS, validate_library


VERSION = "1.2.0-compat.1"
JAVA_VERSION = "1.2.0"
RUNTIME_VERSION = "2.21.0"
ARTIFACT = "tensorflow-java8-glibc-compat"
JAR_NAME = ARTIFACT + "-" + VERSION + ".jar"
NATIVE_BASE = "org/tensorflow/internal/c_api/"
PLATFORMS = ("linux-x86_64", "linux-arm64")
LIBRARIES = ("libjnitensorflow.so", "libtensorflow_cc.so.2", "libtensorflow_framework.so.2")
REQUIRED = (
    "org/tensorflow/SavedModelBundle.class", "org/tensorflow/proto/SavedModel.class",
    "org/tensorflow/ndarray/Shape.class", "org/bytedeco/javacpp/Loader.class",
    "io/github/fredbill1/tensorflow/compat/shaded/protobuf/Message.class",
    "META-INF/tensorflow-native-build.json", "META-INF/licenses/index.json",
    "META-INF/LICENSE", "META-INF/NOTICE", "META-INF/THIRD_PARTY_NOTICES.md",
    "META-INF/maven/io.github.fredbill1/tensorflow-java8-glibc-compat/pom.xml",
)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def verify(jar):
    report = {"jar_sha256": sha256(jar), "classes": 0, "platforms": {}}
    with zipfile.ZipFile(jar) as contents:
        names = contents.namelist()
        required = set(REQUIRED)
        required.update(NATIVE_BASE + platform + "/" + library for platform in PLATFORMS for library in LIBRARIES)
        missing = sorted(required - set(names))
        if missing:
            raise ValueError("Incomplete compatibility jar: " + ", ".join(missing))
        if len(names) != len(set(names)):
            raise ValueError("Jar contains duplicate entries")
        if any(name.startswith("com/google/protobuf/") for name in names):
            raise ValueError("Jar contains unrelocated protobuf")
        pom = ET.fromstring(contents.read("META-INF/maven/io.github.fredbill1/tensorflow-java8-glibc-compat/pom.xml"))
        if pom.findall("{http://maven.apache.org/POM/4.0.0}dependencies/{http://maven.apache.org/POM/4.0.0}dependency"):
            raise ValueError("Embedded consumer POM reintroduces bundled dependencies")
        manifest = contents.read("META-INF/MANIFEST.MF").decode().replace("\r\n", "\n").replace("\n ", "")
        attributes = dict(line.split(": ", 1) for line in manifest.splitlines() if ": " in line)
        expected = {"TensorFlow-Java-Version": JAVA_VERSION, "TensorFlow-Runtime-Version": RUNTIME_VERSION,
                    "Implementation-Version": VERSION, "Implementation-Title": ARTIFACT}
        for key, value in expected.items():
            if attributes.get(key) != value:
                raise ValueError("Unexpected jar manifest " + key)
        for name in names:
            if name.endswith(".class") and not name.startswith("META-INF/versions/"):
                with contents.open(name) as stream:
                    header = stream.read(8)
                if len(header) != 8 or header[:4] != b"\xca\xfe\xba\xbe" or struct.unpack_from(">H", header, 6)[0] > 52:
                    raise ValueError("Not a Java 8 base class: " + name)
                report["classes"] += 1
        metadata = json.loads(contents.read("META-INF/tensorflow-native-build.json"))
        if (metadata.get("tensorflow_java"), metadata.get("tensorflow_runtime"), metadata.get("javacpp")) != (JAVA_VERSION, RUNTIME_VERSION, "1.5.12"):
            raise ValueError("Unexpected native build versions")
        if metadata.get("limits") != LIMITS or set(metadata.get("platforms", {})) != set(PLATFORMS):
            raise ValueError("Unexpected native build platforms/ABI limits")
        with tempfile.TemporaryDirectory(prefix="tf-compat-audit-") as temporary:
            for platform in PLATFORMS:
                build = metadata["platforms"][platform]
                libraries = {}
                for library in LIBRARIES:
                    path = Path(temporary) / library
                    with contents.open(NATIVE_BASE + platform + "/" + library) as source, path.open("wb") as output:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            output.write(block)
                    expected_hash = build.get("jni_sha256") if library == LIBRARIES[0] else build.get("core_sha256", {}).get(library)
                    if sha256(path) != expected_hash:
                        raise ValueError("Native bytes do not match provenance: " + platform + "/" + library)
                    audit = validate_library(path, platform)
                    libraries[library] = {"sha256": expected_hash, "max_versions": audit["max_versions"]}
                    path.unlink()
                report["platforms"][platform] = libraries
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify(args.jar)
    result = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
