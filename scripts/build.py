#!/usr/bin/env python3
"""Build in a Linux Docker volume and export only deliverables to the checkout.

Usage: python scripts/build.py [Maven arguments...]
No arguments means verify; for a full rebuild use clean verify.
Build docker/native-build.Dockerfile first, as described in the README.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "tensorflow-java8-glibc-compat-build:java8-gcc10"
BUILD = "/build/target"
JAR = "tensorflow-java8-glibc-compat-1.2.0-compat.1.jar"
SOURCES_JAR = JAR[:-4] + "-sources.jar"
DIAGNOSTICS = ("dependency-reduced-pom.xml", "surefire-reports", "tensorflow-native/native-build.json",
               "tensorflow-native/linux-x86_64/build.log", "tensorflow-native/linux-arm64/build.log")


def build_names(root):
    key = hashlib.sha256(os.path.normcase(str(root.resolve())).encode("utf-8")).hexdigest()[:16]
    return "tensorflow-compat-build-" + key, "tensorflow-compat-target-" + key


def docker(*arguments, check=True):
    return subprocess.run(["docker", *arguments], check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, encoding="utf-8", errors="replace")


def real_directory(path):
    """Do not follow an existing output symlink or Windows junction."""
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Build output must be a real directory: " + str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_output(path):
    if path.is_symlink():
        path.unlink()
    elif path.resolve() != path:
        path.rmdir() if path.is_dir() else path.unlink()
    elif path.is_dir():
        shutil.rmtree(str(path))
    elif path.exists():
        path.unlink()


def stream_maven(command, log_path):
    if log_path.is_symlink() or log_path.resolve() != log_path:
        raise ValueError("Build log must be a real file: " + str(log_path))
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8", errors="replace", bufsize=1) as process:
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    print(line, end="", flush=True)
                return process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise


def export_outputs(container, target, publish_jar):
    paths = (JAR, SOURCES_JAR) + DIAGNOSTICS
    inspect = ("import json; from pathlib import Path; "
               "print(json.dumps([p for p in " + repr(paths) +
               " if (Path('/build/target') / p).exists()]))")
    present = set(json.loads(docker("exec", container, "python3", "-c", inspect).stdout))
    published = False
    with tempfile.TemporaryDirectory(prefix=".build-export-", dir=str(target)) as temporary:
        staging = Path(temporary)
        # Stage every selected output before publishing anything to the checkout.
        if publish_jar and JAR in present:
            docker("cp", container + ":" + BUILD + "/" + JAR, str(staging / JAR))
            if SOURCES_JAR not in present:
                raise ValueError("The build generated no patched sources jar")
            docker("cp", container + ":" + BUILD + "/" + SOURCES_JAR, str(staging / SOURCES_JAR))
        for relative in DIAGNOSTICS:
            destination = target / relative
            real_directory(destination.parent)
            if relative in present:
                incoming = staging / relative
                incoming.parent.mkdir(parents=True, exist_ok=True)
                docker("cp", container + ":" + BUILD + "/" + relative, str(incoming))
        for relative in DIAGNOSTICS:
            destination = target / relative
            if relative in present:
                remove_output(destination)
                shutil.move(str(staging / relative), str(destination))
            else:
                remove_output(destination)
        # A diagnostics/export error must also preserve the last successful Jar.
        if publish_jar and JAR in present:
            os.replace(str(staging / SOURCES_JAR), str(target / SOURCES_JAR))
            os.replace(str(staging / JAR), str(target / JAR))
            published = True
    return published


def build(arguments, root=ROOT):
    root = Path(root).resolve()
    container, volume = build_names(root)
    target = root / "target"
    created = False
    result = 1
    started = time.monotonic()
    print("Build volume: {} ({}); container: {}".format(volume, BUILD, container), flush=True)
    try:
        if docker("image", "inspect", IMAGE, check=False).returncode:
            raise ValueError("Build image is unavailable. Run: docker build --platform linux/amd64 "
                             "-f docker/native-build.Dockerfile -t " + IMAGE + " .")
        creation = docker("create", "--name", container, "--platform", "linux/amd64",
                          "-v", str(root) + ":/workspace:ro", "-v", volume + ":/build",
                          "-v", "tensorflow-compat-maven:/root/.m2",
                          "-w", "/workspace", IMAGE, "sleep", "infinity", check=False)
        if creation.returncode:
            if docker("container", "inspect", container, check=False).returncode == 0:
                raise ValueError("Another build owns container {}. Wait for it to finish. "
                                 "If a previous build was killed, inspect it with docker ps -a "
                                 "and remove the stopped container before retrying.".format(container))
            raise ValueError(creation.stderr.strip())
        created = True
        docker("start", container)
        real_directory(target)
        # Even a successful test/process-classes must never export an old cached Jar.
        prepare = ("from pathlib import Path; import shutil; p = Path('/build/target'); "
                   "(p / 'tensorflow-java8-glibc-compat-1.2.0-compat.1.jar').unlink(missing_ok=True); "
                   "shutil.rmtree(str(p / 'surefire-reports'), ignore_errors=True); "
                   "[(p / r).unlink(missing_ok=True) for r in "
                   "('tensorflow-native/linux-x86_64/build.log', 'tensorflow-native/linux-arm64/build.log')]")
        docker("exec", container, "python3", "-c", prepare)
        command = ["docker", "exec", container, "mvn", "-B", *(arguments or ["verify"]),
                   "-Dtensorflow.compat.build.directory=" + BUILD]
        print("Maven arguments: " + " ".join(command[4:]), flush=True)
        try:
            result = stream_maven(command, target / "build.log")
        except KeyboardInterrupt:
            result = 130
            # Killing the container stops the daemon-side exec before exporting logs.
            docker("stop", "--time", "1", container, check=False)
            print("Build interrupted; last successful host Jar is retained.", file=sys.stderr)
        if result != 130:
            try:
                published = export_outputs(container, target, publish_jar=result == 0)
                if published:
                    print("Exported: " + str(target / JAR), flush=True)
                elif result == 0:
                    print("No Jar generated by these Maven goals; host Jar was not changed.", flush=True)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                print("Output export failed: " + str(error), file=sys.stderr)
                if isinstance(error, subprocess.CalledProcessError) and error.stderr:
                    print(error.stderr, file=sys.stderr)
                if result == 0:
                    result = 1
    finally:
        if created:
            try:
                cleanup = docker("rm", "-f", container, check=False)
                if cleanup.returncode:
                    print("Container cleanup failed: " + cleanup.stderr, file=sys.stderr)
                    if result == 0:
                        result = 1
            except OSError as error:
                print("Container cleanup failed: " + str(error), file=sys.stderr)
                if result == 0:
                    result = 1
        print("Build elapsed: {:.1f}s; log: {}".format(time.monotonic() - started, target / "build.log"), flush=True)
    return result


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return build(sys.argv[1:])
    except KeyboardInterrupt:
        print("Build interrupted.", file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Build failed: " + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
