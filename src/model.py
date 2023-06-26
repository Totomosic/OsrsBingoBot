import dataclasses
import random
import psycopg2
import psycopg2.errors
from typing import Type, Generic, TypeVar, Union
import templates

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

@dataclasses.dataclass
class TaskInstance:
    id: int
    task_id: int
    start_time: str
    end_time: str

@dataclasses.dataclass
class TaskCompletion:
    id: int
    instance_id: int
    user_id: int
    approver_id: int
    completion_time: str
    evidence_message_id: int

TASKS_TABLE = "tasks"
TASK_INSTANCES_TABLE = "task_instances"
TASK_COMPLETIONS_TABLE = "task_completions"

def select_with_model(model: Type[T], connection: "psycopg2.connection", query: str, *vars) -> Union[T, None]:
    cursor = connection.cursor()
    cursor.execute(query, vars)
    row = cursor.fetchone()
    cursor.close()
    if row is not None:
        return model(*row)
    return None

def select_multiple_with_model(model: Type[T], connection: "psycopg2.connection", query: str) -> list[T]:
    cursor = connection.cursor()
    cursor.execute(query)
    rows = cursor.fetchall()
    cursor.close()
    return [model(*row) for row in rows]

def insert_model(model: T, connection: "psycopg2.connection", table_name: str):
    cursor = connection.cursor()
    values = [f"{str(getattr(model, field))}" for field in model.__dataclass_fields__.keys()]
    try:
        cursor.execute(f"INSERT INTO \"{table_name}\" VALUES ({', '.join('%s' for _ in values)})", values)
        cursor.close()
        connection.commit()
    except psycopg2.errors.Error as e:
        connection.commit()
        raise e

def update_model(model: T, connection: "psycopg2.connection", table_name: str):
    cursor = connection.cursor()
    parameters = []
    for field in model.__dataclass_fields__.keys():
        parameters.append(field)
        parameters.append(str(getattr(model, field)))
    parameters_arr = [f"{parameters[i * 2]}=%s" for i in range(len(parameters) // 2)]
    cursor.execute(f"UPDATE {table_name} SET {', '.join(parameters_arr)} WHERE id = %s", [*[param for idx, param in enumerate(parameters) if idx % 2 == 1], model.id])
    cursor.close()
    connection.commit()

class DatabaseConnection:
    def __init__(self, dsn: str):
        self.connection = psycopg2.connect(dsn=dsn)

    def get_tasks(self):
        return select_multiple_with_model(Task, self.connection, f"SELECT * FROM {TASKS_TABLE}")

    def get_task_by_id(self, task_id: int):
        return select_with_model(Task, self.connection, f"SELECT * FROM {TASKS_TABLE} WHERE id = %s", task_id)

    def get_random_task(self):
        return random.choice(self.get_tasks())

    def get_random_tasks(self, ntasks: int):
        return random.choices(self.get_tasks(), k=ntasks)

    def insert_tasks(self, tasks: list[Task]):
        for task in tasks:
            try:
                insert_model(task, self.connection, TASKS_TABLE)
            except psycopg2.errors.UniqueViolation:
                pass

    def update_task(self, task: Task):
        update_model(task, self.connection, TASKS_TABLE)

    def initialize(self):
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"""
                --DROP TABLE IF EXISTS {TASKS_TABLE} CASCADE;
                CREATE TABLE IF NOT EXISTS {TASKS_TABLE} (
                    id INTEGER PRIMARY KEY,
                    description VARCHAR(255),
                    instruction VARCHAR(255),
                    weight INTEGER NOT NULL
                )
            """)
            cursor.execute(f"""
                --DROP TABLE IF EXISTS {TASK_INSTANCES_TABLE} CASCADE;
                CREATE TABLE IF NOT EXISTS {TASK_INSTANCES_TABLE} (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    FOREIGN KEY (task_id) REFERENCES task(id)
                )
            """)
            cursor.execute(f"""
                --DROP TABLE IF EXISTS {TASK_COMPLETIONS_TABLE} CASCADE;
                CREATE TABLE IF NOT EXISTS {TASK_COMPLETIONS_TABLE} (
                    id INTEGER PRIMARY KEY,
                    instance_id INTEGER NOT NULL,
                    user_id INTEGER,
                    approver_id INTEGER,
                    completion_time TIMESTAMP,
                    evidence_message_id INTEGER,
                    FOREIGN KEY (instance_id) REFERENCES task_instance(id)
                )
            """)
            cursor.close()
            self.connection.commit()
        except psycopg2.errors.DuplicateTable:
            pass
