# These images run the final Jar. Native compilation happens in the separate build image.
ARG BASE_IMAGE=ubuntu:18.04@sha256:152dc042452c496007f07ca9127571cb9c29697f42acbfad72324b2bb2e43c98
FROM --platform=$BUILDPLATFORM ubuntu:22.04@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11 AS jdk
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
      amd64) arch=x64; checksum=5d64ae542b59a962b3caadadd346f4b1c3010879a28bb02d928326993de16e79 ;; \
      arm64) arch=aarch64; checksum=19552c1cf7f5c18290a6bdcd6757f70ea5c331a2bc0dd7a3b3120e8dbc4b4891 ;; \
      *) echo "Unsupported target architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fSL --connect-timeout 20 --max-time 600 --retry 4 "https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u462-b08/OpenJDK8U-jdk_${arch}_linux_hotspot_8u462b08.tar.gz" -o /tmp/jdk.tar.gz \
    && echo "$checksum  /tmp/jdk.tar.gz" | sha256sum -c - \
    && mkdir /jdk && tar -xzf /tmp/jdk.tar.gz --strip-components=1 -C /jdk \
    && rm /tmp/jdk.tar.gz

FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends binutils ca-certificates libstdc++6 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=jdk /jdk /opt/java
ENV JAVA_HOME=/opt/java PATH=/opt/java/bin:$PATH LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    TF_CPP_MIN_LOG_LEVEL=2 TF_NUM_INTRAOP_THREADS=1 TF_NUM_INTEROP_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /check
CMD ["java", "-version"]
