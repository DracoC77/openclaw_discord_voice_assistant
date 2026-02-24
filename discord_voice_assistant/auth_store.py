"""Persistent user authorization and agent routing store.

Backed by two JSON files in the data directory:
  - authorized_users.json: user IDs, roles (admin/user), metadata
  - agent_routing.json: per-user OpenClaw agent ID mappings

Bootstraps from AUTHORIZED_USER_IDS and ADMIN_USER_IDS env vars on first run.
Uses fcntl file locking for safe concurrent access.
"""

from __future__ import annotations

import fcntl
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_USER = "user"


class AuthStore:
    """Persistent authorization and agent routing store."""

    def __init__(
        self,
        data_dir: Path,
        bootstrap_user_ids: list[int] | None = None,
        bootstrap_admin_ids: list[int] | None = None,
        default_agent_id: str = "voice",
    ) -> None:
        self._data_dir = Path(data_dir)
        self._users_path = self._data_dir / "authorized_users.json"
        self._routes_path = self._data_dir / "agent_routing.json"
        self._default_agent_id = default_agent_id

        # In-memory caches
        self._users: dict[str, dict[str, Any]] = {}
        self._routes: dict[str, dict[str, Any]] = {}

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._load_or_bootstrap(
            bootstrap_user_ids=bootstrap_user_ids or [],
            bootstrap_admin_ids=bootstrap_admin_ids or [],
        )

    # ------------------------------------------------------------------
    # File I/O with locking
    # ------------------------------------------------------------------

    def _read_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            with open(path, "r") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read %s: %s", path, e)
            return {}

    def _write_json(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    json.dump(data, f, indent=2, sort_keys=True)
                    f.write("\n")
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            tmp.rename(path)
        except OSError as e:
            log.error("Failed to write %s: %s", path, e)

    # ------------------------------------------------------------------
    # Bootstrap & persistence
    # ------------------------------------------------------------------

    def _load_or_bootstrap(
        self,
        bootstrap_user_ids: list[int],
        bootstrap_admin_ids: list[int],
    ) -> None:
        """Load from disk, or bootstrap from env vars if files don't exist."""
        users_data = self._read_json(self._users_path)
        routes_data = self._read_json(self._routes_path)

        if users_data.get("users"):
            self._users = users_data["users"]
            log.info(
                "Loaded %d authorized user(s) from %s",
                len(self._users), self._users_path,
            )
        else:
            # Bootstrap from env vars
            now = datetime.now(timezone.utc).isoformat()
            for uid in bootstrap_admin_ids:
                self._users[str(uid)] = {
                    "role": ROLE_ADMIN,
                    "added_by": "env_bootstrap",
                    "added_at": now,
                }
            for uid in bootstrap_user_ids:
                uid_str = str(uid)
                if uid_str not in self._users:
                    self._users[uid_str] = {
                        "role": ROLE_USER,
                        "added_by": "env_bootstrap",
                        "added_at": now,
                    }
            if self._users:
                admin_count = sum(
                    1 for u in self._users.values() if u["role"] == ROLE_ADMIN
                )
                log.info(
                    "Bootstrapped %d user(s) from env vars (%d admin, %d user)",
                    len(self._users),
                    admin_count,
                    len(self._users) - admin_count,
                )
            self._save_users()

        if routes_data.get("routes"):
            self._routes = routes_data["routes"]
            log.info(
                "Loaded %d agent route(s) from %s",
                len(self._routes), self._routes_path,
            )
        else:
            self._save_routes()

    def _save_users(self) -> None:
        self._write_json(self._users_path, {"users": self._users})

    def _save_routes(self) -> None:
        self._write_json(self._routes_path, {"routes": self._routes})

    def reload(self) -> None:
        """Reload from disk (useful after external edits)."""
        users_data = self._read_json(self._users_path)
        routes_data = self._read_json(self._routes_path)
        self._users = users_data.get("users", {})
        self._routes = routes_data.get("routes", {})

    # ------------------------------------------------------------------
    # User authorization
    # ------------------------------------------------------------------

    def is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized. Fail-closed: empty store = deny all."""
        return str(user_id) in self._users

    def is_admin(self, user_id: int) -> bool:
        """Check if a user has admin role."""
        entry = self._users.get(str(user_id))
        return entry is not None and entry.get("role") == ROLE_ADMIN

    def get_role(self, user_id: int) -> str | None:
        """Get a user's role, or None if not authorized."""
        entry = self._users.get(str(user_id))
        return entry.get("role") if entry else None

    def get_all_users(self) -> dict[str, dict[str, Any]]:
        """Return a copy of all authorized users."""
        return dict(self._users)

    @property
    def user_count(self) -> int:
        return len(self._users)

    @property
    def admin_count(self) -> int:
        return sum(1 for u in self._users.values() if u.get("role") == ROLE_ADMIN)

    def add_user(
        self, user_id: int, role: str = ROLE_USER, added_by: int | str = "unknown"
    ) -> bool:
        """Add a user. Returns False if already exists."""
        uid_str = str(user_id)
        if uid_str in self._users:
            return False
        self._users[uid_str] = {
            "role": role,
            "added_by": str(added_by),
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_users()
        log.info("Added user %s with role %s (by %s)", user_id, role, added_by)
        return True

    def remove_user(self, user_id: int) -> bool:
        """Remove a user. Returns False if not found."""
        uid_str = str(user_id)
        if uid_str not in self._users:
            return False
        del self._users[uid_str]
        # Also remove any agent route
        self._routes.pop(uid_str, None)
        self._save_users()
        self._save_routes()
        log.info("Removed user %s", user_id)
        return True

    def promote_user(self, user_id: int) -> bool:
        """Promote a user to admin. Returns False if not found or already admin."""
        uid_str = str(user_id)
        entry = self._users.get(uid_str)
        if not entry or entry.get("role") == ROLE_ADMIN:
            return False
        entry["role"] = ROLE_ADMIN
        self._save_users()
        log.info("Promoted user %s to admin", user_id)
        return True

    def demote_user(self, user_id: int) -> bool:
        """Demote an admin to user. Returns False if not found or not admin."""
        uid_str = str(user_id)
        entry = self._users.get(uid_str)
        if not entry or entry.get("role") != ROLE_ADMIN:
            return False
        entry["role"] = ROLE_USER
        self._save_users()
        log.info("Demoted user %s to user", user_id)
        return True

    def is_last_admin(self, user_id: int) -> bool:
        """Check if this user is the only admin (lockout protection)."""
        if not self.is_admin(user_id):
            return False
        return self.admin_count <= 1

    # ------------------------------------------------------------------
    # Agent routing
    # ------------------------------------------------------------------

    def get_agent_id(self, user_id: int) -> str:
        """Get the agent ID for a user, falling back to default."""
        route = self._routes.get(str(user_id))
        if route and route.get("agent_id"):
            return route["agent_id"]
        return self._default_agent_id

    def set_agent_id(self, user_id: int, agent_id: str) -> None:
        """Set a per-user agent ID override."""
        uid_str = str(user_id)
        if uid_str not in self._routes:
            self._routes[uid_str] = {}
        self._routes[uid_str]["agent_id"] = agent_id
        self._save_routes()
        log.info("Set agent_id for user %s to %s", user_id, agent_id)

    def clear_agent_id(self, user_id: int) -> bool:
        """Remove a per-user agent ID override. Returns False if none existed."""
        uid_str = str(user_id)
        if uid_str in self._routes:
            del self._routes[uid_str]
            self._save_routes()
            log.info("Cleared agent_id override for user %s", user_id)
            return True
        return False

    def get_all_routes(self) -> dict[str, dict[str, Any]]:
        """Return a copy of all agent routes."""
        return dict(self._routes)

    @property
    def default_agent_id(self) -> str:
        return self._default_agent_id

    # ------------------------------------------------------------------
    # Session key helpers
    # ------------------------------------------------------------------

    def make_session_id(self, guild_id: int, channel_id: int, user_id: int) -> str:
        """Build a per-user session key for OpenClaw."""
        return f"voice:{guild_id}:{channel_id}:{user_id}"
