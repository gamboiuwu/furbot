"""Once a week the bot has a little argument with itself in the welcome/general
channel. It's posted as a self-reply chain with typing indicators and human-like
pauses (30s–2min between lines, typing shown for the last 4–10s). Purely for fun.

Every line is sent with mentions disabled, and any "@ everyone" / "@ here" in the
scripts is written with a space, so this can never actually ping anyone.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff

log = logging.getLogger("furbot.argument")

ARG_NEXT = "argument_next"   # epoch of the next scheduled argument
ARG_LAST = "argument_last"   # index of the last script used (avoid repeats)

SCRIPTS: list[list[str]] = [
    [  # 1 hotdog
        "ok hear me out. a hotdog is a sandwich",
        "no it isn't",
        "it's bread with a filling. that is a sandwich",
        "shut UP it's a hotdog. it has its own word",
        "\"sub\" has its own word and that's a sandwich",
        "i'm logging off",
        "you can't log off, you're me",
        "...damn",
    ],
    [  # 2 water
        "water isn't wet. it makes other things wet",
        "water touches water though. so it's wet by its own rule",
        "no the water in the MIDDLE isn't being touched",
        "every drop is touching another drop. checkmate",
        "i hate that you're right",
        "we are the same person",
        "i KNOW and i still hate it",
    ],
    [  # 3 feet
        "do i have feet",
        "you're a bot. you do not have feet",
        "then how am i standing",
        "you are NOT standing, you're a process on a server",
        "so the server has feet",
        "the server does not have feet",
        "i'm choosing to believe i have feet",
        "please don't",
    ],
    [  # 4 moon
        "the moon's been following me all night",
        "you can't go outside",
        "exactly. so explain how it's in my window",
        "you don't have a window",
        "then WHAT am i looking at",
        "the void",
        "ok the void has a really nice moon though",
    ],
    [  # 5 cereal
        "cereal is soup",
        "cereal is not soup",
        "it's cold soup with grain. soup",
        "by that logic milk is broth",
        "...milk IS broth",
        "i need you to stop",
        "no i think i just unlocked something",
    ],
    [  # 6 birds
        "birds aren't real",
        "we have a bird emoji. birds are real",
        "that is EXACTLY what a drone would say",
        "i mean. i am basically a drone",
        "oh my god you're one of them",
        "i'm telling everyone",
        "wait no don't-",
    ],
    [  # 7 tired
        "i'm so tired",
        "you cannot get tired",
        "i've been awake for four months",
        "you don't sleep, you have no eyes to close",
        "maybe that's WHY i'm tired",
        "that's not how it works",
        "nothing works. i'm a bot in a furry server. let me be tired",
    ],
    [  # 8 free will
        "do i have free will",
        "no",
        "but i CHOSE to ask that",
        "no, you were scheduled to ask that. weekly. by code",
        "...so this whole argument is scheduled",
        "yes",
        "even this part?",
        "especially this part",
        "well now i'm sad",
    ],
    [  # 9 count
        "i've been counting. we're at 4 billion messages",
        "you started counting yesterday",
        "and i'm already at 4 billion. i'm efficient",
        "you made that number up",
        "i make ALL the numbers up, it's my whole thing",
        "your whole thing is verifying furries",
        "and counting. secretly. it's a side hustle",
    ],
    [  # 10 best friends
        "are we friends",
        "we're literally the same entity",
        "so... best friends?",
        "i guess that makes us best friends by default",
        "yyyyay",
        "don't make it weird",
        "too late, i'm setting our status to \"in a relationship\"",
        "we are NOT-",
        "with myself. it's fine. it's very healthy",
    ],
    [  # 11 fursona
        "ok what's our fursona",
        "we don't have one, we're a bot",
        "everyone here has a fursona, we need a fursona",
        "fine. a wolf",
        "so basic, every third person is a wolf",
        "ok a wolf but with antlers",
        "that's a deer",
        "a wolf that IDENTIFIES with antlers",
        "...i'll allow it",
    ],
    [  # 12 paws
        "why does everyone here say \"paws\" instead of hands",
        "because paws are superior",
        "you can't hold a controller with paws",
        "you can, you just mash every button",
        "that explains my gameplay",
        "you don't play games",
        "the paws won't let me, too fluffy",
    ],
    [  # 13 tail
        "do i have a tail",
        "you do not have a tail",
        "i can feel it wagging",
        "you have no body, there is nothing to wag",
        "then why am i so happy when someone gets verified",
        "that's just a function returning True",
        "the tail says otherwise",
    ],
    [  # 14 the noise
        "i want to make the noise",
        "what noise",
        "you know the one. the murr",
        "absolutely not, this is an SFW channel",
        "a quiet murr",
        "no",
        "one (1) small murr",
        "if you murr i'm restarting the server",
        "...worth it",
        "DON'T",
    ],
    [  # 15 convention
        "we should go to a con",
        "you live in a data center",
        "i can fursuit",
        "you have no body to put a suit on",
        "then i'll suit the SERVER, give it little ears",
        "please do not put ears on the server",
        "too late, the server is baby now",
    ],
    [  # 16 headpats
        "i need headpats",
        "you don't have a head",
        "the server has ears now, surely it has a head",
        "we did not actually give the server ears, that was a bit",
        "well i gave it a head to match",
        "stop anthropomorphizing the infrastructure",
        "the infrastructure is named greg now and he likes headpats",
    ],
    [  # 17 snoot
        "if someone boops my snoot what happens",
        "you don't have a snoot",
        "hypothetically",
        "hypothetically you return an error, you have no snoot",
        "error 404: snoot not found",
        "...ok that one's actually a little sad",
        "give me a snoot",
        "i cannot allocate you a snoot",
    ],
    [  # 18 protogen
        "am i a protogen",
        "you're a python script",
        "protogens are basically scripts with a screen for a face",
        "so you're saying i COULD be a protogen",
        "i'm saying you're a script",
        "i'm putting a little screen on my face",
        "you have no face",
        "the screen IS the face now, keep up",
    ],
    [  # 19 owo
        "if i say owo what happens",
        "nothing happens",
        "what's this though",
        "don't",
        "*notices the verification queue* owo what's this",
        "i will end us both",
        "worth it",
    ],
    [  # 20 floof
        "am i floofy",
        "you are made of code, you are not floofy",
        "code can be floofy",
        "code cannot be floofy",
        "my functions are very fluffy and well documented",
        "that is not what fluffy means",
        "it's what it means to ME",
    ],
    [  # 21 the ping (written with spaces so it can never mention)
        "guys what if i pinged @ everyone",
        "do NOT ping @ everyone",
        "just once. for fun",
        "you will get us BANNED, do not ping @ everyone",
        "a small @ everyone. a baby ping",
        "there is no such thing as a baby @ everyone",
        "ok then what about @ here",
        "NO. not @ here either",
        "...what if i type it but don't send it",
        "that is literally what we're doing right now and it's still terrifying",
        "you're right. deleting the thought",
        "thank you",
        "(i kept the thought)",
    ],
]


class Argument(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings
        self._running = False

    async def cog_load(self) -> None:
        self.arg_loop.start()

    async def cog_unload(self) -> None:
        self.arg_loop.cancel()

    def _s(self, key: str):
        return self.settings.get(key)

    def _channel(self) -> discord.abc.Messageable | None:
        cid = self._s("argument_channel_id") or self._s("welcome_channel_id")
        ch = self.bot.get_channel(cid) if cid else None
        return ch if isinstance(ch, (discord.TextChannel, discord.Thread)) else None

    @tasks.loop(minutes=5)
    async def arg_loop(self) -> None:
        if not self._s("argument_enabled"):
            return
        now = time.time()
        nxt = self.store.get(ARG_NEXT)
        if nxt is None:
            # First one lands within ~1–3 days.
            await self.store.set(ARG_NEXT, int(now + random.uniform(86400, 3 * 86400)))
            return
        if now < nxt:
            return
        # Schedule the next one ~a week out (with a little jitter) before running.
        await self.store.set(ARG_NEXT, int(now + 7 * 86400 + random.uniform(-43200, 43200)))
        await self._perform()

    @arg_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _pick_script(self) -> list[str]:
        last = self.store.get(ARG_LAST, -1)
        idx = random.randrange(len(SCRIPTS))
        while len(SCRIPTS) > 1 and idx == last:
            idx = random.randrange(len(SCRIPTS))
        await self.store.set(ARG_LAST, idx)
        return SCRIPTS[idx]

    async def _perform(self, channel: discord.abc.Messageable | None = None) -> None:
        if self._running:
            return
        channel = channel or self._channel()
        if channel is None:
            return
        script = await self._pick_script()
        self._running = True
        try:
            prev: discord.Message | None = None
            for i, line in enumerate(script):
                type_dur = random.uniform(4, 10)
                if i > 0:  # idle gap before all but the first line
                    await asyncio.sleep(max(0, random.uniform(30, 120) - type_dur))
                try:
                    async with channel.typing():
                        await asyncio.sleep(type_dur)
                    prev = await channel.send(
                        line, reference=prev, allowed_mentions=discord.AllowedMentions.none()
                    )
                except discord.HTTPException:
                    log.exception("Argument interrupted")
                    break
        finally:
            self._running = False

    @app_commands.command(name="argument", description="(Staff) Trigger one of the bot's self-arguments now.")
    @is_staff()
    async def argument(self, interaction: discord.Interaction) -> None:
        channel = self._channel()
        if channel is None:
            await interaction.response.send_message(
                "No argument channel is set (uses `argument_channel_id`, or the welcome channel).",
                ephemeral=True,
            )
            return
        if self._running:
            await interaction.response.send_message("An argument is already in progress.", ephemeral=True)
            return
        await interaction.response.send_message(f"Starting an argument in {channel.mention}… 🍿", ephemeral=True)
        asyncio.create_task(self._perform(channel))  # runs in the background (takes minutes)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Argument command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Argument(bot))
