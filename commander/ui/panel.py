from __future__ import annotations

import discord

from commander.authorization import InteractionAuthorizer
from commander.operations import OperatorOperations


def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎛️ Discord Commander",
        description="Choose an available operation. All results are sent to this control room.",
        colour=discord.Colour.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Available now",
        value="Ping • Status NAS • Status Network • Edge Info • Wake NAS • Shutdown NAS",
        inline=False,
    )
    embed.add_field(name="Help", value="Use the Help button to view every command.", inline=False)
    embed.set_footer(text="Control room only")
    return embed


class CommanderPanel(discord.ui.View):
    """Persistent control-room panel; active buttons share command authorization."""

    def __init__(self, authorizer: InteractionAuthorizer, operations: OperatorOperations) -> None:
        super().__init__(timeout=None)
        self._authorizer = authorizer
        self._operations = operations

    @discord.ui.button(
        label="Status Network",
        style=discord.ButtonStyle.primary,
        custom_id="commander:network-status",
        row=0,
    )
    async def network_status(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.network_status(interaction)

    @discord.ui.button(
        label="Edge Info",
        style=discord.ButtonStyle.secondary,
        custom_id="commander:edge-info",
        row=0,
    )
    async def edge_info(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.edge_info(interaction)

    @discord.ui.button(
        label="Status NAS",
        style=discord.ButtonStyle.secondary,
        custom_id="commander:nas-status",
        row=0,
    )
    async def nas_status(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.nas_status(interaction)

    @discord.ui.button(
        label="Wake NAS",
        style=discord.ButtonStyle.success,
        custom_id="commander:nas-wake",
        row=1,
    )
    async def wake_nas(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.request_wake(interaction, self._authorizer)

    @discord.ui.button(
        label="Shutdown NAS",
        style=discord.ButtonStyle.danger,
        custom_id="commander:nas-shutdown",
        row=1,
    )
    async def shutdown_nas(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.request_shutdown(interaction, self._authorizer)

    @discord.ui.button(
        label="Help",
        style=discord.ButtonStyle.secondary,
        custom_id="commander:help",
        row=2,
    )
    async def help(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.help(interaction)

    @discord.ui.button(
        label="Ping",
        style=discord.ButtonStyle.secondary,
        custom_id="commander:ping",
        row=2,
    )
    async def ping(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._authorizer.require_operator(interaction):
            await self._operations.ping(interaction)
