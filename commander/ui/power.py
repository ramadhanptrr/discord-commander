from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from commander.authorization import InteractionAuthorizer

if TYPE_CHECKING:
    from commander.operations import OperatorOperations


class PowerConfirmationView(discord.ui.View):
    """A short-lived, requester-bound confirmation for a destructive power action."""

    def __init__(
        self,
        *,
        operation: str,
        requester_id: int,
        authorizer: InteractionAuthorizer,
        operations: OperatorOperations,
    ) -> None:
        super().__init__(timeout=60)
        self._operation = operation
        self._requester_id = requester_id
        self._authorizer = authorizer
        self._operations = operations
        self.message: discord.InteractionMessage | None = None

        self.confirm.label = "Confirm Wake" if operation == "wake" else "Confirm Shutdown"
        self.confirm.style = (
            discord.ButtonStyle.success if operation == "wake" else discord.ButtonStyle.danger
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._requester_id:
            await interaction.response.send_message(
                "Only the operator who started this action can confirm it.",
                ephemeral=True,
            )
            return False
        return await self._authorizer.require_operator(interaction)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, row=0)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._operations.execute_power_action(interaction, self._operation)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        embed = discord.Embed(
            title="Power operation cancelled",
            description="No action was executed.",
            colour=discord.Colour.dark_grey(),
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                pass
