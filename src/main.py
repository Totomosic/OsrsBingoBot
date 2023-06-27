import asyncio
import argparse
from collections import defaultdict
import dataclasses
import datetime
import discord
from discord.ext import commands
import json
import logging
import math
import os
import time
import traceback

from discord.message import Message
from discord.reaction import Reaction

import model
import templates
import utils

logging.basicConfig(level=logging.INFO)

@dataclasses.dataclass
class BotConfig:
    bot_token: str
    tasks_filename: str
    database_dsn: str

@dataclasses.dataclass
class BotContext:
    database: model.DatabaseConnection

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

COMMAND_PREFIX = "!"

class BotLogger:
    def __init__(self, channel: discord.TextChannel):
        self.channel = channel

    async def info(self, msg: str):
        await self.channel.send(msg)

class BingoBot(commands.Bot):
    async def on_ready(self):
        channel = self.get_channel(1122929431929954394)
        self.logger = BotLogger(channel)
        self.loop.create_task(self.reaction_added_watcher())
        self.loop.create_task(self.reaction_removed_watcher())

    async def on_message(self, message: Message):
        return await super().on_message(message)

    async def reaction_added_watcher(self):
        while True:
            reaction: discord.RawReactionActionEvent = await self.wait_for("raw_reaction_add")
            if reaction.user_id != self.user.id:
                message = self.get_channel(reaction.channel_id).get_partial_message(reaction.message_id)
                message = await message.fetch()
                if message.author.id != self.user.id:
                    active_task = g_context.database.get_active_task_instance()
                    if active_task is not None:
                        completion = model.TaskCompletion(
                            id=None,
                            instance_id=active_task.id,
                            user_id=message.author.id,
                            approver_id=reaction.user_id,
                            completion_time=datetime.datetime.now(),
                            evidence_channel_id=reaction.channel_id,
                            evidence_message_id=reaction.message_id,
                        )
                        user = self.get_user(completion.user_id)
                        approver = self.get_user(completion.approver_id)
                        if g_context.database.add_task_completion(completion):
                            await self.logger.info(f"Added completion for user {user.mention} (Approved by {approver.mention})")
                        else:
                            await self.logger.info(f"Task has already been completed by {user.mention}")

    async def reaction_removed_watcher(self):
        while True:
            reaction: discord.RawReactionActionEvent = await self.wait_for("raw_reaction_remove")
            if reaction.user_id != self.user.id:
                message = self.get_channel(reaction.channel_id).get_partial_message(reaction.message_id)
                message = await message.fetch()
                if message.author.id != self.user.id:
                    completions = g_context.database.remove_completions_from_message(message.id)
                    for completion in completions:
                        user = self.get_user(int(completion.user_id))
                        approver = self.get_user(int(completion.approver_id))
                        await self.logger.info(f"Removed completion for user {user.mention} (Approved by {approver.mention})")

config = get_config_from_args()
bot_token = read_discord_token(config)
description = """Discord osrs bot"""
bot = BingoBot(
    intents=discord.Intents.all(),
    command_prefix=COMMAND_PREFIX,
    description=description,
    case_insensitive=True,
)

g_context = BotContext(
    database=model.DatabaseConnection(config.database_dsn),
)
g_context.database.initialize()
with open(config.tasks_filename, "r") as f:
    tasks_data = json.load(f)
parsed_tasks = [model.Task(id=task["id"], description=task["task"], weight=task["weight"], instruction="test") for task in tasks_data["tasks"]]
g_context.database.insert_tasks(parsed_tasks)

# Setup bot commands

@bot.command()
async def list(ctx: commands.Context, page: int = 1):
    tasks = g_context.database.get_tasks()
    formatted_tasks = [f"**{task.id}** {task.description}" for task in tasks]
    paginator = utils.Paginator(bot, formatted_tasks, per_page=25, start_page=page)
    await paginator.send(ctx)

@bot.command()
async def task(ctx: commands.Context, task_id: int = None):
    if task_id is None:
        task = g_context.database.get_random_task()
    else:
        task = g_context.database.get_task_by_id(task_id)
        if task is None:
            raise Exception(f"No task with ID {task_id}")
    parsed_task = model.ParsedTask.from_task(task)
    embed = discord.Embed(
        title=f"Task {parsed_task.id}",
        color=0x0099FF,
        description=parsed_task.description.evaluate(),
    )
    # embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar.url)
    await ctx.send(embed=embed)

@bot.command()
async def edit(ctx: commands.Context, task_id: int, template: str):
    template_obj = templates.ParsedTemplate(template)
    existing_task = g_context.database.get_task_by_id(task_id)
    if not existing_task:
        raise Exception(f"No task with ID {task_id}")
    existing_task.description = template_obj.get_template()
    g_context.database.update_task(existing_task)
    await ctx.send(f"Successfully updated task **{task_id}**: {existing_task.description}")

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
    tasks = g_context.database.get_random_tasks(nchoices)
    parsed_tasks = [model.ParsedTask.from_task(task) for task in tasks]

    evaluated_tasks = [task.description.evaluate() for task in parsed_tasks]
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
    selected_index = reaction_counts[0][0]
    task = evaluated_tasks[selected_index]
    await message.clear_reactions()

    task_instance = g_context.database.create_task_instance(tasks[selected_index].id, evaluated_tasks[selected_index])

    embed.description = f"Voting ended\n\n**Selected Task:**\n{task}\n\n**Submission Instructions:**\n{parsed_tasks[selected_index].instruction}\n\nEnds <t:{int(task_instance.end_time.timestamp())}:R>"
    await message.edit(embed=embed)

@bot.command()
async def activetask(ctx: commands.Context):
    task_instance = g_context.database.get_active_task_instance()
    if task_instance is not None:
        await ctx.send(f"Active task: {task_instance.evaluated_task}")
    else:
        await ctx.send("No active task")

@bot.command()
async def completions(ctx: commands.Context):
    active_task = g_context.database.get_active_task_instance()
    if active_task is not None:
        completions = g_context.database.get_task_completions(active_task.id)
        completion_strs = []
        for completion in completions:
            user = bot.get_user(int(completion.user_id))
            if user is not None:
                message = bot.get_channel(int(completion.evidence_channel_id)).get_partial_message(int(completion.evidence_message_id))
                completion_strs.append(f"{user.mention} at <t:{int(completion.completion_time.timestamp())}> ({message.jump_url})")
        embed = discord.Embed(
            title=f"Task {active_task.id} - {len(completion_strs)} Completions",
            color=0x0099FF,
            description="\n".join(completion_strs),
        )
        await ctx.send(embed=embed)
    else:
        await ctx.send("No active task")

def handle_errors(*cmds):
    for cmd in cmds:
        @cmd.error
        async def error_handler(ctx: commands.Context, error):
            await ctx.send(f"Failed to run command!\n{str(traceback.format_exc())}")

handle_errors(*bot.all_commands.values())

# Run bot
bot.run(bot_token)
