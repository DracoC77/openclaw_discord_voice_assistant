# ---- Build stage: compile C extensions (webrtcvad) ----
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install CPU-only PyTorch first (saves ~1.8GB vs CUDA default)
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the project itself
COPY . .
RUN pip install --no-cache-dir .

# ---- Node.js binary source (just grab the binaries) ----
FROM node:20-slim AS node-bin

# ---- Node.js build stage: compile native modules against the runtime glibc ----
# IMPORTANT: This must use the same base image as the runtime stage so that
# native addons (@discordjs/opus, sodium-native) link against the correct
# glibc version.  Building in node:20-slim then copying into python:3.11-slim
# causes a glibc mismatch (e.g. 2.36 vs 2.41).
FROM python:3.11-slim AS node-builder

# Copy Node.js runtime from official image
COPY --from=node-bin /usr/local/bin/node /usr/local/bin/node
COPY --from=node-bin /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# Build tools needed to compile native addons (opus, sodium-native)
RUN apt-get update && apt-get install -y --no-install-recommends \
    make \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bridge
COPY voice_bridge/package.json .
# GCC 14 (in newer Debian) promotes -Wincompatible-pointer-types to an error,
# which breaks the bundled opus C source in @discordjs/opus v0.9.0.
# Downgrade it back to a warning so the native addon compiles.
RUN CFLAGS="-Wno-error=incompatible-pointer-types" npm install --omit=dev

# ---- Runtime stage: slim image with both Python and Node.js ----
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    espeak-ng \
    libsndfile1 \
    gosu \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js runtime (native modules were compiled against this same base image)
COPY --from=node-bin /usr/local/bin/node /usr/local/bin/node
COPY --from=node-bin /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# piper-phonemize bundles native .so files that the dynamic linker needs to find
RUN ldconfig /usr/local/lib/python3.11/site-packages/piper_phonemize.libs 2>/dev/null; \
    ldconfig /usr/local/lib/python3.11/site-packages/onnxruntime/capi 2>/dev/null; \
    ldconfig

# Copy voice bridge (pre-compiled native modules from node-builder)
COPY --from=node-builder /bridge/node_modules /app/voice_bridge/node_modules
COPY voice_bridge/src /app/voice_bridge/src
COPY voice_bridge/package.json /app/voice_bridge/package.json

# Copy application code
COPY . .

# Download default Piper TTS model (~75MB)
# Stored in /opt/piper (not /app/models which is a volume mount point)
RUN mkdir -p /opt/piper && \
    python -c "import urllib.request; \
    urllib.request.urlretrieve('https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx', '/opt/piper/en_US-hfc_male-medium.onnx'); \
    urllib.request.urlretrieve('https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx.json', '/opt/piper/en_US-hfc_male-medium.onnx.json')"

# Create non-root user
RUN useradd -m -s /bin/bash appuser

# Create data directories and set ownership
RUN mkdir -p /app/data /app/models /app/logs \
    && chown -R appuser:appuser /app

# Entrypoint fixes volume permissions then drops to appuser
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Runtime configuration
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV MODELS_DIR=/app/models

VOLUME ["/app/data", "/app/models", "/app/logs"]

# Proactive voice webhook server port
EXPOSE 18790

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["start-all"]
