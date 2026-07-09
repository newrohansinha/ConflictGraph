FROM rust:1-bookworm AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends clang llvm \
    && rm -rf /var/lib/apt/lists/*
RUN rustup toolchain install nightly --component rust-src \
    && cargo install bpf-linker --locked
WORKDIR /src
COPY tracer/ tracer/
COPY scripts/build-ebpf.sh scripts/build-ebpf.sh
RUN ./scripts/build-ebpf.sh

FROM debian:bookworm-slim
WORKDIR /opt/conflictgraph
COPY --from=build /src/tracer/target/release/conflictgraph-tracer /usr/local/bin/conflictgraph-tracer
COPY --from=build /src/tracer/target/bpfel-unknown-none/release/conflictgraph-ebpf tracer/target/bpfel-unknown-none/release/conflictgraph-ebpf
EXPOSE 9465
ENTRYPOINT ["conflictgraph-tracer"]
