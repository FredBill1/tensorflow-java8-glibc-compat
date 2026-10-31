# Development

## Build contract

The release is one self-contained Java-only Maven artifact containing the patched TensorFlow Java API, generated protobuf classes, ndarray, JavaCPP, an isolated protobuf runtime, and complete native resource directories for Linux x86-64 and ARM64. It has no external runtime dependencies in its consumer POM.

The version `1.2.0-compat.1` identifies the first compatibility revision of TensorFlow Java 1.2.0. It uses TensorFlow native runtime 2.21.0. Treat these as a matched set: changing the Java version alone is not a supported upgrade.

Build with an actual JDK 8. ndarray uses `sun.misc.Unsafe`, so compiling with a recent JDK and `--release 8` is not an equivalent replacement. Base classes must use Java class-file major version 52 or earlier.

## Reproducible inputs and preparation

The source preparation step downloads the official `tensorflow-core-api`, `tensorflow-core-native`, and `tensorflow-ndarray` source artifacts. It verifies their fixed SHA-256 values and extracts Java source files without module descriptors. The version-specific patches replace Java local-variable `var` declarations and `List.copyOf` while preserving immutable-copy and null-rejection behavior.

The source preparation cache fingerprints its inputs and generated contents. Unchanged inputs and output files are reused without rewriting timestamps. Changed, missing, or added generated files invalidate the cache and the compiled output. Apply permanent changes as patches; do not edit generated source files.

`scripts/tensorflow-native-inputs.json` locks the native helper source commit, headers wheels, official native classifiers, versions, and hashes. Maven compiles the Java bindings before JNI generation. `scripts/prepare-tensorflow-native.py` uses JavaCPP and the pinned GCC 10.5.0 native and ARM64 cross-compilers to rebuild only `libjnitensorflow.so`.

Each native resource directory contains all three libraries:

```text
org/tensorflow/internal/c_api/linux-x86_64/
org/tensorflow/internal/c_api/linux-arm64/
    libjnitensorflow.so
    libtensorflow_cc.so.2
    libtensorflow_framework.so.2
```

The rebuilt JNI bridge and the corresponding two official TensorFlow runtime libraries must remain together. The runtime libraries are byte-for-byte copies of the official classifier entries. The build checks architecture, ABI imports, relative loader paths, and the JNI export-name set before publishing resources. It records provenance and hashes in `META-INF/tensorflow-native-build.json`.

## Docker build workflow

The build/release helpers and standard-library unit tests support Python 3.8 or newer, including Python 3.8 in the pinned native-build image. Exporting TensorFlow/Keras fixtures separately requires Python 3.12.

```sh
docker build --platform linux/amd64 -f docker/native-build.Dockerfile -t tensorflow-java8-glibc-compat-build:java8-gcc10 .
python scripts/build.py clean verify
```

For subsequent builds, use `python scripts/build.py` to retain incremental caches. Additional arguments are Maven arguments, for example `python scripts/build.py clean verify -DskipTests`. A build with skipped tests is not a complete release validation.

The source checkout is mounted read-only. The build directory and Maven cache reside in Docker volumes, avoiding generated-source and native-header writes through a Windows bind mount. Caches belong to this project and require no sibling checkout, application source tree, or manually prepared external directory. `clean` removes build output while retaining downloaded Maven inputs.

The host receives the release JAR only after a successful build. Diagnostics are also exported after failed attempts. A failed build does not overwrite a previous successful host JAR. Check the current command's result before preparing a release; the presence of an old JAR does not prove a new build succeeded.

On a Linux native filesystem, the image can also run Maven directly:

```sh
docker run --rm --platform linux/amd64 -v "$PWD:/workspace" -v tensorflow-compat-maven:/root/.m2 -w /workspace tensorflow-java8-glibc-compat-build:java8-gcc10 mvn -B clean verify
```

This writes `target/` through the bind mount and may create root-owned files. Prefer `scripts/build.py` on Windows and on WSL paths backed by Windows storage.

## Dependency isolation and packaging

Shade protobuf only into `io.github.fredbill1.tensorflow.compat.shaded.protobuf`. Preserve TensorFlow's Java API packages, JavaCPP packages, native declarations, and resource names. The consumer artifact must not depend on unrelocated protobuf. Clients that directly use protobuf helper types exposed by TensorFlow messages must use the relocated types from this artifact.

The release POM declares the compatibility artifact and its metadata without the upstream dependencies already embedded in the JAR. It must not cause consumers to download official TensorFlow, JavaCPP, or protobuf artifacts. Preserve upstream licenses and notices without allowing duplicate archive paths to overwrite distinct materials. The release preparation step checks the artifact and collects its accompanying license materials.

The binary JAR's embedded Maven POM is also dependency-free, so installing the JAR with Maven's `install-file` cannot reintroduce bundled runtime dependencies. The `-sources.jar` attachment contains the exact patched TensorFlow Java sources with protobuf references adjusted to match the published binary. It includes versioned source links for bundled JavaCPP and protobuf without introducing extra downloads. The `-source.zip` release archive contains the project's build scripts, patches, tests, and documentation.

Some upstream Java artifacts omit license resources. `third_party/licenses/inputs.json` records the exact upstream release URLs and SHA-256 values of vendored license texts. The build verifies these local texts before collecting them alongside notices extracted from the source/native JARs. These upstream texts retain their original bytes, including line endings; `.gitattributes` disables Git text conversion for them. Preserve this rule when updating a notice and its checksum.

## Upgrading

Update source versions and hashes, the native input lock, source patches, JavaCPP/protobuf versions, and the TensorFlow runtime version together. Review upstream generated bindings and native helper sources, rebuild both JNI bridges, and repeat all release checks. ABI limits must be checked on the actual ELF files; the build image's glibc version is not a statement of the artifact's minimum runtime requirement.

The current contract is GLIBC 2.27, GLIBCXX 3.4.22, and CXXABI 1.3.11. Rebuilding JNI cannot lower requirements imposed by the unchanged official TensorFlow C++ libraries. GPU support, other operating systems, custom TensorFlow operators, and alternate TensorFlow versions are outside this release.
