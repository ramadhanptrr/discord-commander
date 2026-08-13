from __future__ import annotations

import logging
import subprocess

from commander.config import ConfigSource, load_network_config


logger = logging.getLogger("commander.network")


class NetworkChecker:
    """Probe the home gateway directly from the container over the routed path.

    Reads the ``home_network`` group fresh from Turso on every call, so an edited timeout or
    default probe count applies to the next probe without a Commander restart.
    """

    def __init__(self, turso: ConfigSource) -> None:
        self._turso = turso

    def is_reachable(self, host: str | None = None, count: int | None = None) -> bool:
        config = load_network_config(self._turso)
        host = host or config.host
        count = count or config.manual_probe_count
        timeout = config.ping_timeout_seconds
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
