"""Package the exact patched TensorFlow Java sources used by this build."""
import argparse
from pathlib import Path
import zipfile


RELOCATED = "io.github.fredbill1.tensorflow.compat.shaded.protobuf"
UPSTREAM_NOTES = """# Sources in this artifact

The org.tensorflow Java sources are the SHA-256-verified upstream 1.2.0
source artifacts after the Java 8 patches in this repository. References to
com.google.protobuf are rewritten to match the published binary namespace.
This source attachment is for browsing the rebuilt TensorFlow API. Build the
repository to reproduce the binary, including JavaCPP and native packaging.

The bundled JavaCPP 1.5.12 and protobuf-java 4.31.1 binaries retain their
upstream source distributions. Their source archives are available at:

- https://repo.maven.apache.org/maven2/org/bytedeco/javacpp/1.5.12/javacpp-1.5.12-sources.jar
- https://repo.maven.apache.org/maven2/com/google/protobuf/protobuf-java/4.31.1/protobuf-java-4.31.1-sources.jar

Upstream license and notice texts accompany this attachment under
META-INF/licenses. These source links do not add build-time downloads.
"""


def add(archive, name, data):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def create_sources(project, build, classes, output):
    generated = build / "generated-sources/tensorflow"
    sources = sorted((generated / "org").rglob("*.java"))
    if not sources or not (generated / "org/tensorflow/SavedModelBundle.java").is_file():
        raise ValueError("Verified patched TensorFlow Java sources are missing")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for source in sources:
                data = source.read_text(encoding="utf-8").replace("com.google.protobuf", RELOCATED)
                data = ("/* Compatibility distribution by tensorflow-java8-glibc-compat: "
                        "Java 8 patches applied where needed; protobuf references relocated. "
                        "See META-INF/UPSTREAM-SOURCES.md and the project patch files. */\n" + data)
                add(archive, source.relative_to(generated).as_posix(), data.encode("utf-8"))
            for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
                add(archive, "META-INF/" + name, (project / name).read_bytes())
            for source in sorted((classes / "META-INF/licenses").rglob("*")):
                if source.is_file():
                    add(archive, source.relative_to(classes).as_posix(), source.read_bytes())
            add(archive, "META-INF/UPSTREAM-SOURCES.md", UPSTREAM_NOTES.encode("utf-8"))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print("Packaged {} patched Java sources: {}".format(len(sources), output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project-root", "build-directory", "classes-directory", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    create_sources(args.project_root, args.build_directory, args.classes_directory, args.output)


if __name__ == "__main__":
    main()
