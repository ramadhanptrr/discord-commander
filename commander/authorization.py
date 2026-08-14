from __future__ import annotations

import logging

import discord

from shared.config import DiscordConfig


logger = logging.getLogger("commander.authorization")


class InteractionAuthorizer:
    """Apply one uniform operator policy to slash commands and component clicks."""

    def __init__(self, config: DiscordConfig) -> None:
        self._config = config

    def _rejection_reason(self, interaction: discord.Interaction) -> str | None:
        if interaction.guild_id is None:
            return "direct-message"
        if interaction.guild_id not in self._config.guild_ids:
            return "guild-not-allowed"
        if interaction.channel_id != self._config.control_room_channel_id:
            return "channel-not-control-room"
        if interaction.user.id not in self._config.allowed_user_ids:
            return "user-not-allowed"
        return None

    async def require_operator(self, interaction: discord.Interaction) -> bool:
        reason = self._rejection_reason(interaction)
        if reason is None:
            return True

        logger.warning(
            "Rejected Discord interaction: reason=%s guild=%s channel=%s user=%s",
            reason,
            interaction.guild_id,
            interaction.channel_id,
            interaction.user.id,
        )
        message = "This command is available only to authorized operators in the Commander control room."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False
