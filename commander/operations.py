from __future__ import annotations

import asyncio
import io
import logging

import discord

from commander.config import Config
from commander.edge import EdgeController
from commander.network import NetworkChecker


logger = logging.getLogger("commander.operations")
_EDGE_INLINE_LIMIT = 3_200
_EDGE_ERROR_LIMIT = 1_800


def _timestamped_embed(
    *, title: str, description: str, colour: discord.Colour
) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        colour=colour,
        timestamp=discord.utils.utcnow(),
    )


def _code_block(value: str) -> str:
    # A zero-width space prevents script output from closing the fence early.
    escaped_value = value.replace("```", "``\u200b`")
    return f"```text\n{escaped_value}\n```"


class OperatorOperations:
    """Shared public control-room operations for both commands and panel buttons."""

    def __init__(self, config: Config, edge: EdgeController, network: NetworkChecker) -> None:
        self._config = config
        self._edge = edge
        self._network = network

    async def network_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=False)
        try:
            reachable = await asyncio.to_thread(self._network.is_reachable)
        except Exception:
            logger.exception("Unexpected failure while checking home network")
            embed = _timestamped_embed(
                title="⚠️ Network status unavailable",
                description="The network probe could not be completed.",
                colour=discord.Colour.red(),
            )
        else:
            if reachable:
                embed = _timestamped_embed(
                    title="🟢 Home network online",
                    description=(
                        f"`{self._config.network.host}` answered the manual network probe."
                    ),
                    colour=discord.Colour.green(),
                )
            else:
                embed = _timestamped_embed(
                    title="🔴 Home network unreachable",
                    description=(
                        f"`{self._config.network.host}` did not answer "
                        f"{self._config.network.manual_probe_count} manual probe(s)."
                    ),
                    colour=discord.Colour.red(),
                )

        embed.set_footer(text="Manual check • Commander control room")
        await interaction.edit_original_response(content=None, embed=embed)

    async def edge_info(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=False)
        try:
            result = await asyncio.to_thread(self._edge.execute_info_script)
        except Exception:
            logger.exception("Unexpected failure while executing edge-info script")
            embed = _timestamped_embed(
                title="⚠️ Edge info unavailable",
                description="The edge-info command could not be completed.",
                colour=discord.Colour.red(),
            )
            embed.set_footer(text="Manual check • Commander control room")
            await interaction.edit_original_response(content=None, embed=embed)
            return

        if not result.success:
            output = result.output[:_EDGE_ERROR_LIMIT]
            if len(result.output) > _EDGE_ERROR_LIMIT:
                output += "\n[error output truncated]"
            embed = _timestamped_embed(
                title="🔴 Edge info failed",
                description=_code_block(output),
                colour=discord.Colour.red(),
            )
            embed.set_footer(text="Manual check • Commander control room")
            await interaction.edit_original_response(content=None, embed=embed)
            return

        if len(result.output) > _EDGE_INLINE_LIMIT:
            attachment = discord.File(
                io.BytesIO(result.output.encode("utf-8")),
                filename="edge-info.txt",
            )
            embed = _timestamped_embed(
                title="🟢 Edge info",
                description="Output is attached as `edge-info.txt`.",
                colour=discord.Colour.blurple(),
            )
            embed.set_footer(text="Manual check • Commander control room")
            await interaction.edit_original_response(
                content=None,
                embed=embed,
                attachments=[attachment],
            )
            return

        embed = _timestamped_embed(
            title="🟢 Edge info",
            description=_code_block(result.output),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="Manual check • Commander control room")
        await interaction.edit_original_response(content=None, embed=embed)
