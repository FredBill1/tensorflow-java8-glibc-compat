"""Preserve upstream notices under unique names before shading dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ARTIFACTS = (
    ("tensorflow-core-api-sources", "sources/core-api-sources.jar"),
    ("tensorflow-core-native-sources", "sources/core-native-sources.jar"),
    ("tensorflow-ndarray-sources", "sources/ndarray-sources.jar"),
    ("tensorflow-core-native-linux-x86_64", "org/tensorflow/tensorflow-core-native/1.2.0/tensorflow-core-native-1.2.0-linux-x86_64.jar"),
    ("tensorflow-core-native-linux-arm64", "org/tensorflow/tensorflow-core-native/1.2.0/tensorflow-core-native-1.2.0-linux-arm64.jar"),
    ("javacpp", "org/bytedeco/javacpp/1.5.12/javacpp-1.5.12.jar"),
    ("protobuf-java", "com/google/protobuf/protobuf-java/4.31.1/protobuf-java-4.31.1.jar"),
)


def collect(project, build, classes, repository):
    output = classes / "META-INF/licenses"
    records = []
    for artifact, relative in ARTIFACTS:
        archive = (build / "tensorflow-source-jars/1.2.0" / relative.split("/", 1)[1]
                   if relative.startswith("sources/") else repository / relative)
        if not archive.is_file():
            raise ValueError("Missing upstream artifact for license collection: " + str(archive))
        notices = []
        with zipfile.ZipFile(archive) as jar:
            for member in jar.infolist():
                name = Path(member.filename).name.lower()
                if member.is_dir() or not any(token in name for token in ("license", "notice", "copying")):
                    continue
                if not (name.startswith(("license", "notice", "copying")) or "-license" in name or "-notice" in name):
                    continue
                relative_name = Path(member.filename)
                if relative_name.is_absolute() or ".." in relative_name.parts:
                    raise ValueError("Invalid notice path: " + member.filename)
                data = jar.read(member)
                target = output / artifact / relative_name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.is_file() or target.read_bytes() != data:
                    target.write_bytes(data)
                notices.append({"path": member.filename, "sha256": hashlib.sha256(data).hexdigest()})
        records.append({"artifact": artifact, "notices": notices})
    vendored = project / "third_party/licenses"
    if (vendored / "inputs.json").is_file():
        for item in json.loads((vendored / "inputs.json").read_text(encoding="utf-8")):
            source = vendored / item["path"]
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("Vendored upstream notice SHA-256 mismatch: " + item["path"])
            target = output / item["artifact"] / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
            record = next(record for record in records if record["artifact"] == item["artifact"])
            record["notices"].append({"path": item["path"], "sha256": item["sha256"], "source": item["url"]})
    # Source artifacts can omit license resources; native artifacts contain the
    # complete TensorFlow runtime notices, including bundled third-party code.
    for artifact in ("tensorflow-core-native-linux-x86_64", "tensorflow-core-native-linux-arm64", "javacpp", "protobuf-java"):
        if not next(record["notices"] for record in records if record["artifact"] == artifact):
            raise ValueError("Upstream artifact contains no license/notice resources: " + artifact)
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.json").write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Preserved upstream notices for {} artifacts".format(len(records)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project-root", "build-directory", "classes-directory", "maven-repository"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    collect(args.project_root, args.build_directory, args.classes_directory, args.maven_repository)


if __name__ == "__main__":
    main()
