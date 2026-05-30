"""One-month member check-in.

About a month after joining, each verified member is DMed once with a friendly,
optional feedback form (two buttons: share with name / anonymously). Submissions
go to a channel in the separate staff server. The bot never messages them again.

Avoids mass-DM: on first activation, everyone already past the threshold is
marked as handled, so only members crossing one month from then on are messaged.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff

log = logging.getLogger("furbot.review")

REVIEW_SENT = "review_sent"        # {user_id_str: epoch_sent}
REVIEW_STATE = "review_state"      # {"seeded": bool}
REVIEW_RECHECK = "review_recheck"  # {user_id_str: due_epoch} (opted-in follow-ups)
REVIEW_OPTOUT = "review_optout"    # [user_id_str, ...] (never contact again)

QUESTIONS = [
    "How has your experience been so far?",
    "Anything we've done well?",
    "Anything we could do better?",
    "Anything you'd like added or changed?",
]


def anon_handle(user_id: int) -> str:
    """Stable, non-reversible anonymous handle for a user."""
    digest = hashlib.sha256(f"nyfurs-review:{user_id}".encode()).hexdigest()
    return f"AnonymousFur#{int(digest[:6], 16) % 10000:04d}"


class ReviewModal(discord.ui.Modal):
    def __init__(self, cog: "Review", guild_id: int, user_id: int, anonymous: bool) -> None:
        super().__init__(title="NYFurs One Month Check-In", timeout=900)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.anonymous = anonymous
        self.inputs: list[discord.ui.TextInput] = []
        for q in QUESTIONS:
            item = discord.ui.TextInput(label=q, style=discord.TextStyle.paragraph, required=False, max_length=1000)
            self.inputs.append(item)
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answers = [i.value for i in self.inputs]
        await self.cog.handle_submission(interaction, self.guild_id, self.user_id, self.anonymous, answers)


class ReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"review:v1:(?P<mode>named|anon):(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    _SPEC = {"named": "Share with my name", "anon": "Share anonymously"}

    def __init__(self, mode: str, guild_id: int, user_id: int) -> None:
        self.mode = mode
        self.guild_id = guild_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label=self._SPEC[mode],
                style=discord.ButtonStyle.primary if mode == "named" else discord.ButtonStyle.secondary,
                custom_id=f"review:v1:{mode}:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["mode"], int(match["guild_id"]), int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form isn't for you.", ephemeral=True)
            return
        cog: "Review | None" = interaction.client.get_cog("Review")
        if cog is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ReviewModal(cog, self.guild_id, self.user_id, anonymous=self.mode == "anon")
        )


class ReviewConsentButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"review:consent:v1:(?P<choice>yes|no):(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    _SPEC = {"yes": ("Yes, that's fine", discord.ButtonStyle.success),
             "no": ("No thanks", discord.ButtonStyle.secondary)}

    def __init__(self, choice: str, guild_id: int, user_id: int) -> None:
        self.choice = choice
        self.guild_id = guild_id
        self.user_id = user_id
        label, style = self._SPEC[choice]
        super().__init__(
            discord.ui.Button(
                label=label, style=style,
                custom_id=f"review:consent:v1:{choice}:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["choice"], int(match["guild_id"]), int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return
        cog: "Review | None" = interaction.client.get_cog("Review")
        if cog is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await cog.handle_consent(interaction, self.choice, self.user_id)


class ReviewOptOutButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"review:optout:v1:(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    def __init__(self, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label="Don't contact me again",
                style=discord.ButtonStyle.danger,
                custom_id=f"review:optout:v1:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild_id"]), int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return
        cog: "Review | None" = interaction.client.get_cog("Review")
        if cog is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await cog.handle_optout(interaction, self.user_id)


def build_consent_view(guild_id: int, user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewConsentButton("yes", guild_id, user_id))
    view.add_item(ReviewConsentButton("no", guild_id, user_id))
    return view


class Review(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.review_loop.start()

    async def cog_unload(self) -> None:
        self.review_loop.cancel()

    def _s(self, key: str):
        return self.settings.get(key)

    def _guild(self) -> discord.Guild | None:
        gid = self.config.guild_id
        if gid:
            return self.bot.get_guild(gid)
        return self.bot.guilds[0] if self.bot.guilds else None

    def _eligible(self, guild: discord.Guild) -> list[discord.Member]:
        rid = self.config.floofs_role_id
        role = guild.get_role(rid) if rid else None
        if role is None:
            return []
        days = self._s("review_after_days")
        now = discord.utils.utcnow()
        sent = self.store.get(REVIEW_SENT, {})
        optout = self.store.get(REVIEW_OPTOUT, [])
        out = []
        for m in guild.members:
            if m.bot or role not in m.roles or m.joined_at is None:
                continue
            if str(m.id) in sent or str(m.id) in optout:
                continue
            if (now - m.joined_at).days >= days:
                out.append(m)
        return out

    # ---- submission handling --------------------------------------------

    async def handle_submission(
        self, interaction: discord.Interaction, guild_id: int, user_id: int,
        anonymous: bool, answers: list[str],
    ) -> None:
        if anonymous:
            who = anon_handle(user_id)
        else:
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            who = f"{member} (`{user_id}`)" if member else f"User `{user_id}`"

        embed = discord.Embed(title="One Month Feedback", color=discord.Color.teal())
        embed.add_field(name="From", value=who, inline=False)
        for q, a in zip(QUESTIONS, answers):
            if a and a.strip():
                embed.add_field(name=q, value=a.strip()[:1024], inline=False)
        if len(embed.fields) == 1:
            embed.add_field(name="(no written answers)", value="The member submitted the form without text.", inline=False)

        channel = self.bot.get_channel(self._s("feedback_channel_id"))
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                log.exception("Failed to post review feedback")
        else:
            log.warning("Feedback channel %s not found — is the bot in the staff server?", self._s("feedback_channel_id"))

        months = max(1, round(self._s("review_recheck_days") / 30))
        await interaction.response.send_message(
            "Thank you. Your feedback has been received.\n\n"
            f"One last thing: would you be open to a short check-in like this again in about {months} months?",
            view=build_consent_view(guild_id, user_id),
            ephemeral=True,
        )

    async def handle_consent(self, interaction: discord.Interaction, choice: str, user_id: int) -> None:
        days = self._s("review_recheck_days")
        if choice == "yes":
            due = int(time.time()) + days * 86400
            await self.store.update(lambda d: d.setdefault(REVIEW_RECHECK, {}).__setitem__(str(user_id), due))
            months = max(1, round(days / 30))
            msg = (
                f"Thank you. We'll check in again in about {months} months. "
                "If you change your mind, you can simply ignore that message."
            )
        else:
            await self.store.update(lambda d: d.get(REVIEW_RECHECK, {}).pop(str(user_id), None))
            msg = "Understood. We won't reach out again. Thanks for your time."
        await interaction.response.edit_message(content=msg, view=None)

    async def handle_optout(self, interaction: discord.Interaction, user_id: int) -> None:
        def mut(d: dict) -> None:
            lst = d.setdefault(REVIEW_OPTOUT, [])
            if str(user_id) not in lst:
                lst.append(str(user_id))
            d.get(REVIEW_RECHECK, {}).pop(str(user_id), None)

        await self.store.update(mut)
        await interaction.response.edit_message(
            content="Understood. You won't be contacted again. Thank you for your time.", view=None
        )

    # ---- DM sending ------------------------------------------------------

    async def _send_review(self, guild: discord.Guild, member: discord.Member) -> None:
        text = (
            "**NYFurs — One Month Check-In**\n"
            f"Hi {member.display_name},\n"
            "You've been part of NYFurs for about a month now, and we'd genuinely love to hear how "
            "it's been going for you. Your feedback helps us make the community better for everyone. ^_^\n"
            "It's completely optional, but if you have a minute, use a button below to open a short form. "
            "You're welcome to keep it anonymous if you'd prefer.\n"
            "Just so you know: we'll only reach out again if you choose to opt in to a future check-in at "
            "the end of the form. Otherwise, this is the only message you'll receive from this bot.\n"
            "Thanks for being part of NYFurs.\n"
            "— The NYFurs Staff Team"
        )
        view = discord.ui.View(timeout=None)
        view.add_item(ReviewButton("named", guild.id, member.id))
        view.add_item(ReviewButton("anon", guild.id, member.id))
        view.add_item(ReviewOptOutButton(guild.id, member.id))
        try:
            await member.send(text, view=view)
        except discord.HTTPException:
            pass  # closed DMs — still marked sent so we don't retry forever

    # ---- loop ------------------------------------------------------------

    @tasks.loop(minutes=30)
    async def review_loop(self) -> None:
        interval = max(1, self._s("review_interval_minutes"))
        if self.review_loop.minutes != interval:
            self.review_loop.change_interval(minutes=interval)
        if not self._s("review_enabled"):
            return
        guild = self._guild()
        if guild is None:
            return
        if not guild.chunked:
            try:
                await guild.chunk()
            except (discord.HTTPException, discord.ClientException):
                return

        eligible = self._eligible(guild)
        state = self.store.get(REVIEW_STATE, {})

        # First activation: seed everyone already past the threshold so we don't
        # blast the existing membership — only message people going forward.
        if not state.get("seeded"):
            now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

            def seed(d: dict) -> None:
                sent = d.setdefault(REVIEW_SENT, {})
                for m in eligible:
                    sent[str(m.id)] = now
                d.setdefault(REVIEW_STATE, {})["seeded"] = True

            await self.store.update(seed)
            log.info("Review: seeded %d existing member(s) as already-handled.", len(eligible))
            return

        batch = max(1, self._s("review_batch"))
        delay = 60.0 / batch  # spread across the cycle
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

        # Opted-in follow-ups that have come due — send the same form again.
        recheck = self.store.get(REVIEW_RECHECK, {})
        optout = self.store.get(REVIEW_OPTOUT, [])
        due = [uid for uid, t in recheck.items() if t <= time.time() and uid not in optout][:batch]
        for uid in due:
            member = guild.get_member(int(uid))
            if member is not None:
                await self._send_review(guild, member)
            await self.store.update(lambda d, k=uid: d.get(REVIEW_RECHECK, {}).pop(k, None))
            await asyncio.sleep(delay)

        # First-time check-ins for members who just crossed the threshold.
        sent_count = 0
        for member in eligible[:batch]:
            await self._send_review(guild, member)
            await self.store.update(lambda d, mid=str(member.id): d.setdefault(REVIEW_SENT, {}).__setitem__(mid, now))
            sent_count += 1
            await asyncio.sleep(delay)
        if sent_count or due:
            log.info("Review: %d first-time, %d follow-up DM(s) this cycle.", sent_count, len(due))

    @review_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ---- manual test -----------------------------------------------------

    @app_commands.command(
        name="review_test",
        description="(Staff) Send yourself or a member the one-month check-in now, for testing.",
    )
    @app_commands.describe(member="Who to send the test check-in to (defaults to you)")
    @is_staff()
    async def review_test(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        guild = interaction.guild or self._guild()
        target = member or interaction.user
        if guild is None or not isinstance(target, discord.Member):
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        await self._send_review(guild, target)  # does not mark them as sent — repeatable
        note = "" if self.bot.get_channel(self._s("feedback_channel_id")) else (
            "\nNote: I can't see the feedback channel yet — invite me to the staff server and check "
            "`feedback_channel_id`, or submissions won't post."
        )
        await interaction.response.send_message(
            f"Sent the check-in DM to {target.mention}. If it didn't arrive, their DMs are closed.{note}",
            ephemeral=True,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Review command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Review(bot))
