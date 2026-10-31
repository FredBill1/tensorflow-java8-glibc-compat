FROM maven:3.9.9-eclipse-temurin-8@sha256:e20e4fc05fb5480c884a1f750c362ed0e91aca15cec5736e12796713710ec9b3 AS maven
FROM eclipse-temurin:8-jdk-focal@sha256:c135cf05131eeb3cd22aa2dd5e02d4f937a0ebf8059b8493fb147a702964814e

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 binutils \
      gcc-10=10.5.0-1ubuntu1~20.04 g++-10=10.5.0-1ubuntu1~20.04 \
      gcc-10-aarch64-linux-gnu=10.5.0-1ubuntu1~20.04cross1 \
      g++-10-aarch64-linux-gnu=10.5.0-1ubuntu1~20.04cross1 \
      libstdc++-10-dev-arm64-cross=10.5.0-1ubuntu1~20.04cross1 \
      libgcc-10-dev-arm64-cross=10.5.0-1ubuntu1~20.04cross1 \
      libc6-dev-arm64-cross=2.31-0ubuntu9.9cross1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=maven /usr/share/maven /usr/share/maven
ENV MAVEN_HOME=/usr/share/maven
ENV PATH="/usr/share/maven/bin:${PATH}"
WORKDIR /workspace
CMD ["mvn", "-B", "verify"]
