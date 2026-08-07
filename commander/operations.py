from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING

import discord

from commander.config import Config
from commander.edge import EdgeController
from commander.nas import NasController
from commander.network import NetworkChecker
from commander.power_state import PowerOperationState
from commander.ratelimit import RateLimiter

if TYPE_CHECKING:
    from commander.authorization import InteractionAuthorizer


logger = logging.getLogger("commander.operations")
_EDGE_INLINE_LIMIT = 3_200
_EDGE_ERROR_LIMIT = 1_800
_POWER_OUTPUT_LIMIT = 3_000


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

    def __init__(
        self,
        config: Config,
        edge: EdgeController,
        network: NetworkChecker,
        nas: NasController,
        power_state: PowerOperationState,
        limiter: RateLimiter,
    ) -> None:
        self._config = config
        self._edge = edge
        self._network = network
        self._nas = nas
        self._power_state = power_state
        self._limiter = limiter

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

    async def request_wake(
        self, interaction: discord.Interaction, authorizer: InteractionAuthorizer
    ) -> None:
        await self._request_power_confirmation(interaction, authorizer, "wake")

    async def request_shutdown(
        self, interaction: discord.Interaction, authorizer: InteractionAuthorizer
    ) -> None:
        await self._request_power_confirmation(interaction, authorizer, "shutdown")

    async def _request_power_confirmation(
        self,
        interaction: discord.Interaction,
        authorizer: InteractionAuthorizer,
        operation: str,
    ) -> None:
        # Imported lazily to keep the view's operation type annotation from creating an import cycle.
        from commander.ui.power import PowerConfirmationView

        if operation == "wake":
            title = "🚀 Wake NAS"
            description = "Send a Wake-on-LAN packet to power on the NAS?"
            colour = discord.Colour.green()
        else:
            title = "🛑 Shutdown NAS"
            description = "Jalankan graceful shutdown pada NAS?"
            colour = discord.Colour.red()

        view = PowerConfirmationView(
            operation=operation,
            requester_id=interaction.user.id,
            authorizer=authorizer,
            operations=self,
        )
        embed = _timestamped_embed(title=title, description=description, colour=colour)
        embed.set_footer(text="Confirmation expires after 60 seconds")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

        try:
            view.message = await interaction.original_response()
        except discord.DiscordException:
            logger.warning("Could not retain power confirmation message for timeout cleanup")

    async def execute_power_action(
        self, interaction: discord.Interaction, operation: str
    ) -> None:
        """Run the confirmed action while holding the single NAS power-operation lock."""
        if not await self._power_state.acquire(operation):
            await interaction.response.send_message(
                "⚠️ Another NAS power operation is already in progress. Please wait for it to finish.",
                ephemeral=False,
            )
            return

        try:
            allowed, wait_seconds = await self._limiter.is_allowed(interaction.user.id, operation)
            if not allowed:
                await interaction.response.send_message(
                    f"⚠️ Rate limit reached. Try again in {wait_seconds} seconds.",
                    ephemeral=False,
                )
                return

            if interaction.message is not None:
                try:
                    await interaction.message.edit(
                        embed=self._power_progress_embed(operation, "Memulai operasi..."),
                        view=None,
                    )
                except discord.DiscordException:
                    logger.warning("Could not disable completed power confirmation buttons")

            await interaction.response.defer(thinking=True, ephemeral=False)
            if operation == "wake":
                await self._execute_wake(interaction)
            else:
                await self._execute_shutdown(interaction)
        except Exception:
            logger.exception("Unexpected NAS %s operation failure", operation)
            if interaction.response.is_done():
                embed = _timestamped_embed(
                    title="⚠️ NAS operation failed",
                    description="The NAS operation could not be completed.",
                    colour=discord.Colour.red(),
                )
                await interaction.edit_original_response(content=None, embed=embed)
            else:
                await interaction.response.send_message(
                    "⚠️ The NAS operation could not be completed.", ephemeral=False
                )
        finally:
            await self._power_state.release(operation)

    async def _execute_wake(self, interaction: discord.Interaction) -> None:
        result = await asyncio.to_thread(self._nas.wake)
        if not result.success:
            embed = _timestamped_embed(
                title="🔴 Wake NAS failed",
                description=_code_block(result.output[:_POWER_OUTPUT_LIMIT]),
                colour=discord.Colour.red(),
            )
            await interaction.edit_original_response(content=None, embed=embed)
            return

        await interaction.edit_original_response(
            content=None,
            embed=self._power_progress_embed("wake", "Wake-on-LAN packet sent. Waiting for NAS boot..."),
        )
        start = asyncio.get_running_loop().time()
        for attempt in range(1, 25):
            await asyncio.sleep(5)
            if await asyncio.to_thread(self._nas.is_online):
                elapsed = int(asyncio.get_running_loop().time() - start)
                embed = _timestamped_embed(
                    title="🟢 NAS online",
                    description=f"NAS responded after approximately {elapsed} seconds.",
                    colour=discord.Colour.green(),
                )
                embed.set_footer(text="Wake NAS • Commander control room")
                await interaction.edit_original_response(content=None, embed=embed)
                return

            await interaction.edit_original_response(
                content=None,
                embed=self._power_progress_embed(
                    "wake", f"Waiting for NAS boot... attempt {attempt}/24"
                ),
            )

        embed = _timestamped_embed(
            title="🔴 NAS did not come online",
            description="NAS tidak merespons dalam dua menit setelah Wake-on-LAN dikirim.",
            colour=discord.Colour.red(),
        )
        embed.set_footer(text="Wake NAS • Commander control room")
        await interaction.edit_original_response(content=None, embed=embed)

    async def _execute_shutdown(self, interaction: discord.Interaction) -> None:
        result = await asyncio.to_thread(self._nas.shutdown)
        colour = discord.Colour.green() if result.success else discord.Colour.red()
        title = "🟢 NAS shutdown command completed" if result.success else "🔴 NAS shutdown failed"

        if len(result.output) > _POWER_OUTPUT_LIMIT:
            attachment = discord.File(
                io.BytesIO(result.output.encode("utf-8")),
                filename="nas-shutdown.txt",
            )
            description = "Operation output is attached as `nas-shutdown.txt`."
            await interaction.edit_original_response(
                content=None,
                embed=_timestamped_embed(title=title, description=description, colour=colour),
                attachments=[attachment],
            )
            return

        embed = _timestamped_embed(
            title=title,
            description=_code_block(result.output),
            colour=colour,
        )
        embed.set_footer(text="Shutdown NAS • Commander control room")
        await interaction.edit_original_response(content=None, embed=embed)

    def _power_progress_embed(self, operation: str, description: str) -> discord.Embed:
        title = "🚀 Wake NAS" if operation == "wake" else "🛑 Shutdown NAS"
        embed = _timestamped_embed(
            title=title,
            description=description,
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="Power operation in progress • Commander control room")
        return embed

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
