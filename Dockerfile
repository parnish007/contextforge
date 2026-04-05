# ContextForge v5.0 Nexus — Multi-Stage Dockerfile
# ==================================================
# Stage 1: deps   — install Python dependencies
# Stage 2: nexus  — production image (final target)

# ── Stage 1: dependency cache ─────────────────────────────────────────────────
FROM python:3.11-slim AS deps

WORKDIR /install

# System libs required by sentence-transformers / cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install/pkgs -r requirements.txt


# ── Stage 2: nexus runtime ────────────────────────────────────────────────────
FROM python:3.11-slim AS nexus

# Non-root user for security
RUN useradd -ms /bin/bash nexus
USER nexus

WORKDIR /app

# Copy installed packages from deps stage
COPY --from=deps /install/pkgs /usr/local

# Copy source (bind-mount will overlay at runtime; this ensures image works standalone)
COPY --chown=nexus:nexus . /app

# Data directory must exist for SQLite init
RUN mkdir -p /app/data /app/.forge /app/benchmark/test_v5/logs

# Default: start MCP SSE server
EXPOSE 8765 9000
CMD ["python", "-m", "src.transport.server", "--sse", "--host", "0.0.0.0", "--port", "8765"]
