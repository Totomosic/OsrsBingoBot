import asyncio
import argparse
from collections import defaultdict
import dataclasses
import discord
from discord.ext import commands
import json
import math
import os
import time
import traceback

import model
import tasks
import templates

@dataclasses.dataclass
class BotConfig:
    bot_token: str
    tasks_filename: str
    database_dsn: str

@dataclasses.dataclass
class BotContext:
    database: model.DatabaseConnection
    tasks: tasks.TaskDatabase

def read_discord_token(config: BotConfig) -> str:
    return config.bot_token

def get_config_from_args() -> BotConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("config_filename", type=str, help="Path to config file")
    args = parser.parse_args()

    with open(args.config_filename, "r") as f:
        config_data = json.load(f)

    return BotConfig(
        **config_data,
        database_dsn=os.environ["DB_URI"],
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
    database=model.DatabaseConnection(config.database_dsn),
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

number_reactions = [
    "1️⃣",
    "2️⃣",
    "3️⃣",
    "4️⃣",
    "5️⃣",
    "6️⃣",
    "7️⃣",
    "8️⃣",
    "9️⃣",
]

@bot.command()
async def choice(ctx: commands.Context):
    nchoices = 3
    voting_time_seconds = 10
    end_voting_time = int(time.time() + voting_time_seconds)
    tasks = g_context.tasks.get_random_tasks(nchoices)

    evaluated_tasks = [task.task.evaluate() for task in tasks]
    message_choices = "\n".join([f"{number_reactions[idx]} {task}" for idx, task in enumerate(evaluated_tasks)])

    embed = discord.Embed(
        title="Tasks",
        color=0x0099FF,
        description=f"Voting ends <t:{end_voting_time}:R>\n\n" + message_choices
    )
    message = await ctx.send(embed=embed)
    for i in range(nchoices):
        await message.add_reaction(number_reactions[i])

    await asyncio.sleep(voting_time_seconds)
    embed.description = "Voting ended\n\n" + message_choices
    await message.edit(embed=embed)
    message = await message.fetch()
    reactions = message.reactions

    reaction_counts = []
    for reaction in reactions:
        if str(reaction.emoji) in number_reactions:
            index = number_reactions.index(str(reaction.emoji))
            reaction_counts.append((index, reaction.count))

    reaction_counts.sort(key=lambda pair: pair[1], reverse=True)
    task = evaluated_tasks[reaction_counts[0][0]]
    await message.clear_reactions()

    embed.description = "Voting ended\n\n" + message_choices + f"\n\nSelected Task:\n{task}"
    await message.edit(embed=embed)


def handle_errors(*cmds):
    for cmd in cmds:
        @cmd.error
        async def error_handler(ctx: commands.Context, error):
            await ctx.send(f"Failed to run command!\n{str(traceback.format_exc())}")

handle_errors(*bot.all_commands.values())

# Run bot
bot.run(bot_token)
