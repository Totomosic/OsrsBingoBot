import asyncio
import argparse
import dataclasses
import discord
from discord.ext import commands
import math

import tasks
import templates

@dataclasses.dataclass
class BotConfig:
    bot_token: str
    tasks_filename: str

@dataclasses.dataclass
class BotContext:
    tasks: tasks.TaskDatabase

def read_discord_token(config: BotConfig) -> str:
    return config.bot_token

def get_config_from_args() -> BotConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", type=str, required=True, help="Discord bot token")
    parser.add_argument("--tasks_filename", type=str, default="tasks/tasks.json", help="Path to tasks json file")
    args = parser.parse_args()

    return BotConfig(
        bot_token=args.token,
        tasks_filename=args.tasks_filename,
    )

# Initialize bot

config = get_config_from_args()
bot_token = read_discord_token(config)
description = """Discord osrs bot"""
bot = commands.Bot(
    intents=discord.Intents.all(),
    command_prefix="!",
    description=description,
    case_insensitive=True,
)

g_context = BotContext(
    tasks=tasks.TaskDatabase(),
)
g_context.tasks.load_task_file(config.tasks_filename)

# Setup bot commands

g_page_reactions = {
    "◀️": -1,
    "▶️": 1,
}
g_page_react_order = ["◀️", "▶️"]

class Paginator:
    def __init__(self, data: list[str], per_page: int = 10, start_page: int = 1):
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
                reaction, user = await bot.wait_for("reaction_add", timeout=60, check=check)
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

@bot.command()
async def list(ctx: commands.Context, page: int = 1):
    tasks = g_context.tasks.get_tasks()
    formatted_tasks = [f"**{task.id}** {task.task.get_template()}" for task in tasks]
    paginator = Paginator(formatted_tasks, per_page=25, start_page=page)
    await paginator.send(ctx)

@bot.command()
async def task(ctx: commands.Context, task_id: int = None):
    if task_id is None:
        task = g_context.tasks.get_random_task()
    else:
        task = g_context.tasks.get_task_by_id(task_id)
        if task is None:
            raise Exception(f"No task with ID {task_id}")
    embed = discord.Embed(
        title=f"Task {task.id}",
        color=0x0099FF,
        description=task.task.evaluate(),
    )
    # embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar.url)
    await ctx.send(embed=embed)

@bot.command()
async def edit(ctx: commands.Context, task_id: int, template: str):
    template_obj = templates.ParsedTemplate(template)
    existing_task = g_context.tasks.get_task_by_id(task_id)
    if not existing_task:
        raise Exception(f"No task with ID {task_id}")
    existing_task.task = template_obj
    g_context.tasks.save(config.tasks_filename)
    await ctx.send(f"Successfully updated task **{task_id}**: {existing_task.task.get_template()}")

def handle_errors(*cmds):
    for cmd in cmds:
        @cmd.error
        async def error_handler(ctx: commands.Context, error):
            await ctx.send(f"Failed to run command!\n{str(error.original)}")

handle_errors(*bot.all_commands.values())

# Run bot
bot.run(bot_token)
