# Third-party materials

The compatibility artifact redistributes upstream code and native binaries. The project's Apache 2.0 license does not replace upstream licenses. The release license bundle retains the notices collected from the exact source and binary inputs used for that build; consult it alongside the files retained inside the JAR.

| Component | Version | Use | Upstream |
| --- | --- | --- | --- |
| TensorFlow Java core API, native bindings, and ndarray | 1.2.0 | Java sources recompiled for Java 8; compatibility patches applied | [tensorflow/java](https://github.com/tensorflow/java) |
| TensorFlow CPU C++ runtime | 2.21.0 | Official Linux x86-64 and ARM64 runtime libraries, unchanged | [tensorflow/tensorflow](https://github.com/tensorflow/tensorflow) |
| JavaCPP | 1.5.12 | JNI generation during the build; runtime loader and Java API in the JAR | [bytedeco/javacpp](https://github.com/bytedeco/javacpp) |
| protobuf-java | 4.31.1 | Embedded runtime relocated to the compatibility project's namespace | [protocolbuffers/protobuf](https://github.com/protocolbuffers/protobuf) |

TensorFlow and TensorFlow Java use Apache License 2.0. JavaCPP provides upstream license choices; preserve its full license and exception notices. Protobuf uses its upstream BSD-style license. The TensorFlow native artifacts include additional third-party software and associated notices; their complete bundled materials must accompany redistribution.

The build also downloads TensorFlow Python wheels only to obtain matching headers. Those wheels are build inputs, not Python packages installed into the runtime or included as wheels in the release. The native input lock records the source commit, URLs, and SHA-256 values. Docker base images, the JDK, Maven, Python, and GCC are build/test tools and are not copied into the compatibility JAR.

Compatibility changes include replacing Java syntax and collection calls unavailable on Java 8, rebuilding JNI with the pinned toolchain, and relocating the protobuf runtime and its references. The TensorFlow C++ runtime libraries retain the exact contents of their official artifact entries. Source patches and build provenance document these changes.

For a release, review the collected upstream notices, the project [LICENSE](LICENSE), and [NOTICE](NOTICE) together. Do not replace upstream notices with this summary or silently discard distinct notices that share a filename.
