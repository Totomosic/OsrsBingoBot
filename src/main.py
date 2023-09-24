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
import random
import traceback
import re

from discord.message import Message
from discord.reaction import Reaction

import model
import templates
import utils

@dataclasses.dataclass
class BotConfig:
    bot_token: str
    tasks_filename: str
    database_dsn: str
    announcement_channel_id: int
    submission_channel_id: int
    log_channel_id: int
    voting_time_seconds: int
    task_start_delay_seconds: int
    voting_task_count: int
    task_duration_seconds: int
    admin_role_id: int
    community_role_id: int
    winner_task_count: int
    log_filename: str

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

    config = BotConfig(
        **config_data,
        database_dsn=os.environ["DB_URI"],
        voting_task_count=3,
    )
    logging.basicConfig(
        filename=config.log_filename,
        filemode='a',
        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
        level=logging.INFO,
    )
    return config

# Initialize bot

COMMAND_PREFIX = "!"

BOT_ACKNOWLEDGE_REACTION = "✅"

def get_task_type_from_message(message: discord.Message) -> str:
    if message.content.strip().lower().startswith("bonus"):
        return model.TASK_TYPE_BONUS
    return model.TASK_TYPE_STANDARD

class BotLogger:
    def __init__(self, channel: discord.TextChannel):
        self.channel = channel

    async def info(self, msg: str):
        logging.info(msg)
        if self.channel is not None:
            await self.channel.send(msg)

class BingoBot(commands.Bot):
    async def on_ready(self):
        g_context.announcement_channel = self.get_channel(config.announcement_channel_id)
        g_context.submission_channel = self.get_channel(config.submission_channel_id)
        log_channel = self.get_channel(config.log_channel_id) if config.log_channel_id is not None else None
        self.logger = BotLogger(log_channel)
        self.loop.create_task(self.reaction_added_watcher())
        self.loop.create_task(self.reaction_removed_watcher())
        self.loop.create_task(self.vote_start_watcher())
        self.loop.create_task(self.vote_ended_watcher())
        # self.loop.create_task(self.winner_watcher())
        self.loop.create_task(self.task_start_watcher())
        logging.info("Bot online")

    async def vote_start_watcher(self):
        while True:
            active_task = g_context.database.get_active_task_instance()
            active_vote = g_context.database.get_active_vote()
            if active_task is not None and active_vote is None:
                now = datetime.datetime.now()
                if now + datetime.timedelta(seconds=config.voting_time_seconds) > active_task.end_time:
                    await start_new_vote()
                    bonus_task = g_context.database.get_active_task_instance(task_type=model.TASK_TYPE_BONUS)
                    if bonus_task is not None:
                        await post_task_instance(bonus_task)
            await asyncio.sleep(3)

    async def task_start_watcher(self):
        while True:
            active_vote = g_context.database.get_active_vote()
            if active_vote is not None and active_vote.selected_option_id is not None:
                now = datetime.datetime.now()
                if now - datetime.timedelta(seconds=config.task_start_delay_seconds) > active_vote.end_time:
                    selected_option = g_context.database.get_vote_option_by_id(active_vote.selected_option_id)
                    selected_task = g_context.database.get_task_by_id(selected_option.task_id)

                    active_vote.completed = True
                    g_context.database.update_vote(active_vote)

                    new_task = await start_task(selected_task, evaluated_task=selected_option.evaluated_task)
                    logging.info(f"Vote finished, winning index {selected_option.option_index}")
                    logging.info(f"Selected task: {new_task.evaluated_task} (TaskId={selected_task.id}) (TaskInstanceId={new_task.id})")

                    vote_message = g_context.announcement_channel.get_partial_message(active_vote.voting_message_id)
                    if vote_message is not None:
                        try:
                            await vote_message.delete()
                        except discord.NotFound:
                            pass
            await asyncio.sleep(3)

    async def vote_ended_watcher(self):
        while True:
            now = datetime.datetime.now()
            active_vote = g_context.database.get_active_vote()
            if active_vote is not None and active_vote.end_time < now:
                await finish_vote(active_vote)
            await asyncio.sleep(3)

    # async def winner_watcher(self):
    #     while True:
    #         unclaimed_tasks = g_context.database.get_unclaimed_tasks()
    #         standard_unclaimed_tasks = [task for task in unclaimed_tasks if task.task_type == model.TASK_TYPE_STANDARD]
    #         if len(standard_unclaimed_tasks) >= config.winner_task_count:
    #             all_task_completions: list[model.TaskCompletion] = []
    #             for task in unclaimed_tasks:
    #                 all_task_completions += g_context.database.get_task_completions(task.id)
    #             channel = g_context.announcement_channel
    #             if len(all_task_completions) > 0:
    #                 winner = random.choice(all_task_completions)
    #                 user = self.get_user(int(winner.user_id))
    #                 while user is None and len(all_task_completions) > 1:
    #                     all_task_completions.remove(winner)
    #                     winner = random.choice(all_task_completions)
    #                     user = self.get_user(int(winner.user_id))
    #                 if user is not None:
    #                     embed = discord.Embed(title="Congratuations!")
    #                     embed.description = f"The winner is {user.mention}"
    #                     await channel.send(embed=embed)
    #             else:
    #                 embed = discord.Embed(title="Congratuations!")
    #                 embed.description = f"No winners"
    #                 await channel.send(embed=embed)
    #             for task in unclaimed_tasks:
    #                 task.drawn_prize = True
    #                 g_context.database.update_task_instance(task)
    #         await asyncio.sleep(60)

    async def reaction_added_watcher(self):
        while True:
            reaction: discord.RawReactionActionEvent = await self.wait_for("raw_reaction_add")
            if reaction.user_id != self.user.id and reaction.channel_id == g_context.submission_channel.id and is_user_id_bingo_admin(reaction.guild_id, reaction.user_id):
                message = self.get_channel(reaction.channel_id).get_partial_message(reaction.message_id)
                try:
                    message = await message.fetch()
                except discord.errors.NotFound:
                    return
                if message.author.id != self.user.id:
                    active_task = g_context.database.get_task_instance_by_time(message.created_at, task_type=get_task_type_from_message(message))
                    if active_task is not None:
                        completion = model.TaskCompletion(
                            id=None,
                            instance_id=active_task.id,
                            user_id=message.author.id,
                            approver_id=reaction.user_id,
                            completion_time=message.created_at,
                            evidence_channel_id=reaction.channel_id,
                            evidence_message_id=reaction.message_id,
                        )
                        user = self.get_user(completion.user_id)
                        approver = self.get_user(completion.approver_id)
                        if g_context.database.add_task_completion(completion):
                            await message.add_reaction(BOT_ACKNOWLEDGE_REACTION)
                            await self.logger.info(f"Added completion for user {user.mention} (Approved by {approver.mention}) (Type={active_task.task_type})")
                        else:
                            await self.logger.info(f"Task has already been completed by {user.mention}")
                    else:
                        await self.logger.info(f"No active task to approve")

    async def reaction_removed_watcher(self):
        while True:
            reaction: discord.RawReactionActionEvent = await self.wait_for("raw_reaction_remove")
            if reaction.user_id != self.user.id and reaction.channel_id == g_context.submission_channel.id and is_user_id_bingo_admin(reaction.guild_id, reaction.user_id):
                message = self.get_channel(reaction.channel_id).get_partial_message(reaction.message_id)
                try:
                    message = await message.fetch()
                except discord.errors.NotFound:
                    return
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
    g_context.database.update_task_instance(task_instance)

    channel = g_context.announcement_channel
    if task_instance.message_id is not None:
        message = channel.get_partial_message(task_instance.message_id)
        if not message:
            return
        try:
            message = await message.fetch()
        except discord.errors.NotFound:
            return
        if message is not None:
            try:
                await message.delete()
            except discord.errors.NotFound:
                return

    # task = g_context.database.get_task_by_id(task_instance.task_id)
    # embed = discord.Embed(title="Ended Task")
    # embed.description = f"{task_instance.evaluated_task}\n\n**Submission Instructions:**\n{task.instruction}\n\nEnded at <t:{int(task_instance.end_time.timestamp())}>"
    # embed.color = 0xFF0000
    # await message.edit(embed=embed)

async def cancel_vote(vote: model.TaskVote):
    channel = g_context.announcement_channel
    message = channel.get_partial_message(vote.voting_message_id)
    try:
        await message.delete()
    except discord.errors.NotFound:
        pass
    g_context.database.delete_vote(vote)

TASK_TYPE_TITLE = {
    model.TASK_TYPE_STANDARD: "Current Task",
    model.TASK_TYPE_BONUS: "Bonus Task",
}

TASK_TYPE_COLOR = {
    model.TASK_TYPE_STANDARD: 0x00FF00,
    model.TASK_TYPE_BONUS: 0xFF00FF,
}

async def post_task_instance(task_instance: model.TaskInstance):
    task = g_context.database.get_task_by_id(task_instance.task_id)
    if not task:
        return False

    role = g_context.announcement_channel.guild.get_role(config.community_role_id)
    content = ""
    if role is not None and task_instance.task_type == model.TASK_TYPE_STANDARD:
        content = role.mention

    task_description = f"{task_instance.evaluated_task}\n\n**Submission Instructions:**\n{task.instruction}\nPost all screenshots as **one message** in {g_context.submission_channel.jump_url}"
    if task_instance.task_type == model.TASK_TYPE_BONUS:
        task_description += "\n**Include the word \"Bonus\" at the start of your submission message**"
    task_description += f"\n\nEnds <t:{int(task_instance.end_time.timestamp())}:R>"

    embed = discord.Embed()
    embed.title = TASK_TYPE_TITLE[task_instance.task_type]
    embed.description = task_description
    embed.color = TASK_TYPE_COLOR[task_instance.task_type]
    task_message = await g_context.announcement_channel.send(content=content, embed=embed)

    task_instance.message_id = task_message.id
    task_instance.channel_id = g_context.announcement_channel.id
    g_context.database.update_task_instance(task_instance)

async def start_task(selected_task: model.Task, evaluated_task: str):
    task_start_time = datetime.datetime.now()
    task_end_time = utils.round_datetime(task_start_time + datetime.timedelta(seconds=config.task_duration_seconds))

    previous_task = g_context.database.get_most_recent_task_instance()
    new_task = model.TaskInstance(
        id=None,
        task_id=selected_task.id,
        task_type=model.TASK_TYPE_STANDARD,
        evaluated_task=evaluated_task,
        start_time=task_start_time,
        end_time=task_end_time,
        channel_id=g_context.announcement_channel.id,
        message_id=None,
        drawn_prize=False,
    )
    g_context.database.create_task_instance(new_task)

    await post_task_instance(new_task)

    if previous_task is not None:
        await end_task(previous_task)
    previous_bonus_task = g_context.database.get_most_recent_task_instance(task_type=model.TASK_TYPE_BONUS)
    if previous_bonus_task is not None:
        await end_task(previous_bonus_task)
    return new_task

async def create_bonus_task(task_description: str, task_instruction: str):
    active_standard_task = g_context.database.get_active_task_instance(task_type=model.TASK_TYPE_STANDARD)
    if not active_standard_task:
        return None

    task = model.Task(
        id=max(100000, g_context.database.get_max_task_id() + 1),
        description=task_description,
        instruction=task_instruction,
        weight=0,
    )
    parsed_task = model.ParsedTask.from_task(task)
    g_context.database.insert_task(task)
    previous_task = g_context.database.get_most_recent_task_instance(task_type=model.TASK_TYPE_BONUS)

    new_task_instance = model.TaskInstance(
        id=None,
        task_id=task.id,
        task_type=model.TASK_TYPE_BONUS,
        evaluated_task=parsed_task.description.evaluate(),
        start_time=datetime.datetime.now(),
        end_time=active_standard_task.end_time,
        channel_id=None,
        message_id=None,
        drawn_prize=False,
    )
    g_context.database.create_task_instance(new_task_instance)

    if previous_task:
        await end_task(previous_task)
    return new_task_instance

async def finish_vote(vote: model.TaskVote):
    channel = g_context.announcement_channel
    message = channel.get_partial_message(vote.voting_message_id)
    if message is None:
        return
    try:
        message = await message.fetch()
    except discord.errors.NotFound:
        return

    reactions = message.reactions
    vote_options = g_context.database.get_vote_options(vote.id)
    reaction_counts = []
    for reaction in reactions:
        if str(reaction.emoji) in number_reactions:
            index = number_reactions.index(str(reaction.emoji))
            if index < len(vote_options):
                reaction_counts.append((index, reaction.count))
    await message.clear_reactions()

    if len(reaction_counts) == 0:
        return

    reaction_counts.sort(key=lambda pair: pair[1], reverse=True)
    selected_index: int = reaction_counts[0][0]

    selected_option = vote_options[selected_index]
    vote.selected_option_id = selected_option.id
    g_context.database.update_vote(vote)

    embed = discord.Embed(
        title="Vote ended",
        color=0x0099FF,
        description=f"**Selected task**\n{selected_option.evaluated_task}"
    )
    await message.edit(embed=embed)

async def start_new_vote(end_time_override: datetime.datetime = None):
    database = g_context.database
    active_vote = database.get_active_vote()
    if active_vote is not None:
        await cancel_vote(active_vote)
    tasks = database.get_random_tasks(config.voting_task_count)
    parsed_tasks = [model.ParsedTask.from_task(task) for task in tasks]

    start_time = datetime.datetime.now()
    end_time = end_time_override or utils.round_datetime(start_time + datetime.timedelta(seconds=config.voting_time_seconds - config.task_start_delay_seconds))

    evaluated_tasks = [task.description.evaluate() for task in parsed_tasks]
    message_choices = "\n".join([f"{number_reactions[idx]} {task}" for idx, task in enumerate(evaluated_tasks)])

    embed = discord.Embed(
        title="Vote for next task",
        color=0x0099FF,
        description=f"Voting ends <t:{int(end_time.timestamp())}:R>\n\n" + message_choices
    )
    role = g_context.announcement_channel.guild.get_role(config.community_role_id)
    content = ""
    if role is not None:
        content = role.mention
    message = await g_context.announcement_channel.send(content=content, embed=embed)
    for i in range(config.voting_task_count):
        await message.add_reaction(number_reactions[i])

    vote_obj = model.TaskVote(
        id=None,
        start_time=start_time,
        end_time=end_time,
        completed=False,
        voting_channel_id=str(g_context.announcement_channel.id),
        voting_message_id=str(message.id),
        selected_option_id=None,
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

    logging.info(f"Starting vote with {config.voting_task_count} options")
    for i in range(config.voting_task_count):
        logging.info(f"\t{i + 1}. {evaluated_tasks[i]} (TaskId={tasks[i].id})")

@dataclasses.dataclass
class TaskStats:
    tasks: list[model.TaskInstance]
    completions: list[model.TaskCompletion]

    def has_completions(self):
        return len(self.completions) > 0

    def get_standard_tasks(self) -> list[model.TaskInstance]:
        return [t for t in self.tasks if t.task_type == model.TASK_TYPE_STANDARD]

    def get_bonus_tasks(self) -> list[model.TaskInstance]:
        return [t for t in self.tasks if t.task_type == model.TASK_TYPE_BONUS]

    def get_standard_task_completions(self) -> list[model.TaskCompletion]:
        standard_task_ids = [t.id for t in self.get_standard_tasks()]
        return [c for c in self.completions if c.instance_id in standard_task_ids]

    def get_bonus_task_completions(self) -> list[model.TaskCompletion]:
        bonus_task_ids = [t.id for t in self.get_bonus_tasks()]
        return [c for c in self.completions if c.instance_id in bonus_task_ids]

    def get_unique_user_ids(self) -> list[int]:
        user_ids = set([int(c.user_id) for c in self.completions])
        return list(user_ids)

    def get_completions_for_user(self, user_id: int) -> tuple[list[model.TaskCompletion], list[model.TaskCompletion]]:
        standard_completions = self.get_standard_task_completions()
        bonus_completions = self.get_bonus_task_completions()
        return [c for c in standard_completions if int(c.user_id) == int(user_id)], [c for c in bonus_completions if int(c.user_id) == int(user_id)]

def compute_task_stats(tasks: list[model.TaskInstance]):
    stats = TaskStats(tasks=tasks, completions=[])
    for task in tasks:
        stats.completions += g_context.database.get_task_completions(task.id)
    return stats

async def draw_winner_with_tasks(tasks: list[model.TaskInstance], existing_message: discord.Message = None, channel: discord.TextChannel = None, update_tasks: bool = True):
    stats = compute_task_stats(tasks)
    channel = channel or g_context.announcement_channel
    if stats.has_completions():
        winner = random.choice(stats.completions)
        user = bot.get_user(int(winner.user_id))
        while user is None and len(stats.completions) > 1:
            stats.completions.remove(winner)
            winner = random.choice(stats.completions)
            user = bot.get_user(int(winner.user_id))
        if user is not None:
            role = g_context.announcement_channel.guild.get_role(config.community_role_id)
            content = ""
            if role is not None:
                content = role.mention
            embed = discord.Embed()
            embed.color = 0xf9cd46
            description = f"In the last {len(stats.get_standard_tasks())} weeks, there were...\n\n"
            description += f"**{len(stats.completions)}** total task completions ({len(stats.get_standard_task_completions())} standard tasks, {len(stats.get_bonus_task_completions())} bonus tasks)\n"
            description += f"**{len(stats.get_unique_user_ids())}** unique participants\n\n"
            description += f"**The winner is, {user.mention if user is not None else 'Unknown'}!**\n\n"
            description += "Please message a task admin to claim your prize."
            embed.description = description
            if existing_message is not None:
                await existing_message.edit(embed=embed, content=content)
            else:
                await channel.send(embed=embed, content=content)
    else:
        embed = discord.Embed(title="Congratuations!")
        embed.description = f"No winners"
        if existing_message is not None:
            await existing_message.edit(embed=embed, content=content)
        else:
            await channel.send(embed=embed, content=content)
    if update_tasks:
        for task in tasks:
            task.drawn_prize = True
            g_context.database.update_task_instance(task)

async def draw_winner(channel: discord.TextChannel = None, update_tasks: bool = True):
    unclaimed_tasks = g_context.database.get_unclaimed_tasks()
    await draw_winner_with_tasks(unclaimed_tasks, existing_message=None, channel=channel, update_tasks=update_tasks)

# Setup bot commands

@bot.command()
async def bonustask(ctx: commands.Context, task_description: str, task_instruction: str):
    if not is_bingo_admin(ctx.author):
        return
    bonus_task = await create_bonus_task(task_description, task_instruction)
    if bonus_task is not None:
        await ctx.send(f"Bonus task created - {bonus_task.evaluated_task} - will be announced with next vote")
    else:
        logging.error(f"Failed to create bonus task")

@bot.command()
async def listtasks(ctx: commands.Context, page: int = 1):
    if not is_bingo_admin(ctx.author):
        return
    tasks = g_context.database.get_standard_tasks()
    formatted_tasks = [f"**{task.id}** {task.description}" for task in tasks]
    paginator = utils.Paginator(bot, formatted_tasks, per_page=25, start_page=page)
    await paginator.send(ctx)

@bot.command()
async def gettask(ctx: commands.Context, task_id: int = None):
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
async def startvote(ctx: commands.Context, end_time: int = None):
    if not is_bingo_admin(ctx.author):
        return
    await start_new_vote(end_time_override=datetime.datetime.fromtimestamp(end_time) if end_time is not None else None)

@bot.command()
async def drawwinner(ctx: commands.Context):
    if not is_bingo_admin(ctx.author):
        return
    await draw_winner()

@bot.command()
async def testwinner(ctx: commands.Context):
    if not is_bingo_admin(ctx.author):
        return
    await draw_winner(channel=ctx.channel, update_tasks=False)

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

@bot.command()
async def reloadtasks(ctx: commands.Context):
    if not is_bingo_admin(ctx.author):
        return
    g_context.database.delete_all_tasks()
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

@bot.command()
async def rerollwinner(ctx: commands.Context, message_id: str):
    channel = g_context.announcement_channel
    message = channel.get_partial_message(int(message_id))
    if not message:
        ctx.send("No message found")
        return
    try:
        message = await message.fetch()
        message_content = message.embeds[0].description
        pattern = "In the last (\d+) weeks, there were..."
        matches = re.match(pattern, message_content)
        if not matches:
            ctx.send("Invalid message format")
            return
        weeks = int(matches.group(1))
        logging.info(f"Weeks {weeks}")
        timestamp = message.created_at - datetime.timedelta(seconds=weeks * config.task_duration_seconds)
        task_instances = g_context.database.get_completed_tasks_between(timestamp, message.created_at)
        await draw_winner_with_tasks(task_instances, existing_message=message, update_tasks=False)
    except discord.errors.NotFound:
        ctx.send("No message found")
        return

@bot.command()
async def taskcount(ctx: commands.Context):
    if not is_bingo_admin(ctx.author):
        return
    completed_tasks = g_context.database.get_unclaimed_tasks()
    standard_tasks = [t for t in completed_tasks if t.task_type == model.TASK_TYPE_STANDARD]
    bonus_tasks = [t for t in completed_tasks if t.task_type == model.TASK_TYPE_BONUS]
    await ctx.send(f"{len(standard_tasks)} standard, {len(bonus_tasks)} bonus")

@bot.command()
async def testpermissions(ctx: commands.Context):
    if not is_bingo_admin(ctx.author):
        return
    message = await ctx.send("Test message")
    await message.add_reaction(BOT_ACKNOWLEDGE_REACTION)
    await asyncio.sleep(2)
    await message.remove_reaction(BOT_ACKNOWLEDGE_REACTION, bot.user)
    await asyncio.sleep(2)
    await message.delete()

def handle_errors(*cmds):
    for cmd in cmds:
        @cmd.error
        async def error_handler(ctx: commands.Context, error):
            logging.error(traceback.format_exc())
            # await ctx.send(f"Failed to run command!\n{str(traceback.format_exc())}")

handle_errors(*bot.all_commands.values())

# Run bot
bot.run(bot_token)
