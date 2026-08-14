from __future__ import annotations

import logging
import subprocess

from shared.config import ConfigSource, load_mikrotik_config


logger = logging.getLogger("shared.mikrotik")


class MikroTikClient:
    """Execute the fixed RouterOS operations used by Commander through SSH.

    Identity comes from Infisical and the shared SSH key path from Turso; both are re-read fresh
    on every call so a Turso-side SSH key path change applies without a restart.
    """

    def __init__(self, turso: ConfigSource) -> None:
        self._turso = turso

    def _ssh(self, command: str, timeout: int = 15) -> tuple[bool, str]:
        config = load_mikrotik_config(self._turso)
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            str(config.port),
        ]
        if config.ssh_key_path:
            ssh_command.extend(["-i", config.ssh_key_path])
        ssh_command.extend([f"{config.username}@{config.host}", command])

        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "RouterOS SSH command timed out."
        except Exception:
            logger.exception("RouterOS SSH command could not be started")
            return False, "RouterOS SSH command could not be started."

        output = f"{result.stdout or ''}{result.stderr or ''}".replace("\r", "").strip()
        if result.returncode != 0:
            return False, output or f"RouterOS SSH command failed (exit {result.returncode})."
        return True, output

    def ping(self, address: str) -> bool:
        success, output = self._ssh(f"/ping address={address} count=1")
        return success and "received=0" not in output.lower()

    def send_wol(self, mac: str, interface: str) -> tuple[bool, str | None]:
        # Interface names may contain spaces. Validation rejects quotes and RouterOS control
        # characters, so quoting preserves the configured name without allowing command injection.
        success, output = self._ssh(f'/tool wol interface="{interface}" mac={mac}')
        if success:
            return True, None
        return False, output or "Wake-on-LAN request failed."
