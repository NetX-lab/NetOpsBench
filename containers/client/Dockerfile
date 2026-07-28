# syntax=docker/dockerfile:1
FROM rust:1.97.1-alpine3.24@sha256:3c38f3f82c2f3d73da3b38e18d279393a04cb43ddded0e35088a8c3324d40900 AS builder

RUN apk add --no-cache musl-dev
WORKDIR /src/native/client-agent
COPY native/client-agent/Cargo.toml native/client-agent/Cargo.lock native/client-agent/rust-toolchain.toml ./
COPY native/client-agent/src ./src
RUN cargo build --locked --release --target x86_64-unknown-linux-musl

FROM alpine:3.24.1@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b

RUN apk add --no-cache \
      bash \
      ca-certificates \
      curl \
      ethtool \
      iproute2 \
      iputils \
      procps \
      tcpdump \
      traceroute

COPY --from=builder \
  /src/native/client-agent/target/x86_64-unknown-linux-musl/release/netopsbench-client-agent \
  /usr/local/bin/netopsbench-client-agent

CMD ["sleep", "infinity"]
