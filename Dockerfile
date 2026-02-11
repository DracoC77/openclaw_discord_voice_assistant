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

# Install Python dependencies into a virtual env we can copy cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the project itself
COPY . .
RUN pip install --no-cache-dir .

# ---- Runtime stage: slim image with only what's needed ----
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    espeak-ng \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -s /bin/bash appuser

# Create data directories and set ownership
RUN mkdir -p /app/data/voice_profiles /app/models /app/logs \
    && chown -R appuser:appuser /app

USER appuser

# Runtime configuration
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV MODELS_DIR=/app/models

VOLUME ["/app/data", "/app/models", "/app/logs"]

ENTRYPOINT ["python", "-m", "discord_voice_assistant.main"]
