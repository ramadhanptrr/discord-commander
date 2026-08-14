from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from commander.authorization import InteractionAuthorizer
from commander.edge import EdgeController
from commander.operations import OperatorOperations
from commander.power_state import PowerOperationState
from commander.ratelimit import RateLimiter
from commander.ui.panel import CommanderPanel, build_panel_embed
from shared.config import Config, load_config, load_internal_config, load_turso_config
from shared.logger import setup_logger
from shared.mikrotik import MikroTikClient
from shared.nas import NasController
from shared.network import NetworkChecker
from shared.notifier import DiscordWatchdogNotifier
from shared.turso.cache_manager import TursoCacheManager


logger = setup_logger()

TURSO_LOCAL_DB_PATH = os.getenv("TURSO_LOCAL_DB_PATH", "/data/turso/local.db")


class StatusCommandGroup(app_commands.Group):
    def __init__(
        self, authorizer: InteractionAuthorizer, operations: OperatorOperations
    ) -> None:
        super().__init__(name="status", description="Commander status operations")
        self._authorizer = authorizer
        self._operations = operations

    @app_commands.command(name="net", description="Check home network reachability")
    async def net(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.network_status(interaction)

    @app_commands.command(name="nas", description="Check NAS reachability")
    async def nas(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.nas_status(interaction)


class EdgeCommandGroup(app_commands.Group):
    def __init__(
        self, authorizer: InteractionAuthorizer, operations: OperatorOperations
    ) -> None:
        super().__init__(name="edge", description="Internal edge-host operations")
        self._authorizer = authorizer
        self._operations = operations

    @app_commands.command(name="info", description="Run the fixed edge information script")
    async def info(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.edge_info(interaction)


class WakeCommandGroup(app_commands.Group):
    def __init__(
        self, authorizer: InteractionAuthorizer, operations: OperatorOperations
    ) -> None:
        super().__init__(name="wake", description="Wake device operations")
        self._authorizer = authorizer
        self._operations = operations

    @app_commands.command(name="nas", description="Request Wake-on-LAN for NAS")
    async def nas(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.request_wake(interaction, self._authorizer)


class ShutdownCommandGroup(app_commands.Group):
    def __init__(
        self, authorizer: InteractionAuthorizer, operations: OperatorOperations
    ) -> None:
        super().__init__(name="shutdown", description="Shutdown device operations")
        self._authorizer = authorizer
        self._operations = operations

    @app_commands.command(name="nas", description="Request graceful NAS shutdown")
    async def nas(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.request_shutdown(interaction, self._authorizer)


class CommanderBot(commands.Bot):
    """Discord transport for the first Commander migration phase."""

    def __init__(self, config: Config, turso_cache: TursoCacheManager) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self._config = config
        self._authorizer = InteractionAuthorizer(config.discord)
        mikrotik = MikroTikClient(turso_cache)
        network_checker = NetworkChecker(turso_cache)
        power_state = PowerOperationState()
        watchdog_notifier = DiscordWatchdogNotifier(
            self,
            config.discord.watchdog_channel_id,
            config.network.watchdog_interval_seconds,
        )
        self._watchdog_notifier = watchdog_notifier
        self._turso_cache = turso_cache
        self._turso_sync_task: asyncio.Task[None] | None = None
        nas = NasController(turso_cache, mikrotik)
        self._operations = OperatorOperations(
            turso_cache,
            EdgeController(turso_cache),
            network_checker,
            nas,
            power_state,
            RateLimiter(),
        )
        self._startup_notification_sent = False
        self._startup_notification_lock = asyncio.Lock()
        self._guilds = [discord.Object(id=guild_id) for guild_id in config.discord.guild_ids]

        self.tree.add_command(
            StatusCommandGroup(self._authorizer, self._operations),
            guilds=self._guilds,
        )
        self.tree.add_command(
            app_commands.Command(
                name="ping",
                description="Check whether Commander is online",
                callback=self.ping,
            ),
            guilds=self._guilds,
        )
        self.tree.add_command(
            app_commands.Command(
                name="help",
                description="Show Commander commands",
                callback=self.help,
            ),
            guilds=self._guilds,
        )
        self.tree.add_command(
            app_commands.Command(
                name="start",
                description="Show the Commander welcome panel",
                callback=self.show_start,
            ),
            guilds=self._guilds,
        )
        self.tree.add_command(
            EdgeCommandGroup(self._authorizer, self._operations),
            guilds=self._guilds,
        )
        self.tree.add_command(
            WakeCommandGroup(self._authorizer, self._operations),
            guilds=self._guilds,
        )
        self.tree.add_command(
            ShutdownCommandGroup(self._authorizer, self._operations),
            guilds=self._guilds,
        )
        self.tree.add_command(
            app_commands.Command(
                name="panel",
                description="Show the Commander control panel",
                callback=self.show_panel,
            ),
            guilds=self._guilds,
        )

    async def setup_hook(self) -> None:
        # Static component IDs let an existing panel survive a container restart.
        self.add_view(CommanderPanel(self._authorizer, self._operations))
        await self._validate_configured_channels()

        for guild in self._guilds:
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %s application command(s) to guild %s", len(synced), guild.id)

    async def _validate_configured_channels(self) -> None:
        configured_channels = {
            "control room": self._config.discord.control_room_channel_id,
            "watchdog": self._config.discord.watchdog_channel_id,
        }

        if len(set(configured_channels.values())) != len(configured_channels):
            raise RuntimeError(
                "DISCORD_CONTROL_ROOM_CHANNEL_ID and DISCORD_WATCHDOG_CHANNEL_ID must differ"
            )

        for label, channel_id in configured_channels.items():
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.DiscordException as error:
                raise RuntimeError(
                    f"Could not fetch configured Discord {label} channel ({channel_id})"
                ) from error

            guild = getattr(channel, "guild", None)
            if guild is None or guild.id not in self._config.discord.guild_ids:
                raise RuntimeError(
                    f"Configured Discord {label} channel ({channel_id}) is outside allowed guilds"
                )
            if not isinstance(channel, discord.abc.Messageable):
                raise RuntimeError(
                    f"Configured Discord {label} channel ({channel_id}) cannot receive messages"
                )

    async def show_panel(self, interaction: discord.Interaction) -> None:
        if not await self._authorizer.require_operator(interaction):
            return
        await self._send_panel(interaction)

    async def show_start(self, interaction: discord.Interaction) -> None:
        if not await self._authorizer.require_operator(interaction):
            return
        await self._send_panel(interaction, content="🤖 Discord Commander is active.")

    async def _send_panel(self, interaction: discord.Interaction, content: str | None = None) -> None:
        await interaction.response.send_message(
            content=content,
            embed=build_panel_embed(),
            view=CommanderPanel(self._authorizer, self._operations),
            ephemeral=False,
        )

    async def ping(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.ping(interaction)

    async def help(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.help(interaction)

    async def on_ready(self) -> None:
        logger.info("Discord Commander connected as %s", self.user)
        if self._turso_sync_task is None or self._turso_sync_task.done():
            self._turso_sync_task = asyncio.create_task(
                self._turso_cache.run_periodic_sync(), name="turso-sync"
            )
        await self._notify_startup_once()

    async def _notify_startup_once(self) -> None:
        """Notify the watchdog channel once for each newly started Commander process."""
        async with self._startup_notification_lock:
            if self._startup_notification_sent:
                return

            try:
                internal_config = load_internal_config(self._turso_cache)
            except Exception:
                logger.exception(
                    "Could not read ENABLE_STARTUP_NOTIFICATION from Turso; "
                    "defaulting to sending the Commander startup notification"
                )
                internal_config = None

            if internal_config is not None and not internal_config.enable_startup_notification:
                self._startup_notification_sent = True
                logger.info(
                    "Commander startup notification skipped (ENABLE_STARTUP_NOTIFICATION=FALSE)"
                )
                return

            try:
                await self._watchdog_notifier.send_commander_started()
            except Exception:
                logger.exception("Could not send Commander startup notification to watchdog channel")
                return

            self._startup_notification_sent = True
            logger.info("Sent Commander startup notification to watchdog channel")

    async def close(self) -> None:
        if self._turso_sync_task is not None and not self._turso_sync_task.done():
            self._turso_sync_task.cancel()
            try:
                await self._turso_sync_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Turso sync task ended with an unexpected error during shutdown")
        logger.info("Stopping Turso database synchronization task.")
        await self._turso_cache.aclose()
        await super().close()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.error("Unhandled application-command error", exc_info=error)
        message = "An internal error occurred while processing this command. Please try again later."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.DiscordException:
            logger.exception("Unable to send application-command error response")


def main() -> None:
    turso_config = load_turso_config()
    turso_cache = TursoCacheManager(turso_config, TURSO_LOCAL_DB_PATH)
    # Commander and Worker Pooling each own an independent local replica on their own volume (not
    # shared -- pyturso's synchronized connection holds a cross-process lock on the replica's -wal
    # file for its entire lifetime, so a second process opening its own read connections against
    # the same file fails outright, not just intermittently. See
    # migrations/worker_split_shared_cache.md). Fresh replica on every process start (see
    # machine_lore/ARCHITECTURE.md); this must finish before load_config() below, which reads
    # edge/NAS/home-network values out of the replica.
    asyncio.run(turso_cache.bootstrap())

    config = load_config(turso_cache)
    logger.info(
        "Starting Discord Commander for %s allowed guild(s); control channel=%s; watchdog channel=%s",
        len(config.discord.guild_ids),
        config.discord.control_room_channel_id,
        config.discord.watchdog_channel_id,
    )
    CommanderBot(config, turso_cache).run(config.discord.bot_token, log_handler=None)


if __name__ == "__main__":
    main()
