import argparse
import dataclasses
import discord
from discord.ext import commands

import tasks

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

@bot.command()
async def task(ctx: commands.Context):
    task = g_context.tasks.get_random_task()
    embed = discord.Embed(
        title=f"Task {task.id}",
        color=0x0099FF,
        description=task.task,
    )
    # embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar.url)
    await ctx.send(embed=embed)

# Run bot
bot.run(bot_token)
