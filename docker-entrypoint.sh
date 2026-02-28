#!/bin/bash
set -e

# Fix ownership on mounted volumes so appuser can write to them.
# When Docker/Unraid bind-mounts host directories the host permissions
# override whatever was set at build time.
chown -R appuser:appuser /app/data /app/models /app/logs /opt/piper 2>/dev/null || true

if [ "$1" = "start-all" ]; then
    # Embedded mode: run voice bridge + Python bot in one container.
    # This is the default for single-container deployments (Unraid, standalone).
    # The bridge runs as a background process, the bot is the foreground process.
    export VOICE_BRIDGE_URL="${VOICE_BRIDGE_URL:-ws://localhost:9876}"
    export BRIDGE_PORT="${BRIDGE_PORT:-9876}"

    echo "Starting embedded voice bridge on port ${BRIDGE_PORT}..."
    gosu appuser node /app/voice_bridge/src/index.js &
    BRIDGE_PID=$!

    # Wait briefly for the bridge to start listening
    sleep 1

    # Verify bridge started
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "ERROR: Voice bridge failed to start"
        exit 1
    fi

    echo "Starting Discord voice assistant..."
    # Run bot in foreground; if it exits, also stop the bridge
    gosu appuser python -m discord_voice_assistant.main
    EXIT_CODE=$?

    # Clean up bridge on exit
    kill "$BRIDGE_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
    exit $EXIT_CODE
else
    # Direct command mode: run whatever was passed.
    # Used by docker-compose two-container setup (CMD ["python", "-m", "..."])
    exec gosu appuser "$@"
fi
