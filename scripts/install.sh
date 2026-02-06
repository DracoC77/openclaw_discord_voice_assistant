#!/bin/bash
# =============================================================================
# install.sh - Install Discord Voice Assistant alongside OpenClaw
# =============================================================================
# This script installs the voice assistant as a sidecar service alongside
# an existing OpenClaw Docker container. Run this on the Docker host (e.g.,
# your Unraid server).
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/DracoC77/openclaw_discord_voice_assistant/main/scripts/install.sh | bash
#
# Or manually:
#   git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git
#   cd openclaw_discord_voice_assistant
#   bash scripts/install.sh
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# Detect install directory
if [ -d "/mnt/user/appdata" ]; then
    # Unraid
    INSTALL_DIR="/mnt/user/appdata/discord-voice-assistant"
    info "Detected Unraid environment"
else
    INSTALL_DIR="${HOME}/discord-voice-assistant"
fi

# Allow override via env var
INSTALL_DIR="${DVA_INSTALL_DIR:-$INSTALL_DIR}"

info "Install directory: $INSTALL_DIR"

# --- Step 1: Clone or update the repository ---
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull origin main
else
    info "Cloning repository..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# --- Step 2: Create .env if it doesn't exist ---
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn "Created .env from template. You MUST edit it with your settings:"
    warn "  $INSTALL_DIR/.env"
    warn ""
    warn "Required:"
    warn "  DISCORD_BOT_TOKEN=your_token_here"
    warn "  OPENCLAW_URL=http://your_openclaw_container:3000"
    warn ""
    warn "Edit now? (Press Enter to continue, or Ctrl+C to edit first)"
    read -r || true
fi

# --- Step 3: Detect OpenClaw container ---
info "Looking for OpenClaw container..."
OPENCLAW_CONTAINER=""
for name in $(docker ps --format '{{.Names}}' 2>/dev/null); do
    if docker exec "$name" test -f /app/dist/index.js 2>/dev/null; then
        OPENCLAW_CONTAINER="$name"
        break
    fi
done

if [ -n "$OPENCLAW_CONTAINER" ]; then
    info "Found OpenClaw container: $OPENCLAW_CONTAINER"

    # Get the container's network
    NETWORK=$(docker inspect "$OPENCLAW_CONTAINER" --format '{{range $net, $conf := .NetworkSettings.Networks}}{{$net}}{{end}}' 2>/dev/null | head -1)
    if [ -n "$NETWORK" ] && [ "$NETWORK" != "bridge" ]; then
        info "OpenClaw is on network: $NETWORK"
        # Update docker-compose to use the same network
        if ! grep -q "external: true" "$INSTALL_DIR/docker-compose.yml"; then
            cat >> "$INSTALL_DIR/docker-compose.yml" <<EOF

networks:
  default:
    name: ${NETWORK}
    external: true
EOF
            info "Configured to use OpenClaw's Docker network: $NETWORK"
        fi
    fi

    # Suggest OPENCLAW_URL
    OPENCLAW_PORT=$(docker inspect "$OPENCLAW_CONTAINER" --format '{{range $p, $conf := .NetworkSettings.Ports}}{{$p}}{{end}}' 2>/dev/null | grep -o '[0-9]*' | head -1)
    OPENCLAW_PORT="${OPENCLAW_PORT:-18789}"
    SUGGESTED_URL="http://${OPENCLAW_CONTAINER}:${OPENCLAW_PORT}"
    info "Suggested OPENCLAW_URL: $SUGGESTED_URL"

    # Update .env if OPENCLAW_URL is still the default
    if grep -q "OPENCLAW_URL=http://localhost:18789" "$INSTALL_DIR/.env"; then
        sed -i "s|OPENCLAW_URL=http://localhost:18789|OPENCLAW_URL=${SUGGESTED_URL}|" "$INSTALL_DIR/.env"
        info "Updated OPENCLAW_URL in .env to: $SUGGESTED_URL"
    fi
else
    warn "No OpenClaw container found. Make sure to set OPENCLAW_URL manually in .env"
fi

# --- Step 4: Create data directories ---
mkdir -p "$INSTALL_DIR/data/voice_profiles" "$INSTALL_DIR/models" "$INSTALL_DIR/logs"

# --- Step 5: Build and start ---
info "Building Docker image..."
cd "$INSTALL_DIR"
docker compose build

info "Starting voice assistant..."
docker compose up -d

# --- Step 6: Verify ---
sleep 5
if docker compose ps | grep -q "Up"; then
    info "Discord Voice Assistant is running!"
    info ""
    info "View logs:     docker compose -f $INSTALL_DIR/docker-compose.yml logs -f"
    info "Stop:          docker compose -f $INSTALL_DIR/docker-compose.yml down"
    info "Configuration: $INSTALL_DIR/.env"
else
    error "Container failed to start. Check logs:"
    docker compose logs --tail=50
    exit 1
fi
