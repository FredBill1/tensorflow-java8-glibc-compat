#!/usr/bin/env python3
"""Rebuild only the two TensorFlow JNI bridges with the pinned GCC 10 toolchain.

Invoked by Maven after Java compilation. TensorFlow itself is never compiled.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

from native_abi import LIMITS, compare_jni_exports, validate_library


PLATFORMS = {"linux-x86_64": "/usr/bin/g++-10",
             "linux-arm64": "/usr/bin/aarch64-linux-gnu-g++-10"}
LIBRARIES = ("libjnitensorflow.so", "libtensorflow_cc.so.2", "libtensorflow_framework.so.2")
FLAGS = ["-D_GLIBCXX_USE_CXX11_ABI=1", "-pthread", "-ldl", "-Wl,-z,defs"]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_cache(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(str(temporary), str(path))


def copy_changed(source, destination):
    if destination.is_file() and sha256(source) == sha256(destination):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copyfile(str(source), str(temporary))
    os.replace(str(temporary), str(destination))


def download(record, destination):
    """Pin every network input; never overwrite an existing mismatched input silently."""
    if not destination.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=destination.name + ".part-", dir=str(destination.parent))
        os.close(descriptor)
        temporary = Path(name)
        try:
            print("TF_NATIVE_DOWNLOAD " + record["url"], flush=True)
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(record["url"], timeout=120) as source, temporary.open("wb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    break
                except (OSError, TimeoutError):
                    if attempt == 2:
                        raise
                    time.sleep(1)
            if sha256(temporary) != record["sha256"]:
                raise ValueError("SHA-256 mismatch downloading " + record["url"])
            try:
                # Link the complete file atomically without replacing an existing
                # immutable input that another builder may already be reading.
                os.link(str(temporary), str(destination))
            except FileExistsError:
                pass  # The winner's complete file is verified below as on any hit.
        finally:
            temporary.unlink(missing_ok=True)
    if sha256(destination) != record["sha256"]:
        raise ValueError("Cached native input SHA-256 mismatch: " + str(destination))
    return destination


def cached_download(record, repository, filename):
    """Keep pinned raw inputs outside target, alongside the existing Maven cache."""
    return download(record, repository / ".tensorflow-compat-inputs" / record["sha256"] / filename)


def extract_file(archive, member, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member) as source, destination.open("wb") as output:
        shutil.copyfileobj(source, output, 1024 * 1024)


def extract_headers(wheel, destination, work):
    # Only fixed archive prefixes are accepted; no ZipFile.extractall or symlink traversal.
    destination.resolve().relative_to(work.resolve())
    if destination.exists():
        shutil.rmtree(str(destination))
    count = 0
    with zipfile.ZipFile(str(wheel)) as archive:
        for member in archive.namelist():
            if member.startswith("tensorflow/include/") and not member.endswith("/"):
                relative = Path(member[len("tensorflow/include/"):])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Invalid wheel header path: " + member)
                extract_file(archive, member, destination / relative)
                count += 1
    if not count:
        raise ValueError("TensorFlow headers missing from " + str(wheel))


def run(arguments, **kwargs):
    result = subprocess.run([str(argument) for argument in arguments], check=True, **kwargs)
    return result


def toolchain(compiler):
    if not Path(compiler).is_file():
        raise ValueError("GCC 10 toolchain is missing. Build and use docker/native-build.Dockerfile.")
    def output(option):
        return subprocess.check_output([compiler, option], text=True).strip()
    version = output("-dumpfullversion")
    if version != "10.5.0":
        raise ValueError("Expected GCC 10.5.0, got " + version)
    def program(option):
        value = output(option)
        resolved = shutil.which(value) if not Path(value).is_absolute() else value
        if not resolved:
            raise ValueError("Compiler program is missing: " + value)
        return Path(resolved).resolve()
    backend = program("-print-prog-name=cc1plus")
    linker = program("-print-prog-name=ld")
    standard_library = output("-print-file-name=libstdc++.so")
    libc = output("-print-file-name=libc.so.6")
    packages = subprocess.check_output([
        "dpkg-query", "-W", "-f=${binary:Package}=${Version}\n",
        "gcc-10", "g++-10", "libstdc++-10-dev", "libc6-dev",
        "gcc-10-aarch64-linux-gnu", "g++-10-aarch64-linux-gnu",
        "libstdc++-10-dev-arm64-cross", "libc6-dev-arm64-cross",
        "binutils", "binutils-aarch64-linux-gnu"], text=True).splitlines()
    return {"compiler": compiler, "version": version, "triple": output("-dumpmachine"),
            "compiler_sha256": sha256(compiler),
            "backend_sha256": sha256(backend), "linker_sha256": sha256(linker),
            "libstdcxx": standard_library, "libstdcxx_sha256": sha256(standard_library),
            "libc": libc, "libc_sha256": sha256(libc), "packages": sorted(packages)}


def abi_summary(path, platform):
    value = validate_library(path, platform)
    return {key: value[key] for key in ("machine", "max_versions", "needed", "rpaths", "validated")}


def prepare(args):
    project, build, classes, repository = [Path(value).resolve() for value in
        (args.project_root, args.build_directory, args.classes_directory, args.maven_repository)]
    manifest_file = project / "scripts/tensorflow-native-inputs.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    for field, actual in (("tensorflow_java", args.tensorflow_java_version),
                          ("tensorflow_runtime", args.tensorflow_runtime_version),
                          ("javacpp", args.javacpp_version), ("protobuf", args.protobuf_version)):
        if manifest[field] != actual:
            raise ValueError("Native input lock does not match Maven property " + field)
    work = build / "tensorflow-native"
    work.mkdir(parents=True, exist_ok=True)
    javacpp = repository / ("org/bytedeco/javacpp/{0}/javacpp-{0}.jar".format(args.javacpp_version))
    protobuf = repository / ("com/google/protobuf/protobuf-java/{0}/protobuf-java-{0}.jar".format(args.protobuf_version))
    for dependency in (javacpp, protobuf):
        if not dependency.is_file():
            raise ValueError("Maven dependency missing: " + str(dependency))
    binding_files = sorted((classes / "org/tensorflow/internal/c_api").rglob("*.class"))
    if not binding_files:
        raise ValueError("Compile the Java 8 TensorFlow bindings before preparing native libraries")
    binding_key = fingerprint([(str(path.relative_to(classes)), sha256(path)) for path in binding_files])
    common = {"manifest": sha256(manifest_file), "builder": sha256(Path(__file__)),
              "abi_validator": sha256(project / "scripts/native_abi.py"),
              "build_image_spec": sha256(project / "docker/native-build.Dockerfile"),
              "bindings": binding_key, "javacpp": sha256(javacpp), "protobuf": sha256(protobuf),
              "flags": FLAGS, "limits": LIMITS}
    helper_directory = work / "upstream-native"
    for name, record in manifest["native_sources"].items():
        copy_changed(cached_download(record, repository, name), helper_directory / name)
    classpath = os.pathsep.join(str(path) for path in (classes, javacpp, protobuf))
    metadata = {"schema_version": 1, "tensorflow_java": args.tensorflow_java_version,
                "tensorflow_runtime": args.tensorflow_runtime_version, "javacpp": args.javacpp_version,
                "upstream_commit": manifest["upstream_commit"], "limits": dict(LIMITS), "platforms": {}}
    prepared = []
    for platform, compiler in PLATFORMS.items():
        machine_toolchain = toolchain(compiler)
        expected_triple = "aarch64" if platform == "linux-arm64" else "x86_64"
        if not machine_toolchain["triple"].startswith(expected_triple):
            raise ValueError("Incorrect compiler target: " + machine_toolchain["triple"])
        location = work / platform
        location.mkdir(exist_ok=True)
        record = manifest["platforms"][platform]
        cached_classifier = repository / ("org/tensorflow/tensorflow-core-native/{0}/tensorflow-core-native-{0}-{1}.jar".format(args.tensorflow_java_version, platform))
        classifier = download(record["classifier"], cached_classifier)
        library_directory = location / "official"
        extraction_state = location / "official.json"
        library_key = {"classifier": record["classifier"]["sha256"]}
        old_extraction = read_cache(extraction_state)
        if (old_extraction.get("input") != library_key or
                any(not (library_directory / name).is_file() or sha256(library_directory / name) != old_extraction.get("sha256", {}).get(name) for name in LIBRARIES)):
            with zipfile.ZipFile(str(classifier)) as archive:
                for name in LIBRARIES:
                    extract_file(archive, "org/tensorflow/internal/c_api/" + platform + "/" + name,
                                 library_directory / name)
            write_json(extraction_state, {"input": library_key,
                "sha256": {name: sha256(library_directory / name) for name in LIBRARIES}})
        # Builder links against the conventional unversioned library names.
        for name in LIBRARIES[1:]:
            link = library_directory / name[:-2]
            if not link.is_symlink() or os.readlink(str(link)) != name:
                if link.is_dir():
                    raise ValueError("Unexpected directory at native linker input: " + str(link))
                link.unlink(missing_ok=True)
                link.symlink_to(name)
        core_abi = {name: abi_summary(library_directory / name, platform) for name in LIBRARIES[1:]}
        inputs = dict(common, toolchain=machine_toolchain, platform=platform)
        key = fingerprint(inputs)
        state_file = location / "prepared.json"
        output = location / "output"
        jni = output / LIBRARIES[0]
        state = read_cache(state_file)
        reusable = state.get("input_fingerprint") == key and jni.is_file() and state.get("jni_sha256") == sha256(jni)
        if reusable:
            jni_abi = abi_summary(jni, platform)
            compare_jni_exports(jni, library_directory / LIBRARIES[0])
            print("TF_NATIVE_REUSE platform=" + platform, flush=True)
        else:
            state_file.unlink(missing_ok=True)
            print("TF_NATIVE_BUILD platform=" + platform, flush=True)
            wheel = cached_download(record["headers"], repository, platform + ".whl")
            headers = location / "headers"
            # A rebuild always restores headers from their verified archive. A cache hit
            # does not use staged headers, so it needs no expensive directory scan.
            extract_headers(wheel, headers, work)
            output.mkdir(exist_ok=True)
            command = ["java", "-cp", classpath, "org.bytedeco.javacpp.tools.Builder",
                       "-classpath", str(classes), "-properties", platform,
                       "-Dplatform.compiler=" + compiler,
                       "-Dplatform.includepath=" + os.pathsep.join(str(path) for path in (headers, helper_directory)),
                       "-Dplatform.linkpath=" + str(library_directory),
                       # An empty JavaCPP property falls back to its default rpath.
                       # A second -L is harmless and prevents absolute build rpaths.
                       "-Dplatform.linkpath.prefix2=-L", "-d", str(output), "-o", "jnitensorflow", "-nodelete"]
            for flag in FLAGS:
                command += ["-Xcompiler", flag]
            command += ["org.tensorflow.internal.c_api.**"]
            with (location / "build.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, cwd=str(project))
            if result.returncode:
                print((location / "build.log").read_text(encoding="utf-8")[-24000:], file=sys.stderr)
                raise ValueError("JNI build failed for " + platform + "; see " + str(location / "build.log"))
            jni_abi = abi_summary(jni, platform)
            exports = compare_jni_exports(jni, library_directory / LIBRARIES[0])
            write_json(state_file, {"input_fingerprint": key, "jni_sha256": sha256(jni),
                                   "toolchain": machine_toolchain, "abi": jni_abi, "exports": exports})
        platform_metadata = {"input_fingerprint": key, "jni_sha256": sha256(jni),
                             "source_classifier_sha256": record["classifier"]["sha256"],
                             "core_sha256": {name: sha256(library_directory / name) for name in LIBRARIES[1:]},
                             "abi": dict(core_abi, **{LIBRARIES[0]: jni_abi}), "toolchain": machine_toolchain}
        metadata["platforms"][platform] = platform_metadata
        prepared.append((jni, classes / "org/tensorflow/internal/c_api" / platform / LIBRARIES[0]))
        # A directory classpath (Surefire) must be as self-contained as the final Jar:
        # the ELF $ORIGIN lookup cannot resolve dependencies inside a different Jar.
        for name in LIBRARIES[1:]:
            prepared.append((library_directory / name, classes / "org/tensorflow/internal/c_api" / platform / name))
    # Both architectures must succeed before publishing project resources or provenance.
    for source, destination in prepared:
        copy_changed(source, destination)
    write_json(classes / "META-INF/tensorflow-native-build.json", metadata)
    write_json(work / "native-build.json", metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project-root", "build-directory", "classes-directory", "maven-repository",
                 "tensorflow-java-version", "tensorflow-runtime-version", "javacpp-version", "protobuf-version"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        prepare(args)
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print("TensorFlow native preparation failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
