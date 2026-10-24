FROM debian:trixie

# Install minimal tools for kernel/proc inspection and Python experiments
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-rich \
    procps \
    coreutils \
    lsof \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /lab

CMD ["sleep", "infinity"]
