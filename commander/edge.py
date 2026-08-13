from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from commander.config import ConfigSource, load_edge_config


logger = logging.getLogger("commander.edge")


@dataclass(frozen=True)
class EdgeExecutionResult:
    success: bool
    output: str


class EdgeController:
    """Run the configured fixed edge-info script through the system SSH client.

    Reads the ``edge`` group fresh from the Turso local replica on every call, so a value edited
    in Turso applies to the next invocation without a Commander restart. A bad/missing value
    raises ``RuntimeError``, which the calling command handler turns into a Discord error reply.
    """

    def __init__(self, turso: ConfigSource) -> None:
        self._turso = turso

    def execute_info_script(self) -> EdgeExecutionResult:
        config = load_edge_config(self._turso)
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
            f"{config.ssh_user}@{config.internal_ip}",
            config.info_script,
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
