import datetime
import discord
from discord.ext import commands
import math
import asyncio

def round_datetime(date: datetime.datetime) -> datetime.datetime:
    discard = datetime.timedelta(microseconds=date.microsecond, seconds=date.second)
    result = date - discard
    if discard >= datetime.timedelta(seconds=30):
        result += datetime.timedelta(seconds=60)
    return result

g_page_reactions = {
    "◀️": -1,
    "▶️": 1,
}
g_page_react_order = ["◀️", "▶️"]

class Paginator:
    def __init__(self, bot: commands.Bot, data: list[str], per_page: int = 10, start_page: int = 1):
        self.bot = bot
        self.data = data
        self.per_page = per_page
        self.max_pages = math.ceil(len(self.data) / self.per_page)
        self.current_page = min(start_page, self.max_pages)

    def format_chunk(self, current_page: int, max_pages: int, chunk: list[str]):
        return {
            "embed": discord.Embed(
                title=f"Page {current_page}/{max_pages}",
                description="\n".join(chunk),
                color=0x0099FF,
            )
        }

    async def send(self, ctx: commands.Context):
        message = await ctx.send(**self.format_chunk(self.current_page, self.max_pages, self.calculate_chunk()))
        await self._add_reactions(message)
        active = True

        def check(reaction: discord.Reaction, user):
            return user == ctx.author and str(reaction.emoji) in g_page_reactions and reaction.message.id == message.id
                        # or you can use unicodes, respectively: "\u25c0" or "\u25b6"

        while active:
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=60, check=check)
                page_move_amount = g_page_reactions[str(reaction.emoji)]
                new_page = self.current_page + page_move_amount
                if new_page > 0 and new_page <= self.max_pages:
                    self.current_page = new_page
                    await message.edit(**self.format_chunk(self.current_page, self.max_pages, self.calculate_chunk()))
                await message.remove_reaction(reaction.emoji, user)
            except asyncio.TimeoutError:
                await message.clear_reactions()
                active = False

    def calculate_chunk(self):
        return self.data[(self.current_page - 1) * self.per_page : self.current_page * self.per_page]

    async def _add_reactions(self, message: discord.Message):
        for reaction in g_page_react_order:
            await message.add_reaction(reaction)
