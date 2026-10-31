# Testing and release validation

These commands validate the compatibility JAR's Java 8 bytecode, native ABI, dependency isolation, and SavedModel inference.

## Fast Python checks

Use Python 3.8 or newer from the repository root. These checks also run with Python 3.8 in the native-build image:

```sh
python -m unittest discover -s tests -p "test_*.py"
```

These checks exercise standard-library build, download-cache, and release behavior without requiring TensorFlow in the host Python environment. Native execution checks are separate.

## Build checks

```sh
docker build --platform linux/amd64 -f docker/native-build.Dockerfile -t tensorflow-java8-glibc-compat-build:java8-gcc10 .
python scripts/build.py clean verify
```

Check Java 8 base-class bytecode, one complete native directory for each architecture, expected versions, build provenance, ABI imports, official-runtime content hashes, and matching JNI exports. Check that protobuf is isolated and the consumer POM has no dependency that reintroduces upstream runtime artifacts. Repeating the build without changes should reuse verified source and JNI preparation; corrupt or changed cached content must not be accepted silently.

Run `python tests/run_build_preparation.py` for the 17 source-preparation scenarios. The test uses tiny source JARs and a temporary workspace. On Linux its Maven container runs with the calling user's UID/GID, so generated files remain writable and removable by that user. Its private Maven repository is discarded with the test container and does not use the real build's Maven cache volume.

## Independent SavedModel fixtures

Fixture generation needs Python 3.12 and the test-only TensorFlow/Keras dependencies. They are not runtime or build requirements for consuming the JAR.

```sh
python -m venv .venv-model
```

On Linux/macOS:

```sh
.venv-model/bin/python -m pip install --require-hashes -r tests/model-requirements.lock
.venv-model/bin/python tests/export_models.py
```

On Windows PowerShell:

```powershell
.venv-model\Scripts\python.exe -m pip install --require-hashes -r tests/model-requirements.lock
.venv-model\Scripts\python.exe tests/export_models.py
```

The exporter runs Keras 3 and legacy Keras separately and writes `tests/fixtures/` with two SavedModels, producer metadata, encoded inputs, and expected outputs. Fixtures must match TensorFlow 2.21.0. Generated models, virtual environments, and reports are ignored by Git.

## Java 8 native execution matrix

After building the artifact and exporting fixtures:

```sh
python tests/run_native_compat.py
```

The default matrix executes the same artifact in four containers:

| Architecture | Baseline | Modern |
| --- | --- | --- |
| Linux amd64 | Ubuntu 18.04 / GLIBC 2.27 | Ubuntu 22.04 / GLIBC 2.35 |
| Linux arm64 | Ubuntu 18.04 / GLIBC 2.27 | Ubuntu 22.04 / GLIBC 2.35 |

The execution containers use Java 8 with the self-contained JAR and no separately installed Python or TensorFlow. Each case must load the correct TensorFlow runtime and execute both SavedModels against all 24 rows at batch sizes 1, 3, and 7. The expected coverage is 72 batches and 1,440 scalar comparisons per matrix case, including float, int64, embeddings, and UTF-8 string outputs. Check the summary for complete coverage, runtime versions, artifact identity, and ABI results.

ARM64 execution on an x86-64 Docker host requires an already configured runtime emulator. Buildx support alone does not establish that an ARM64 container can execute. The report distinguishes native execution and emulation; emulation is functional validation and provides no production performance measurement.

Use `--jar PATH`, `--fixtures PATH`, and `--output PATH` to select inputs and reports. Repeat `--platform linux/amd64` or `--platform linux/arm64` and `--environment baseline` or `--environment modern` to run a subset while debugging. `--skip-build` reuses test images. A subset is not a complete release matrix. By default reports are written under `test-artifacts/native-compat/`.

## Release review

```sh
python scripts/release.py
```

Inspect the files under `dist/`: the versioned JAR, consumer POM, `SHA256SUMS`, and license bundle. Verify checksum values after copying artifacts. Confirm the release includes the required licenses and excludes credentials, fixture models, and build caches.

For integration into an application, test the released JAR on the intended runtime classpath with that application's existing protobuf version present. Verify model outputs, native extraction permissions, and the CPU architectures used by the target JVMs.

The existing `verify.yml` GitHub Actions workflow runs lightweight Python checks on changes. A manually dispatched native verification run builds the image and artifact, exports the fixtures, runs the full matrix, and prepares local release files for inspection. That verification workflow does not upload Release assets.

## Automatic release assets

Publish a GitHub Release, including a pre-release, for a tag matching the artifact version, optionally prefixed with `v`. For the current version use `v1.2.0-compat.1` or `1.2.0-compat.1`. The tag's commit must contain the release workflow and matching versions in the Maven POM and packaging scripts. Publishing triggers `release.yml`; it builds the tagged code rather than the current `main` branch.

The workflow first checks the published Release and version consistency. The Release must permit adding assets after publication; an immutable Release is rejected before the expensive build. The job runs on GitHub-hosted Ubuntu 22.04 with a 90-minute timeout. Only the preflight and upload commands receive `GH_TOKEN`, and checkout does not persist credentials.

The required sequence is:

1. Run all standard-library unit tests, including release-publication tests.
2. Enable ARM64 emulation and build the pinned Java 8 toolchain image.
3. Execute all 17 source-preparation scenarios, then run `clean verify` and audit the final binary JAR and embedded consumer POM.
4. Export independent fixtures and execute the complete amd64/arm64 × baseline/modern matrix, including all 5,760 scalar comparisons.
5. Run `scripts/release.py` and verify `dist/SHA256SUMS`.
6. Upload the current nine checksummed payloads, verify them against GitHub, separately upload `SHA256SUMS`, and verify all ten assets. Old files in `dist/` that are absent from the current manifest are ignored.

After exporting the binary and source JARs, the workflow frees its own disposable runner's build volume, Maven volume, and Docker builder cache before installing fixture dependencies. It retains the exported files and diagnostics. This cleanup is guarded to GitHub-hosted runners and is not part of local build commands.

The job uses the repository's `GITHUB_TOKEN` with `contents: write`; no extra publishing secret is required. Runs for the same tag do not overlap or cancel an active upload. A retry compares existing asset SHA-256 values: identical files are skipped, absent files are uploaded, and a name with different contents fails without replacement. The workflow neither creates another Release nor uploads to Maven Central.

If a failed GitHub upload leaves an incomplete (`starter`) asset, remove that incomplete attachment before retrying. The helper does not delete it automatically. A rebuild of the same tag is not assumed to be byte-identical; if an already uploaded asset differs, retain the original validated files or publish a new version instead of overwriting it.

On failure, a small Actions diagnostic artifact retains logs and native summaries for seven days. It excludes `dist/`, JARs, fixtures, and build caches. Rerun the failed Release workflow after resolving a transient failure; changing the contents of an existing release version is rejected. Actions versions match `verify.yml`: `actions/checkout@v4`, `actions/setup-python@v5`, `docker/setup-qemu-action@v3`, and `actions/upload-artifact@v4`.

## Current verification

On 2026-10-31, release `1.2.0-compat.1` passed independent local verification:

| Check | Result |
| --- | --- |
| Python unit tests | 41 passed on Windows and with Python 3.8 in the native-build image, including 14 release-publication scenarios |
| Workflow lint | Both workflows passed actionlint 1.7.12, including shellcheck and pyflakes integrations |
| Real JDK 8 source-preparation regression | All 17 scenarios passed on Windows and on a Linux native filesystem with Python 3.12 and non-root UID/GID 1001; temporary files were cleaned up successfully |
| Maven `verify` and final JAR audit | Passed; Java 8 base classes, complete native resources, provenance, and dependency-free embedded Maven POM |
| Native execution matrix | All four amd64/arm64 × Ubuntu 18.04/22.04 cases passed |
| SavedModel outputs | Both Keras models, 24 rows, batches 1/3/7; 1,440 scalar comparisons per case, 5,760 total |
| Native ABI | All six libraries meet GLIBC 2.27 / GLIBCXX 3.4.22 / CXXABI 1.3.11; JNI exports match the official bindings |
| Official TensorFlow runtime libraries | Content hashes unchanged from the upstream artifacts |
| Local release preparation | `dist/` prepared with binary, sources, consumer POM, checksums, and notices |

The final binary JAR is 398,242,953 bytes and the patched Java source JAR is 5,363,686 bytes. All native cases used Java 8 and TensorFlow 2.21.0. ARM64 cases executed through QEMU on an x86-64 host; these results establish functional compatibility, not ARM64 performance. Reports are retained locally under `test-artifacts/native-compat/`.

The GitHub Actions workflows are provided but have not yet been executed in GitHub's hosted environment. The execution matrix covers standalone JVM inference on the listed platforms; applications should validate their own models and runtime environments.
