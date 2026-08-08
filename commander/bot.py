from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from commander.authorization import InteractionAuthorizer
from commander.config import Config, load_config
from commander.edge import EdgeController
from commander.logger import setup_logger
from commander.mikrotik import MikroTikClient
from commander.nas import NasController
from commander.nasmonitor import NasUptimeWatchdog
from commander.netmonitor import NetworkWatchdog
from commander.network import NetworkChecker
from commander.notifier import DiscordWatchdogNotifier
from commander.operations import OperatorOperations
from commander.panel_state import CommanderPanelLocation, CommanderPanelStore
from commander.power_state import PowerOperationState
from commander.ratelimit import RateLimiter
from commander.ui.panel import CommanderPanel, build_panel_embed


logger = setup_logger()


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

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self._config = config
        self._authorizer = InteractionAuthorizer(config.discord)
        mikrotik = MikroTikClient(config.mikrotik)
        network_checker = NetworkChecker(config.network)
        power_state = PowerOperationState()
        watchdog_notifier = DiscordWatchdogNotifier(
            self,
            config.discord.watchdog_channel_id,
            config.network.watchdog_interval_seconds,
        )
        nas = NasController(config.nas, mikrotik)
        self._operations = OperatorOperations(
            config,
            EdgeController(config.edge),
            network_checker,
            nas,
            power_state,
            RateLimiter(),
        )
        self._network_watchdog = NetworkWatchdog(
            network_checker,
            config.network,
            watchdog_notifier,
        )
        self._nas_uptime_watchdog = NasUptimeWatchdog(
            nas,
            config.nas_watchdog,
            power_state,
            watchdog_notifier,
        )
        self._operations.set_network_watchdog(self._network_watchdog)
        self._network_watchdog_task: asyncio.Task[None] | None = None
        self._nas_uptime_watchdog_task: asyncio.Task[None] | None = None
        self._panel_store = CommanderPanelStore()
        self._panel_lock = asyncio.Lock()
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
        logger.info("Persistent Commander panel view registered")
        await self._validate_configured_channels()
        await self._recover_stored_panel()

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
        await self._ensure_panel(interaction)

    async def show_start(self, interaction: discord.Interaction) -> None:
        if not await self._authorizer.require_operator(interaction):
            return
        await self._ensure_panel(interaction, content="🤖 Discord Commander is active.")

    async def _recover_stored_panel(self) -> None:
        """Check the saved panel at startup without creating a replacement."""
        panel, unavailable = await self._find_stored_panel()
        if panel is not None:
            logger.info(
                "Existing Commander panel found: channel=%s message=%s",
                panel.channel.id,
                panel.id,
            )
        elif unavailable:
            logger.warning("Could not validate the stored Commander panel during startup")

    async def _ensure_panel(
        self, interaction: discord.Interaction, content: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self._panel_lock:
            existing_panel, unavailable = await self._find_stored_panel()
            if existing_panel is not None:
                logger.info(
                    "Existing Commander panel found: channel=%s message=%s",
                    existing_panel.channel.id,
                    existing_panel.id,
                )
                await interaction.followup.send(
                    "The Commander panel is already available in this control room.",
                    ephemeral=True,
                )
                return

            if unavailable:
                await interaction.followup.send(
                    "I could not verify the existing Commander panel. Check the bot's channel "
                    "permissions before creating another one.",
                    ephemeral=True,
                )
                return

            channel = interaction.channel
            if not isinstance(channel, discord.abc.Messageable):
                logger.warning("Commander panel could not be created: control-room channel is unavailable")
                await interaction.followup.send(
                    "I could not access this control-room channel to create the panel.",
                    ephemeral=True,
                )
                return

            try:
                panel = await channel.send(
                    content=content,
                    embed=build_panel_embed(),
                    view=CommanderPanel(self._authorizer, self._operations),
                )
            except discord.Forbidden:
                logger.warning("Commander panel could not be created: missing channel permissions")
                await interaction.followup.send(
                    "I do not have permission to create the Commander panel in this channel.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                logger.exception("Discord API error while creating Commander panel")
                await interaction.followup.send(
                    "Discord could not create the Commander panel. Please try again later.",
                    ephemeral=True,
                )
                return

            try:
                self._panel_store.save(
                    CommanderPanelLocation(channel_id=panel.channel.id, message_id=panel.id)
                )
            except OSError:
                logger.exception("Commander panel was created but its location could not be saved")
                persistence_message = (
                    " The panel was created, but its location could not be saved for restart recovery."
                )
            else:
                logger.info(
                    "Commander panel created: channel=%s message=%s", panel.channel.id, panel.id
                )
                persistence_message = ""

            await self._pin_panel(panel)
            await interaction.followup.send(
                f"Commander panel created.{persistence_message}", ephemeral=True
            )

    async def _find_stored_panel(self) -> tuple[discord.Message | None, bool]:
        """Return ``(panel, unavailable)`` so API failures never cause a duplicate panel."""
        location = self._panel_store.load()
        if location is None:
            return None, False

        if location.channel_id != self._config.discord.control_room_channel_id:
            logger.warning(
                "Stored Commander panel channel=%s does not match the configured control room=%s",
                location.channel_id,
                self._config.discord.control_room_channel_id,
            )
            self._clear_stored_panel()
            return None, False

        try:
            channel = await self.fetch_channel(location.channel_id)
        except discord.NotFound:
            logger.warning("Stored Commander panel channel missing: channel=%s", location.channel_id)
            self._clear_stored_panel()
            return None, False
        except discord.Forbidden:
            logger.warning("Cannot view stored Commander panel channel: channel=%s", location.channel_id)
            return None, True
        except discord.HTTPException:
            logger.exception(
                "Discord API error while fetching stored Commander panel channel=%s", location.channel_id
            )
            return None, True

        if not isinstance(channel, discord.abc.Messageable):
            logger.warning(
                "Stored Commander panel channel cannot fetch messages: channel=%s", location.channel_id
            )
            return None, True

        try:
            panel = await channel.fetch_message(location.message_id)
        except discord.NotFound:
            logger.warning(
                "Stored Commander panel message missing: channel=%s message=%s",
                location.channel_id,
                location.message_id,
            )
            self._clear_stored_panel()
            return None, False
        except discord.Forbidden:
            logger.warning(
                "Cannot view stored Commander panel message: channel=%s message=%s",
                location.channel_id,
                location.message_id,
            )
            return None, True
        except discord.HTTPException:
            logger.exception(
                "Discord API error while fetching stored Commander panel: channel=%s message=%s",
                location.channel_id,
                location.message_id,
            )
            return None, True

        if self.user is not None and panel.author.id != self.user.id:
            logger.warning(
                "Stored Commander panel message is not owned by this bot: channel=%s message=%s",
                location.channel_id,
                location.message_id,
            )
            self._clear_stored_panel()
            return None, False

        return panel, False

    def _clear_stored_panel(self) -> None:
        try:
            self._panel_store.clear()
        except OSError:
            logger.exception("Could not clear missing Commander panel state")

    async def _pin_panel(self, panel: discord.Message) -> None:
        try:
            await panel.pin(reason="Commander persistent control panel")
        except discord.Forbidden:
            logger.warning(
                "Failed to pin Commander panel: missing Manage Messages permission "
                "(channel=%s message=%s)",
                panel.channel.id,
                panel.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to pin Commander panel: channel=%s message=%s", panel.channel.id, panel.id
            )
        else:
            logger.info("Commander panel pinned: channel=%s message=%s", panel.channel.id, panel.id)

    async def ping(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.ping(interaction)

    async def help(self, interaction: discord.Interaction) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.help(interaction)

    async def on_ready(self) -> None:
        logger.info("Discord Commander connected as %s", self.user)
        if self._network_watchdog_task is None or self._network_watchdog_task.done():
            self._network_watchdog_task = asyncio.create_task(
                self._network_watchdog.run(), name="network-watchdog"
            )
        if self._nas_uptime_watchdog_task is None or self._nas_uptime_watchdog_task.done():
            self._nas_uptime_watchdog_task = asyncio.create_task(
                self._nas_uptime_watchdog.run(), name="nas-uptime-watchdog"
            )

    async def close(self) -> None:
        watchdog_tasks = (
            self._network_watchdog_task,
            self._nas_uptime_watchdog_task,
        )
        for task in watchdog_tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in watchdog_tasks:
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Watchdog task ended with an unexpected error during shutdown")
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
    config = load_config()
    logger.info(
        "Starting Discord Commander for %s allowed guild(s); control channel=%s; watchdog channel=%s",
        len(config.discord.guild_ids),
        config.discord.control_room_channel_id,
        config.discord.watchdog_channel_id,
    )
    CommanderBot(config).run(config.discord.bot_token, log_handler=None)


if __name__ == "__main__":
    main()
