from __future__ import annotations

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
from commander.network import NetworkChecker
from commander.operations import OperatorOperations
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
        self._operations = OperatorOperations(
            config,
            EdgeController(config.edge),
            NetworkChecker(config.network),
            NasController(config.nas, mikrotik),
            PowerOperationState(),
            RateLimiter(),
        )
        self._guilds = [discord.Object(id=guild_id) for guild_id in config.discord.guild_ids]

        self.tree.add_command(
            StatusCommandGroup(self._authorizer, self._operations),
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

    async def show_panel(self, interaction: discord.Interaction) -> None:
        if not await self._authorizer.require_operator(interaction):
            return

        await interaction.response.send_message(
            embed=build_panel_embed(),
            view=CommanderPanel(self._authorizer, self._operations),
            ephemeral=False,
        )

    async def on_ready(self) -> None:
        logger.info("Discord Commander connected as %s", self.user)

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
