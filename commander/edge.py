from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from commander.config import EdgeConfig


logger = logging.getLogger("commander.edge")


@dataclass(frozen=True)
class EdgeExecutionResult:
    success: bool
    output: str


class EdgeController:
    """Run the configured fixed edge-info script through the system SSH client."""

    def __init__(self, config: EdgeConfig) -> None:
        self._config = config

    def execute_info_script(self) -> EdgeExecutionResult:
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
            f"{self._config.ssh_user}@{self._config.internal_ip}",
            self._config.info_script,
        ]

        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return EdgeExecutionResult(False, "SSH execution timed out after 60 seconds.")
        except Exception:
            logger.exception("Edge-info SSH execution could not start")
            return EdgeExecutionResult(False, "SSH execution could not be started.")

        if result.returncode != 0:
            stderr = (result.stderr or "No error output returned.").replace("\r", "").strip()
            return EdgeExecutionResult(
                False,
                f"Script failed with exit code {result.returncode}.\n\n{stderr}",
            )

        output = (result.stdout or "").replace("\r", "").strip()
        return EdgeExecutionResult(True, output or "Script completed without output.")
