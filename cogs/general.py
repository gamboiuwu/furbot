"""General-purpose / utility commands.

A small starter set of commands. This is the easiest place to add new
"other stuff" as the bot grows. All commands here are staff-only.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from checks import NotStaff, is_staff
from verification_actions import VERIFICATIONS, build_userinfo_embed

log = logging.getLogger("furbot.general")


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Command error in General cog", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    def _viewer_is_staff(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        sid = getattr(self.bot.config, "staff_role_id", None)
        if sid and any(r.id == sid for r in member.roles):
            return True
        return bool(member.guild_permissions.manage_roles)

    @app_commands.command(name="help", description="Show everything FurBot can do.")
    async def help(self, interaction: discord.Interaction) -> None:
        # Open to everyone. Staff-only sections appear only for staff.
        is_staff_viewer = self._viewer_is_staff(interaction)
        cfg = self.bot.config

        embed = discord.Embed(
            title="🐾 FurBot — Commands",
            description=("Here's what I can do! Commands you can use are below."
                         + (" Staff-only tools are at the bottom." if is_staff_viewer else "")),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="🛏️ Roommate & carpool finder (18+)",
            value=(
                "**/roommate find** — list a hotel room or carpool (hosting or looking to join)\n"
                "**/roommate edit** — change one of your listings\n"
                "**/roommate browse** — see open listings (names hidden)\n"
                "**/roommate status** — your listings & any pending matches\n"
                "**/roommate cancel** — take a listing down\n"
                "**/roommate rules** — the room/ride rules & safety"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎉 Events & fun",
            value=(
                "**/events** — upcoming NYFurs events\n"
                "**/birthday set·view·clear** — birthday shoutout + the Birthday role\n"
                "**/welcomepoints** — your points for welcoming new members\n"
                "**/echo [message]** — have me repeat something (no pings)\n"
                "**/help** — show this message"
            ),
            inline=False,
        )
        embed.add_field(
            name="✅ Getting verified",
            value=(
                "Post your intro in the verification channel and a mod will react to approve you, "
                "which grants the **Floofs** role and a welcome. Mods may ask you to add detail first."
            ),
            inline=False,
        )

        if is_staff_viewer:
            embed.add_field(
                name="🛡️ Staff · members & verification",
                value=(
                    "**/verify @member** — grant the Floofs role manually\n"
                    "**/userinfo @member** — account age, join date & roles (vetting)\n"
                    "**/floofcount** — how many have the Floofs role\n"
                    "**/stats** — verification totals · **/roles** — list role IDs\n"
                    "**/leaderboard** — monthly staff verification leaderboard · **/ping**"
                ),
                inline=False,
            )
            embed.add_field(
                name="⚙️ Staff · config, events & tasks",
                value=(
                    "**/config view·set·reset·save** — bot settings (saved to Nextcloud)\n"
                    "**/syncevents** · **/eventsdebug [id]** — Indico event sync & diagnostics\n"
                    "**/task claim·assign·owner·board** — staff task board\n"
                    "**/argument** — kick off the weekly bit\n"
                    "Onboarding sweeps run automatically; tune them with `/config set onboarding_*`."
                ),
                inline=False,
            )
            embed.add_field(
                name="🛏️ Staff · roommate finder admin",
                value=(
                    "**/roommate setup** — post/refresh the finder hub message\n"
                    "**/roommate importcons [channel]** — add cons from a forum's post titles\n"
                    "**/roommate stats** — listings, offers & matches at a glance"
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
    @is_staff()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member) -> None:
        record = self.bot.store.get(VERIFICATIONS, {}).get(str(member.id))
        embed = build_userinfo_embed(
            member, floofs_role_id=getattr(self.bot.config, "floofs_role_id", None), verified_by=record
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stats", description="Show verification totals (verified / rejected / warned).")
    @is_staff()
    async def stats(self, interaction: discord.Interaction) -> None:
        data = self.bot.store.get("stats", {})
        embed = discord.Embed(title="📊 Verification stats", color=discord.Color.blurple())
        embed.add_field(name="✅ Verified", value=str(data.get("verified", 0)))
        embed.add_field(name="⛔ Rejected", value=str(data.get("rejected", 0)))
        embed.add_field(name="⚠️ Warned", value=str(data.get("warned", 0)))
        embed.add_field(name="📨 Reminders sent", value=str(data.get("reminded", 0)))
        embed.add_field(name="👢 Removed (unverified)", value=str(data.get("onboard_kicked", 0)))
        storage = "Nextcloud" if self.bot.config.webdav_enabled else "local files"
        embed.set_footer(text=f"Stored on: {storage}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ping", description="Check that the bot is alive and see its latency.")
    @is_staff()
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: {latency_ms}ms", ephemeral=True)

    @app_commands.command(name="echo", description="Have the bot repeat your message.")
    @app_commands.describe(message="What should I say?")
    async def echo(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 2000]) -> None:
        # Public command (anyone). Send with ALL mentions disabled so it can't
        # be used to ping @everyone/@here, roles, or mass-mention users.
        await interaction.response.send_message(
            message, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="floofcount", description="See how many members currently have the Floofs role.")
    @is_staff()
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
    @is_staff()
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
