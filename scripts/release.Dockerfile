FROM almalinux:9@sha256:02df05719ef287ebd0df0a1eedb9172a00bd70703aed7b25660e426b8e0d6047

RUN dnf install -y gcc-toolset-14-gcc binutils make patch bison ncurses-devel \
        python3.12 python3.12-pip git tar gzip findutils \
    && dnf clean all \
    && ln -s /usr/bin/python3.12 /usr/local/bin/python3

ENV PATH="/opt/rust/bin:/opt/rh/gcc-toolset-14/root/usr/bin:${PATH}" \
    RUSTUP_HOME="/opt/rustup" CARGO_HOME="/opt/rust"
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs | \
        sh -s -- -y --profile minimal --default-toolchain 1.100.0 --component rust-docs --no-modify-path \
    && chmod -R a+rX /opt/rust /opt/rustup
