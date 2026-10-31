"""Create a local release bundle; never publish to an external service."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from verify_jar import ARTIFACT, JAR_NAME, VERSION, sha256, verify
from create_sources import add


ROOT = Path(__file__).resolve().parents[1]
POM_NAME = ARTIFACT + "-" + VERSION + ".pom"
SOURCES_NAME = ARTIFACT + "-" + VERSION + "-sources.jar"
SOURCE_ARCHIVE = ARTIFACT + "-" + VERSION + "-source.zip"


def source_files(root):
    patterns = ("*.md", "LICENSE", "NOTICE", "pom.xml", ".gitignore", ".gitattributes", ".dockerignore",
                "scripts/*.py", "scripts/*.xml", "scripts/*.json", "patches/**/*.patch",
                "docker/*Dockerfile", "tests/*.py", "tests/*.txt", "tests/*.lock",
                "tests/native/*.java", "tests/native/Dockerfile", "docs/*.md", ".github/workflows/*.yml",
                "third_party/licenses/*.txt", "third_party/licenses/*.json")
    return sorted({path for pattern in patterns for path in root.glob(pattern) if path.is_file()})


def release_pom():
    return '''<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>io.github.fredbill1</groupId>
  <artifactId>tensorflow-java8-glibc-compat</artifactId>
  <version>1.2.0-compat.1</version>
  <packaging>jar</packaging>
  <name>TensorFlow Java 8 glibc Compatibility</name>
  <description>Self-contained TensorFlow Java API and CPU runtime for Java 8 and glibc 2.27.</description>
  <url>https://github.com/fredbill1/tensorflow-java8-glibc-compat</url>
  <licenses><license><name>Apache License, Version 2.0</name><url>https://www.apache.org/licenses/LICENSE-2.0.txt</url></license></licenses>
</project>
'''


def release(root=ROOT, jar=None):
    root = Path(root).resolve()
    jar = Path(jar) if jar else root / "target" / JAR_NAME
    report = verify(jar)
    sources = jar.with_name(SOURCES_NAME)
    with zipfile.ZipFile(sources) as attachment:
        if "org/tensorflow/SavedModelBundle.java" not in attachment.namelist():
            raise ValueError("The patched Java source attachment is incomplete")
    dist = root / "dist"
    if dist.is_symlink() or dist.resolve() != dist:
        raise ValueError("dist must be a real directory inside the project")
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-", dir=str(dist)) as directory:
        staging = Path(directory)
        shutil.copyfile(jar, staging / JAR_NAME)
        shutil.copyfile(sources, staging / SOURCES_NAME)
        with zipfile.ZipFile(staging / SOURCE_ARCHIVE, "w") as archive:
            prefix = ARTIFACT + "-" + VERSION + "/"
            for path in source_files(root):
                try:
                    path.resolve().relative_to(root)
                except ValueError:
                    raise ValueError("Source archive must not follow links outside the project")
                if path.is_symlink():
                    raise ValueError("Source archive must not follow links outside the project")
                add(archive, prefix + path.relative_to(root).as_posix(), path.read_bytes())
        (staging / POM_NAME).write_text(release_pom(), encoding="utf-8")
        if ET.fromstring(release_pom()).find("{http://maven.apache.org/POM/4.0.0}dependencies") is not None:
            raise ValueError("Published POM must not expose bundled dependencies")
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
            shutil.copyfile(root / name, staging / name)
        with zipfile.ZipFile(jar) as source, zipfile.ZipFile(staging / "third-party-licenses.zip", "w", zipfile.ZIP_DEFLATED) as notices:
            for member in sorted(source.namelist()):
                if member.startswith("META-INF/licenses/") and not member.endswith("/"):
                    entry = zipfile.ZipInfo(member[len("META-INF/"):], date_time=(1980, 1, 1, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    notices.writestr(entry, source.read(member))
        (staging / "verification.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        outputs = sorted(path.name for path in staging.iterdir())
        (staging / "SHA256SUMS").write_text("".join(sha256(staging / name) + "  " + name + "\n" for name in outputs), encoding="utf-8")
        outputs.append("SHA256SUMS")
        # Validate every input before replacing a previous successful release.
        for name in outputs:
            destination = dist / name
            if destination.is_symlink() or destination.resolve() != destination:
                raise ValueError("Release output must not be a symlink: " + str(destination))
        for name in outputs:
            os.replace(staging / name, dist / name)
    print("Local release created: " + str(dist / JAR_NAME))
    return dist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path)
    args = parser.parse_args()
    release(jar=args.jar)


if __name__ == "__main__":
    main()
