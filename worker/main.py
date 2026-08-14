from __future__ import annotations

import asyncio
import os
import signal

import discord

from shared.config import load_config, load_turso_config
from shared.discord_client import create_http_only_client
from shared.logger import setup_logger
from shared.mikrotik import MikroTikClient
from shared.nas import NasController
from shared.network import NetworkChecker
from shared.notifier import DiscordWatchdogNotifier
from shared.turso.cache_manager import TursoCacheManager
from shared.turso.writer import TursoProdWriter
from worker.nas_reminder import NasUptimeWatchdog
from worker.network_watchdog import NetworkWatchdog


logger = setup_logger("worker")

TURSO_LOCAL_DB_PATH = os.getenv("TURSO_LOCAL_DB_PATH", "/data/turso/local.db")


async def _shutdown(
    tasks: list[asyncio.Task[None]],
    turso_cache: TursoCacheManager,
    turso_writer: TursoProdWriter,
    discord_client: discord.Client,
) -> None:
    logger.info("Worker Pooling shutting down")
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Worker task ended with an unexpected error during shutdown")

    await turso_cache.aclose()
    await turso_writer.aclose()
    await discord_client.close()


async def main() -> None:
    turso_config = load_turso_config()
    turso_cache = TursoCacheManager(turso_config, TURSO_LOCAL_DB_PATH)
    # Worker Pooling is the sole owner of the shared local replica: it bootstraps it fresh on every
    # process start and is the only process that ever syncs it afterward. Commander only ever opens
    # read-only local connections against the same file. See migrations/worker_split_shared_cache.md.
    await turso_cache.bootstrap()
    turso_writer = TursoProdWriter(turso_config)

    config = load_config(turso_cache)
    discord_client = await create_http_only_client(config.discord.bot_token)
    notifier = DiscordWatchdogNotifier(
        discord_client,
        config.discord.watchdog_channel_id,
        config.network.watchdog_interval_seconds,
    )
    # Worker also owns Turso DOWN/RECOVERED alerting -- Commander deliberately does not attach a
    # notifier to its own (read-only) cache manager instance, so the alert isn't sent twice.
    turso_cache.attach_notifier(notifier)

    mikrotik = MikroTikClient(turso_cache)
    network_checker = NetworkChecker(turso_cache)
    nas = NasController(turso_cache, mikrotik)

    network_watchdog = NetworkWatchdog(network_checker, turso_cache, notifier, turso_writer)
    nas_uptime_watchdog = NasUptimeWatchdog(nas, turso_cache, notifier, turso_writer)

    tasks = [
        asyncio.create_task(network_watchdog.run(), name="network-watchdog"),
        asyncio.create_task(nas_uptime_watchdog.run(), name="nas-uptime-watchdog"),
        asyncio.create_task(turso_cache.run_periodic_sync(), name="turso-sync"),
    ]

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("Worker Pooling started")
    await stop_event.wait()

    await _shutdown(tasks, turso_cache, turso_writer, discord_client)


if __name__ == "__main__":
    asyncio.run(main())
