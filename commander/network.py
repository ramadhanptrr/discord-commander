from __future__ import annotations

import logging
import subprocess

from commander.config import NetworkConfig


logger = logging.getLogger("commander.network")


class NetworkChecker:
    """Probe the home gateway directly from the container over the routed path."""

    def __init__(self, config: NetworkConfig) -> None:
        self._config = config

    def is_reachable(self, host: str | None = None, count: int | None = None) -> bool:
        host = host or self._config.host
        count = count or self._config.manual_probe_count
        timeout = self._config.ping_timeout_seconds
        command = [
            "ping",
            "-c",
            str(count),
            "-i",
            "1",
            "-W",
            str(timeout),
            host,
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=count + timeout + 10,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Ping to %s exceeded its subprocess timeout", host)
            return False
        except FileNotFoundError:
            logger.error("The `ping` binary is unavailable; install iputils-ping in the image")
            return False
        except Exception:
            logger.exception("Ping to %s failed unexpectedly", host)
            return False

        return result.returncode == 0
