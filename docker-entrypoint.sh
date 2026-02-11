#!/bin/bash
set -e

# Fix ownership on mounted volumes so appuser can write to them.
# When Docker/Unraid bind-mounts host directories the host permissions
# override whatever was set at build time.
chown -R appuser:appuser /app/data /app/models /app/logs 2>/dev/null || true

exec gosu appuser "$@"
