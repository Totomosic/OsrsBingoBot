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
    announcement_channel_id: int
    submission_channel_id: int
    log_channel_id: int
    voting_time_seconds: int
    voting_task_count: int
    task_duration_seconds: int
    admin_role_id: int

@dataclasses.dataclass
class BotContext:
    database: model.DatabaseConnection
    announcement_channel: discord.TextChannel
    submission_channel: discord.TextChannel

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
        voting_task_count=3,
    )

# Initialize bot

COMMAND_PREFIX = "!"

BOT_ACKNOWLEDGE_REACTION = "✅"

class BotLogger:
    def __init__(self, channel: discord.TextChannel):
        self.channel = channel

    async def info(self, msg: str):
        await self.channel.send(msg)

class BingoBot(commands.Bot):
    async def on_ready(self):
        g_context.announcement_channel = self.get_channel(config.announcement_channel_id)
        g_context.submission_channel = self.get_channel(config.submission_channel_id)
        log_channel = self.get_channel(config.log_channel_id)
        self.logger = BotLogger(log_channel)
        self.loop.create_task(self.reaction_added_watcher())
        self.loop.create_task(self.reaction_removed_watcher())
        self.loop.create_task(self.vote_start_watcher())
        self.loop.create_task(self.vote_ended_watcher())

    async def vote_start_watcher(self):
        while True:
            active_task = g_context.database.get_active_task_instance()
            active_vote = g_context.database.get_active_vote()
            if active_task is not None and active_vote is None:
                now = datetime.datetime.now()
                if now + datetime.timedelta(seconds=config.voting_time_seconds) > active_task.end_time:
                    await start_new_vote()
            await asyncio.sleep(3)

    async def vote_ended_watcher(self):
        while True:
            now = datetime.datetime.now()
            active_vote = g_context.database.get_active_vote()
            if active_vote is not None and active_vote.end_time < now:
                await finish_vote(active_vote)
            await asyncio.sleep(3)

    async def reaction_added_watcher(self):
        while True:
            reaction: discord.RawReactionActionEvent = await self.wait_for("raw_reaction_add")
            if reaction.user_id != self.user.id and reaction.channel_id == g_context.submission_channel.id and is_user_id_bingo_admin(reaction.guild_id, reaction.user_id):
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
                            await message.add_reaction(BOT_ACKNOWLEDGE_REACTION)
                            await self.logger.info(f"Added completion for user {user.mention} (Approved by {approver.mention})")
                        else:
                            await self.logger.info(f"Task has already been completed by {user.mention}")

    async def reaction_removed_watcher(self):
        while True:
            reaction: discord.RawReactionActionEvent = await self.wait_for("raw_reaction_remove")
            if reaction.user_id != self.user.id and reaction.channel_id == g_context.submission_channel.id and is_user_id_bingo_admin(reaction.guild_id, reaction.user_id):
                message = self.get_channel(reaction.channel_id).get_partial_message(reaction.message_id)
                message = await message.fetch()
                if message.author.id != self.user.id:
                    completions = g_context.database.remove_completions_from_message(message.id)
                    await message.remove_reaction(BOT_ACKNOWLEDGE_REACTION, self.user)
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
    announcement_channel=None,
    submission_channel=None,
)
g_context.database.initialize()
with open(config.tasks_filename, "r") as f:
    tasks = f.readlines()
parsed_tasks = []
for index, task in enumerate(tasks):
    parts = task.split(";")
    if len(parts) == 2:
        parsed_tasks.append(
            model.Task(
                id=index + 1,
                description=parts[0],
                weight=1,
                instruction=parts[1],
            )
        )
g_context.database.insert_tasks(parsed_tasks)

# Helper methods

def is_bingo_admin(user: discord.Member):
    return bool(user.get_role(config.admin_role_id))

def is_user_id_bingo_admin(guild_id: int, user_id: int):
    guild = bot.get_guild(guild_id)
    user = guild.get_member(user_id)
    if user is not None:
        return is_bingo_admin(user)
    return False

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

async def end_task(task_instance: model.TaskInstance):
    channel = g_context.announcement_channel
    message = channel.get_partial_message(task_instance.message_id)
    message = await message.fetch()

    task = g_context.database.get_task_by_id(task_instance.task_id)

    embed = discord.Embed(title="Ended Task")
    embed.description = f"{task_instance.evaluated_task}\n\n**Submission Instructions:**\n{task.instruction}\n\nEnded at <t:{int(task_instance.end_time.timestamp())}>"
    embed.color = 0xFF0000
    await message.edit(embed=embed)

async def cancel_vote(vote: model.TaskVote):
    channel = g_context.announcement_channel
    message = channel.get_partial_message(vote.voting_message_id)
    await message.delete()
    vote.completed = True
    vote.end_time = datetime.datetime.now()
    g_context.database.update_vote(vote)

async def finish_vote(vote: model.TaskVote):
    vote.completed = True
    g_context.database.update_vote(vote)

    channel = g_context.announcement_channel
    message = channel.get_partial_message(vote.voting_message_id)
    message = await message.fetch()

    reactions = message.reactions
    vote_options = g_context.database.get_vote_options(vote.id)
    reaction_counts = []
    for reaction in reactions:
        if str(reaction.emoji) in number_reactions:
            index = number_reactions.index(str(reaction.emoji))
            if index < len(vote_options):
                reaction_counts.append((index, reaction.count))
    await message.clear_reactions()

    reaction_counts.sort(key=lambda pair: pair[1], reverse=True)
    selected_index = reaction_counts[0][0]

    selected_option = vote_options[selected_index]
    selected_task = g_context.database.get_task_by_id(selected_option.task_id)

    previous_task = g_context.database.get_most_recent_task_instance()
    new_task = model.TaskInstance(
        id=None,
        task_id=selected_option.task_id,
        evaluated_task=selected_option.evaluated_task,
        start_time=datetime.datetime.now(),
        end_time=utils.round_datetime(datetime.datetime.now() + datetime.timedelta(seconds=config.task_duration_seconds)),
        channel_id=channel.id,
        message_id=message.id,
    )
    g_context.database.create_task_instance(new_task)

    embed = discord.Embed()
    embed.title = "Current Task"
    embed.description = f"{selected_option.evaluated_task}\n\n**Submission Instructions:**\n{selected_task.instruction}\n\nEnds <t:{int(new_task.end_time.timestamp())}:R>"
    embed.color = 0x00FF00
    await message.edit(embed=embed)

    if previous_task is not None:
        await end_task(previous_task)

async def start_new_vote():
    database = g_context.database
    active_vote = database.get_active_vote()
    if active_vote is not None:
        await cancel_vote(active_vote)
    tasks = g_context.database.get_random_tasks(config.voting_task_count)
    parsed_tasks = [model.ParsedTask.from_task(task) for task in tasks]

    start_time = datetime.datetime.now()
    end_time = utils.round_datetime(start_time + datetime.timedelta(seconds=config.voting_time_seconds))

    evaluated_tasks = [task.description.evaluate() for task in parsed_tasks]
    message_choices = "\n".join([f"{number_reactions[idx]} {task}" for idx, task in enumerate(evaluated_tasks)])

    embed = discord.Embed(
        title="Vote for next task",
        color=0x0099FF,
        description=f"Voting ends <t:{int(end_time.timestamp())}:R>\n\n" + message_choices
    )
    message = await g_context.announcement_channel.send(embed=embed)
    for i in range(config.voting_task_count):
        await message.add_reaction(number_reactions[i])

    vote_obj = model.TaskVote(
        id=None,
        start_time=start_time,
        end_time=end_time,
        completed=False,
        voting_channel_id=str(g_context.announcement_channel.id),
        voting_message_id=str(message.id),
    )
    database.create_vote(vote_obj)

    for i in range(config.voting_task_count):
        option = model.TaskVoteOption(
            id=None,
            vote_id=vote_obj.id,
            option_index=i,
            task_id=tasks[i].id,
            evaluated_task=evaluated_tasks[i],
        )
        database.add_vote_option(option)

# Setup bot commands

@bot.command()
async def list(ctx: commands.Context, page: int = 1):
    if not is_bingo_admin(ctx.author):
        return
    tasks = g_context.database.get_tasks()
    formatted_tasks = [f"**{task.id}** {task.description}" for task in tasks]
    paginator = utils.Paginator(bot, formatted_tasks, per_page=25, start_page=page)
    await paginator.send(ctx)

@bot.command()
async def task(ctx: commands.Context, task_id: int = None):
    if not is_bingo_admin(ctx.author):
        return
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
    if not is_bingo_admin(ctx.author):
        return
    template_obj = templates.ParsedTemplate(template)
    existing_task = g_context.database.get_task_by_id(task_id)
    if not existing_task:
        raise Exception(f"No task with ID {task_id}")
    existing_task.description = template_obj.get_template()
    g_context.database.update_task(existing_task)
    await ctx.send(f"Successfully updated task **{task_id}**: {existing_task.description}")

@bot.command()
async def startvote(ctx: commands.Context):
    if not is_bingo_admin(ctx.author):
        return
    await start_new_vote()

@bot.command()
async def activetask(ctx: commands.Context):
    if not is_bingo_admin(ctx.author):
        return
    task_instance = g_context.database.get_active_task_instance()
    if task_instance is not None:
        await ctx.send(f"Active task: {task_instance.evaluated_task}")
    else:
        await ctx.send("No active task")

@bot.command()
async def completions(ctx: commands.Context):
    if not is_bingo_admin(ctx.author):
        return
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
