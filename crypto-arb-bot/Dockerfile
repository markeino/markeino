# ─── Build Stage ─────────────────────────────────────────────────────────────
FROM rust:1.75-slim-bookworm AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    pkg-config \
    libssl-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Cache dependency compilation separately from source code
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src && echo "fn main() {}" > src/main.rs
RUN cargo build --release 2>/dev/null || true
RUN rm -f target/release/deps/crypto_arb_bot*

# Build the actual application
COPY src/ ./src/
COPY config/ ./config/
RUN cargo build --release

# ─── Runtime Stage ────────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS runtime

RUN apt-get update && apt-get install -y \
    ca-certificates \
    libssl3 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash arb
WORKDIR /home/arb

# Copy binary and config
COPY --from=builder /build/target/release/crypto-arb-bot ./crypto-arb-bot
COPY --from=builder /build/config/ ./config/

# Create logs directory
RUN mkdir -p logs && chown -R arb:arb /home/arb

USER arb

# Expose ports
# 8080 = health check
# 9090 = Prometheus metrics
EXPOSE 8080 9090

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["./crypto-arb-bot"]
