FROM debian:trixie

# Install uv binary for self-contained PEP 723 execution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Install minimal tools for kernel/proc inspection
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    procps \
    coreutils \
    lsof \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /lab

CMD ["sleep", "infinity"]
