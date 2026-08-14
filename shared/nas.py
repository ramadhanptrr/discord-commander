from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from shared.config import ConfigSource, load_nas_config
from shared.mikrotik import MikroTikClient


logger = logging.getLogger("shared.nas")


@dataclass(frozen=True)
class NasOperationResult:
    success: bool
    output: str


class NasController:
    """NAS power operations. Reads the ``nas`` group fresh from Turso on every call, so a Turso
    edit applies to the next action without a Commander restart."""

    def __init__(self, turso: ConfigSource, mikrotik: MikroTikClient) -> None:
        self._turso = turso
        self._mikrotik = mikrotik

    def is_online(self) -> bool:
        config = load_nas_config(self._turso)
        return self._mikrotik.ping(config.ip)

    def wake(self) -> NasOperationResult:
        config = load_nas_config(self._turso)
        success, error = self._mikrotik.send_wol(
            mac=config.mac,
            interface=config.wol_interface,
        )
        if success:
            return NasOperationResult(True, "Wake-on-LAN packet sent.")
        return NasOperationResult(False, error or "Wake-on-LAN request failed.")

    def shutdown(self) -> NasOperationResult:
        config = load_nas_config(self._turso)
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            str(config.ssh_port),
        ]
        if config.ssh_key_path:
            ssh_command.extend(["-i", config.ssh_key_path])
        ssh_command.extend(
            [
                f"{config.ssh_user}@{config.ssh_host}",
                f"sudo {config.shutdown_script}",
            ]
        )

        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return NasOperationResult(False, "NAS shutdown SSH command timed out after 180 seconds.")
        except Exception:
            logger.exception("NAS shutdown SSH command could not be started")
            return NasOperationResult(False, "NAS shutdown SSH command could not be started.")

        output = f"{result.stdout or ''}{result.stderr or ''}".replace("\r", "").strip()
        if result.returncode != 0:
            return NasOperationResult(
                False,
                output or f"Shutdown script failed (exit {result.returncode}).",
            )
        return NasOperationResult(True, output or "Graceful shutdown script completed.")

    def get_boot_epoch(self) -> int | None:
        """Run the fixed unprivileged uptime script and return its Unix boot timestamp."""
        config = load_nas_config(self._turso)
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(config.ssh_port),
        ]
        if config.ssh_key_path:
            ssh_command.extend(["-i", config.ssh_key_path])
        ssh_command.extend(
            [
                f"{config.ssh_user}@{config.ssh_host}",
                config.uptime_script,
            ]
        )

        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            logger.warning("NAS uptime SSH command timed out")
            return None
        except Exception:
            logger.exception("NAS uptime SSH command could not be started")
            return None

        if result.returncode != 0:
            logger.warning("NAS uptime script failed with exit code %s", result.returncode)
            return None
        try:
            return int((result.stdout or "").strip())
        except ValueError:
            logger.warning("NAS uptime script did not return a Unix epoch")
            return None
