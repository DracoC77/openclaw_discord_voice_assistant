FROM python:3.11-slim AS base

# System dependencies for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    espeak-ng \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .
RUN pip install --no-cache-dir -e .

# Create data directories
RUN mkdir -p /app/data/voice_profiles /app/models /app/logs

# Runtime configuration
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV MODELS_DIR=/app/models

VOLUME ["/app/data", "/app/models", "/app/logs"]

ENTRYPOINT ["python", "-m", "clippy.main"]
