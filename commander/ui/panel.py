from __future__ import annotations

import discord

from commander.authorization import InteractionAuthorizer
from commander.operations import OperatorOperations


def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎛️ Discord Commander",
        description="Pilih operasi yang tersedia. Semua hasil dikirim ke control room ini.",
        colour=discord.Colour.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Available now", value="Status Network • Edge Info", inline=False)
    embed.add_field(name="Next migration phase", value="NAS status • Wake NAS • Shutdown NAS", inline=False)
    embed.set_footer(text="Control room only")
    return embed


class CommanderPanel(discord.ui.View):
    """Persistent first-phase panel; active buttons share command authorization."""

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
        label="NAS Status (soon)",
        style=discord.ButtonStyle.secondary,
        custom_id="commander:nas-status-pending",
        disabled=True,
        row=1,
    )
    async def nas_status_pending(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        return

    @discord.ui.button(
        label="Wake NAS (soon)",
        style=discord.ButtonStyle.success,
        custom_id="commander:nas-wake-pending",
        disabled=True,
        row=1,
    )
    async def nas_wake_pending(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        return

    @discord.ui.button(
        label="Shutdown NAS (soon)",
        style=discord.ButtonStyle.danger,
        custom_id="commander:nas-shutdown-pending",
        disabled=True,
        row=1,
    )
    async def nas_shutdown_pending(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        return
