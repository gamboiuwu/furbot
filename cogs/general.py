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

    @app_commands.command(name="help", description="Show everything FurBot can do.")
    async def help(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.config
        is_staff = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_roles
        )

        embed = discord.Embed(
            title="🐾 FurBot — Commands & Features",
            description="Here's everything I can do for the NYFurs server.",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="📋 Commands for everyone",
            value=(
                "**/help** — show this message\n"
                "**/ping** — check that I'm online and see my latency\n"
                "**/floofcount** — how many members have the Floofs role"
            ),
            inline=False,
        )

        if is_staff:
            embed.add_field(
                name="🛡️ Staff commands",
                value=(
                    "**/verify @member** — manually give someone the Floofs role\n"
                    "**/userinfo @member** — account age, join date & roles (vetting)\n"
                    "**/roles** — list every role with its ID (for setup)"
                ),
                inline=False,
            )
            embed.add_field(
                name="✅ Verification reactions (staff only)",
                value=(
                    "In the verification channel, react to a member's message:\n"
                    f"{cfg.approval_emoji} **Approve** — grant the Floofs role + welcome DM\n"
                    f"{cfg.reject_emoji} **Reject** — DM them and temp-ban for "
                    f"{cfg.reject_cooldown_hours}h (auto-unban after)\n"
                    f"{cfg.warn_emoji} **Warn** — DM them to redo their verification"
                ),
                inline=False,
            )

        embed.set_footer(text="Only you can see this message.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="userinfo",
        description="Show a member's account details (handy for vetting before verifying).",
    )
    @app_commands.describe(member="The member to look up")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member) -> None:
        created_ts = int(member.created_at.timestamp())
        account_age_days = (discord.utils.utcnow() - member.created_at).days

        embed = discord.Embed(
            title=f"👤 {member}",
            color=member.color if member.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User ID", value=f"`{member.id}`", inline=False)
        embed.add_field(
            name="Account created",
            value=f"<t:{created_ts}:D> (<t:{created_ts}:R>)\n**{account_age_days} days** old",
            inline=False,
        )
        if member.joined_at:
            joined_ts = int(member.joined_at.timestamp())
            embed.add_field(name="Joined server", value=f"<t:{joined_ts}:D> (<t:{joined_ts}:R>)", inline=False)

        # Young accounts are the classic raid/alt signal — flag them.
        if account_age_days < 7:
            embed.add_field(
                name="⚠️ Heads up",
                value="This account is **less than a week old** — double-check before verifying.",
                inline=False,
            )

        floofs_id = getattr(self.bot.config, "floofs_role_id", None)
        verified = bool(floofs_id) and any(r.id == floofs_id for r in member.roles)
        embed.add_field(name="Verified?", value="✅ Yes" if verified else "❌ Not yet", inline=False)

        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=", ".join(roles) if roles else "None",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @userinfo.error
    async def userinfo_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        msg = (
            "You need the **Manage Roles** permission to use this."
            if isinstance(error, app_commands.MissingPermissions)
            else "Something went wrong looking that up."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

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
            await interaction.response.send_message(
                f"Couldn't find a role with ID `{role_id}` in this server. "
                "Double-check the `FLOOFS_ROLE_ID` variable — run `/roles` to see the real IDs.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"🐾 There are **{len(role.members)}** floofs in the server!", ephemeral=True
        )


    @app_commands.command(name="roles", description="List every role and its ID (handy for configuring the bot).")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def roles(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        # Highest roles first; skip @everyone. Show name and copy-pasteable ID.
        lines = [
            f"`{role.id}` — {role.name}"
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True)
            if not role.is_default()
        ]
        if not lines:
            await interaction.response.send_message("This server has no roles.", ephemeral=True)
            return
        # Stay under Discord's 2000-char message limit.
        text = "**Roles in this server (newest at top):**\n" + "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n… (list truncated)"
        await interaction.response.send_message(text, ephemeral=True)

    @roles.error
    async def roles_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        msg = (
            "You need the **Manage Roles** permission to use this."
            if isinstance(error, app_commands.MissingPermissions)
            else "Something went wrong listing roles."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
