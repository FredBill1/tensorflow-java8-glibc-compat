# tensorflow-java8-glibc-compat

TensorFlow Java 1.2.0 and TensorFlow 2.21.0 CPU inference on Java 8, packaged as one self-contained JAR for Linux x86-64 and ARM64.

This project recompiles the upstream Java API for Java 8 and rebuilds the two JNI bridges with a pinned GCC 10 toolchain. The TensorFlow C++ runtime libraries come from the official artifacts and retain their original bytes. Building does not require Bazel, a TensorFlow source checkout, or a Python TensorFlow installation.

| Component | Version or requirement |
| --- | --- |
| Artifact | `io.github.fredbill1:tensorflow-java8-glibc-compat:1.2.0-compat.1` |
| TensorFlow Java / native runtime | 1.2.0 / 2.21.0 |
| Bundled JavaCPP / protobuf-java | 1.5.12 / 4.31.1 |
| Runtime | Java 8; Linux x86-64 or ARM64; CPU |
| Native ABI minimum | GLIBC 2.27, GLIBCXX 3.4.22, CXXABI 1.3.11 |

The original `org.tensorflow.*` and `org.bytedeco.javacpp.*` packages and native resource paths are preserved. The bundled protobuf runtime is relocated to `io.github.fredbill1.tensorflow.compat.shaded.protobuf` to coexist with applications that use another protobuf version. No additional TensorFlow, JavaCPP, or protobuf JAR is needed for this artifact.

## Build

Use Docker and Python 3.8 or newer on the host for the build, release scripts, and standard-library tests. Fixture export separately requires Python 3.12. Run commands from this repository root. The build image runs as Linux amd64 and cross-compiles ARM64 JNI; ARM64 hosts need Docker support for running amd64 images.

```sh
docker build --platform linux/amd64 -f docker/native-build.Dockerfile -t tensorflow-java8-glibc-compat-build:java8-gcc10 .
python scripts/build.py
```

The default Maven goal is `verify`. Use `python scripts/build.py clean verify` for a clean build. The result is `target/tensorflow-java8-glibc-compat-1.2.0-compat.1.jar`. The build script keeps intermediate output and caches in Linux Docker volumes and exports the deliverables to the host. A native compatibility matrix is a separate validation step; `verify` alone does not execute both architectures.

See [development](docs/development.md) for the source preparation, pinned inputs, incremental builds, and direct Maven workflow. See [testing](docs/testing.md) for independent SavedModel fixtures and the Java 8 native execution matrix.

Release `1.2.0-compat.1` passed the complete local Java 8 compatibility matrix on 2026-10-31; see [current verification](docs/testing.md#current-verification) for results and execution limits.

## Consume the JAR

Releases are local files or downloadable release assets; these coordinates do not imply publication to Maven Central. Install a downloaded or locally built JAR in the same Maven environment used to build your application:

```sh
mvn org.apache.maven.plugins:maven-install-plugin:3.1.4:install-file -Dfile=tensorflow-java8-glibc-compat-1.2.0-compat.1.jar -DgroupId=io.github.fredbill1 -DartifactId=tensorflow-java8-glibc-compat -Dversion=1.2.0-compat.1 -Dpackaging=jar -DgeneratePom=true
```

Declare the dependency normally. Use `provided` when a launcher supplies the compatibility JAR separately:

```xml
<dependency>
  <groupId>io.github.fredbill1</groupId>
  <artifactId>tensorflow-java8-glibc-compat</artifactId>
  <version>1.2.0-compat.1</version>
  <scope>provided</scope>
</dependency>
```

For a standalone Java application, include the compatibility JAR on the runtime classpath:

```sh
java -cp "application.jar:tensorflow-java8-glibc-compat-1.2.0-compat.1.jar" your.application.Main
```

Keep the compatibility JAR out of the application's shaded JAR. Application changes can then be delivered by replacing the smaller application JAR while retaining the same compatibility release. Do not add the official TensorFlow platform JARs alongside this artifact or relocate TensorFlow/JavaCPP packages.

The JAR contains both CPU architectures; JavaCPP selects the native resource matching the JVM's actual architecture. x86-64 CPUs must support the instructions required by the official TensorFlow CPU runtime, including AVX. The native extraction directory must be writable and permit loading shared libraries. This artifact does not bundle glibc, libstdc++, or libgcc.

## Prepare a release

```sh
python scripts/release.py
```

This validates the built artifact and prepares the binary JAR, patched Java `-sources.jar`, project `-source.zip`, a dependency-free consumer POM, `SHA256SUMS`, project notices, and `third-party-licenses.zip` under `dist/`. It does not create a remote repository or upload/publish anything. Complete the validation in [testing](docs/testing.md) before distributing a release.

To build and attach these files automatically, publish a GitHub Release for the matching version tag, such as `v1.2.0-compat.1`. Stable releases and pre-releases both trigger [the release workflow](.github/workflows/release.yml). It checks out the tag, checks the version and release state, runs the complete Java 8 verification and four-case native matrix, and then uploads the validated files to that Release. The existing [verification workflow](.github/workflows/verify.yml) remains available for ordinary changes and manual native checks.

This process requires a published, mutable Release: [immutable releases lock assets at publication](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases), so the workflow rejects them before building. If a run fails, rerun its jobs in GitHub Actions. Runs for the same tag are serialized; existing assets with identical SHA-256 values are retained, missing assets are uploaded, and different contents under an existing name cause failure. The workflow verifies all payloads before separately uploading `SHA256SUMS`. It does not replace release assets or publish to Maven Central. See [automatic release assets](docs/testing.md#automatic-release-assets) for the validation sequence and failure diagnostics.

The artifact includes large official native runtimes for two architectures. It is not designed to fit an 80 MB distribution limit. Splitting an upload into several JARs would not reduce its total size.

## License

Project code is licensed under [Apache License 2.0](LICENSE). Bundled upstream components retain their own licenses and notices; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), [NOTICE](NOTICE), and the license materials collected in the release. TensorFlow and JavaCPP remain the work of their respective upstream contributors. This is an independent compatibility project.
