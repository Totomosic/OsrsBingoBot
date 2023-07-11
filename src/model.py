import dataclasses
import datetime
import logging
import random
import psycopg2
import psycopg2.errors
from typing import Type, Generic, TypeVar, Union
import templates
import utils

T = TypeVar("T")

@dataclasses.dataclass
class Task:
    id: int
    description: str
    instruction: str
    weight: int

@dataclasses.dataclass
class ParsedTask:
    id: int
    description: templates.ParsedTemplate
    instruction: str
    weight: int

    def to_task(self):
        return Task(
            id=self.id,
            description=self.description.get_template(),
            instruction=self.instruction,
            weight=self.weight,
        )

    @classmethod
    def from_task(self, task: Task):
        return ParsedTask(
            id=task.id,
            description=templates.ParsedTemplate(task.description),
            instruction=task.instruction,
            weight=task.weight,
        )
    
TASK_TYPE_STANDARD = "Standard"
TASK_TYPE_BONUS = "Bonus"

@dataclasses.dataclass
class TaskInstance:
    id: int
    task_id: int
    task_type: str
    evaluated_task: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    channel_id: str
    message_id: str
    drawn_prize: bool

@dataclasses.dataclass
class TaskCompletion:
    id: int
    instance_id: int
    user_id: int
    approver_id: int
    completion_time: datetime.datetime
    evidence_channel_id: int
    evidence_message_id: int

@dataclasses.dataclass
class TaskVote:
    id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    completed: bool
    voting_channel_id: str
    voting_message_id: str
    selected_option_id: Union[int, None]

@dataclasses.dataclass
class TaskVoteOption:
    id: int
    vote_id: int
    option_index: int
    task_id: int
    evaluated_task: str

TASKS_TABLE = "tasks"
TASK_INSTANCES_TABLE = "task_instances"
TASK_COMPLETIONS_TABLE = "task_completions"

TASK_VOTING_TABLE = "task_votes"
TASK_VOTING_OPTION_TABLE = "task_vote_options"

def select_with_model(model: Type[T], connection: "psycopg2.connection", query: str, *vars) -> Union[T, None]:
    cursor = connection.cursor()
    cursor.execute(query, vars)
    row = cursor.fetchone()
    cursor.close()
    if row is not None:
        return model(*row)
    return None

def select_multiple_with_model(model: Type[T], connection: "psycopg2.connection", query: str, *vars) -> list[T]:
    cursor = connection.cursor()
    cursor.execute(query, vars)
    rows = cursor.fetchall()
    cursor.close()
    return [model(*row) for row in rows]

def insert_model(model: T, connection: "psycopg2.connection", table_name: str, return_col_name: str = None):
    cursor = connection.cursor()
    fields = model.__dataclass_fields__.keys()
    included_fields = [field for field in fields if field != "id" or getattr(model, field) is not None]
    values = [getattr(model, field) for field in included_fields]
    try:
        columns_str = ', '.join(included_fields)
        returning = f" RETURNING {return_col_name}" if return_col_name is not None else ""
        cursor.execute(f"INSERT INTO \"{table_name}\" ({columns_str}) VALUES ({', '.join('%s' for _ in values)}){returning}", values)
        result = None
        if return_col_name is not None:
            result = cursor.fetchone()[0]
        cursor.close()
        connection.commit()
        return result
    except psycopg2.errors.Error as e:
        connection.commit()
        raise e

def update_model(model: T, connection: "psycopg2.connection", table_name: str):
    cursor = connection.cursor()
    parameters = []
    for field in model.__dataclass_fields__.keys():
        parameters.append(field)
        parameters.append(getattr(model, field))
    parameters_arr = [f"{parameters[i * 2]}=%s" for i in range(len(parameters) // 2)]
    cursor.execute(f"UPDATE {table_name} SET {', '.join(parameters_arr)} WHERE id = %s", [*[param for idx, param in enumerate(parameters) if idx % 2 == 1], model.id])
    cursor.close()
    connection.commit()

class DatabaseConnection:
    def __init__(self, dsn: str):
        self.connection = psycopg2.connect(dsn=dsn)

    def get_tasks(self):
        return select_multiple_with_model(Task, self.connection, f"SELECT * FROM {TASKS_TABLE} ORDER BY id ASC")

    def get_standard_tasks(self):
        return [task for task in self.get_tasks() if task.weight > 0]

    def get_task_by_id(self, task_id: int):
        return select_with_model(Task, self.connection, f"SELECT * FROM {TASKS_TABLE} WHERE id = %s", task_id)

    def get_random_task(self):
        return random.choice(self.get_standard_tasks())

    def get_random_tasks(self, ntasks: int):
        return random.sample(self.get_standard_tasks(), k=ntasks)

    def insert_task(self, task: Task):
        insert_model(task, self.connection, TASKS_TABLE)

    def get_max_task_id(self):
        return select_with_model(int, self.connection, f"SELECT MAX(id) FROM {TASKS_TABLE}")

    def insert_tasks(self, tasks: list[Task]):
        for task in tasks:
            try:
                insert_model(task, self.connection, TASKS_TABLE)
            except psycopg2.errors.UniqueViolation:
                update_model(task, self.connection, TASKS_TABLE)

    def delete_all_tasks(self):
        cursor = self.connection.cursor()
        cursor.execute(f"DELETE FROM {TASKS_TABLE}")
        cursor.close()
        self.connection.commit()

    def update_task(self, task: Task):
        update_model(task, self.connection, TASKS_TABLE)

    def get_active_task_instance(self, task_type: str = TASK_TYPE_STANDARD):
        return select_with_model(TaskInstance, self.connection, f"SELECT * FROM {TASK_INSTANCES_TABLE} WHERE end_time > %s AND task_type = %s", datetime.datetime.now(), task_type)

    def get_task_instance_by_time(self, timestamp: datetime.datetime, task_type: str = TASK_TYPE_STANDARD):
        return select_with_model(TaskInstance, self.connection, f"SELECT * FROM {TASK_INSTANCES_TABLE} WHERE end_time > %s AND start_time < %s AND task_type = %s", timestamp, timestamp, task_type)

    def get_unclaimed_tasks(self):
        return select_multiple_with_model(TaskInstance, self.connection, f"SELECT * FROM {TASK_INSTANCES_TABLE} WHERE drawn_prize = false ORDER BY end_time ASC")

    def create_task_instance(self, new_task: TaskInstance):
        active_instance = self.get_active_task_instance(task_type=new_task.task_type)
        if active_instance is not None:
            active_instance.end_time = datetime.datetime.now()
            update_model(active_instance, self.connection, TASK_INSTANCES_TABLE)
        task_id = insert_model(new_task, self.connection, TASK_INSTANCES_TABLE, return_col_name="id")
        new_task.id = task_id

    def update_task_instance(self, task_instance: TaskInstance):
        update_model(task_instance, self.connection, TASK_INSTANCES_TABLE)

    def get_most_recent_task_instance(self, task_type: str = TASK_TYPE_STANDARD):
        return select_with_model(TaskInstance, self.connection, f"SELECT * FROM {TASK_INSTANCES_TABLE} WHERE task_type = %s ORDER BY end_time DESC LIMIT 1", task_type)

    def get_task_completions(self, task_instance_id: int):
        return select_multiple_with_model(TaskCompletion, self.connection, f"SELECT * FROM {TASK_COMPLETIONS_TABLE} WHERE instance_id = %s", task_instance_id)

    def add_task_completion(self, completion: TaskCompletion):
        try:
            insert_model(completion, self.connection, TASK_COMPLETIONS_TABLE)
            return True
        except psycopg2.errors.UniqueViolation:
            return False

    def remove_completions_from_message(self, evidence_message_id: str):
        completions = select_multiple_with_model(TaskCompletion, self.connection, f"SELECT * FROM {TASK_COMPLETIONS_TABLE} WHERE evidence_message_id = %s", str(evidence_message_id))
        cursor = self.connection.cursor()
        cursor.execute(f"DELETE FROM {TASK_COMPLETIONS_TABLE} WHERE evidence_message_id = %s", [str(evidence_message_id)])
        cursor.close()
        self.connection.commit()
        return completions

    def get_active_vote(self):
        return select_with_model(TaskVote, self.connection, f"SELECT * FROM {TASK_VOTING_TABLE} WHERE completed = false")

    def create_vote(self, vote: TaskVote):
        vote_id = insert_model(vote, self.connection, TASK_VOTING_TABLE, return_col_name="id")
        vote.id = vote_id

    def update_vote(self, vote: TaskVote):
        update_model(vote, self.connection, TASK_VOTING_TABLE)

    def delete_vote(self, vote: TaskVote):
        cursor = self.connection.cursor()
        cursor.execute(f"DELETE FROM {TASK_VOTING_TABLE} WHERE id = %s", [vote.id])
        cursor.close()
        self.connection.commit()

    def add_vote_option(self, option: TaskVoteOption):
        insert_model(option, self.connection, TASK_VOTING_OPTION_TABLE)

    def get_vote_options(self, task_vote_id: int):
        return select_multiple_with_model(TaskVoteOption, self.connection, f"SELECT * FROM {TASK_VOTING_OPTION_TABLE} WHERE vote_id = %s ORDER BY option_index ASC", task_vote_id)

    def get_vote_option_by_id(self, option_id: int):
        return select_with_model(TaskVoteOption, self.connection, f"SELECT * FROM {TASK_VOTING_OPTION_TABLE} WHERE id = %s", option_id)

    def initialize(self):
        cursor = self.connection.cursor()
        cursor.execute(f"""
            --DROP TABLE IF EXISTS {TASKS_TABLE} CASCADE;
            CREATE TABLE IF NOT EXISTS {TASKS_TABLE} (
                id INTEGER PRIMARY KEY,
                description VARCHAR(255) NOT NULL,
                instruction VARCHAR(255) NOT NULL,
                weight INTEGER NOT NULL
            )
        """)
        cursor.execute(f"""
            --DROP TABLE IF EXISTS {TASK_INSTANCES_TABLE} CASCADE;
            CREATE TABLE IF NOT EXISTS {TASK_INSTANCES_TABLE} (
                id SERIAL PRIMARY KEY,
                task_id INTEGER,
                task_type VARCHAR(64) NOT NULL,
                evaluated_task VARCHAR(255) NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                channel_id VARCHAR(128),
                message_id VARCHAR(128),
                drawn_prize BOOLEAN,
                FOREIGN KEY (task_id) REFERENCES {TASKS_TABLE}(id) ON DELETE SET NULL
            )
        """)
        cursor.execute(f"""
            --DROP TABLE IF EXISTS {TASK_COMPLETIONS_TABLE} CASCADE;
            CREATE TABLE IF NOT EXISTS {TASK_COMPLETIONS_TABLE} (
                id SERIAL PRIMARY KEY,
                instance_id INTEGER NOT NULL,
                user_id VARCHAR(128),
                approver_id VARCHAR(128),
                completion_time TIMESTAMP,
                evidence_channel_id VARCHAR(128),
                evidence_message_id VARCHAR(128),
                FOREIGN KEY (instance_id) REFERENCES {TASK_INSTANCES_TABLE}(id) ON DELETE CASCADE,
                UNIQUE (instance_id, user_id)
            )
        """)
        cursor.execute(f"""
            --DROP TABLE IF EXISTS {TASK_VOTING_TABLE} CASCADE;
            CREATE TABLE IF NOT EXISTS {TASK_VOTING_TABLE} (
                id SERIAL PRIMARY KEY,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                completed BOOLEAN,
                voting_channel_id VARCHAR(128),
                voting_message_id VARCHAR(128)
            )
        """)
        cursor.execute(f"""
            --DROP TABLE IF EXISTS {TASK_VOTING_OPTION_TABLE} CASCADE;
            CREATE TABLE IF NOT EXISTS {TASK_VOTING_OPTION_TABLE} (
                id SERIAL PRIMARY KEY,
                vote_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                task_id INTEGER,
                evaluated_task VARCHAR(255),
                FOREIGN KEY (vote_id) REFERENCES {TASK_VOTING_TABLE}(id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES {TASKS_TABLE}(id) ON DELETE SET NULL
            )
        """)
        cursor.close()
        self.connection.commit()

        cursor = self.connection.cursor()
        try:
            cursor.execute(f"""
                ALTER TABLE {TASK_VOTING_TABLE}
                ADD COLUMN selected_option_id INTEGER REFERENCES {TASK_VOTING_OPTION_TABLE}(id) ON DELETE SET NULL
            """)
            cursor.execute(f"""UPDATE {TASK_VOTING_TABLE} SET selected_option_id = (SELECT MIN(id) FROM {TASK_VOTING_OPTION_TABLE} WHERE vote_id = {TASK_VOTING_TABLE}.id) WHERE selected_option_id IS NULL""")
        except psycopg2.errors.DuplicateColumn:
            pass
        cursor.close()
        self.connection.commit()
