from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from commander.config import NasConfig
from commander.mikrotik import MikroTikClient


logger = logging.getLogger("commander.nas")


@dataclass(frozen=True)
class NasOperationResult:
    success: bool
    output: str


class NasController:
    """NAS power operations backed by fixed Infisical configuration only."""

    def __init__(self, config: NasConfig, mikrotik: MikroTikClient) -> None:
        self._config = config
        self._mikrotik = mikrotik

    def is_online(self) -> bool:
        return self._mikrotik.ping(self._config.ip)

    def wake(self) -> NasOperationResult:
        success, error = self._mikrotik.send_wol(
            mac=self._config.mac,
            interface=self._config.wol_interface,
        )
        if success:
            return NasOperationResult(True, "Wake-on-LAN packet sent.")
        return NasOperationResult(False, error or "Wake-on-LAN request failed.")

    def shutdown(self) -> NasOperationResult:
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            str(self._config.ssh_port),
        ]
        if self._config.ssh_key_path:
            ssh_command.extend(["-i", self._config.ssh_key_path])
        ssh_command.extend(
            [
                f"{self._config.ssh_user}@{self._config.ssh_host}",
                f"sudo {self._config.shutdown_script}",
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
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(self._config.ssh_port),
        ]
        if self._config.ssh_key_path:
            ssh_command.extend(["-i", self._config.ssh_key_path])
        ssh_command.extend(
            [
                f"{self._config.ssh_user}@{self._config.ssh_host}",
                self._config.uptime_script,
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
