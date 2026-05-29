"""General-purpose / utility commands.

A small starter set of commands. This is the easiest place to add new
"other stuff" as the bot grows.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check that the bot is alive and see its latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: {latency_ms}ms", ephemeral=True)

    @app_commands.command(name="floofcount", description="See how many members currently have the Floofs role.")
    async def floofcount(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        role_id = getattr(self.bot.config, "floofs_role_id", None)
        if guild is None or not role_id:
            await interaction.response.send_message(
                "The Floofs role isn't configured yet.", ephemeral=True
            )
            return
        role = guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message("Couldn't find the Floofs role.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🐾 There are **{len(role.members)}** floofs in the server!", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
