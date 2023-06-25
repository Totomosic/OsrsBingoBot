import dataclasses
import psycopg2

@dataclasses.dataclass
class Task:
    id: int
    description: str
    instruction: str
    weight: int

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

def create_database_if_not_exist():
    pass

class DatabaseConnection:
    def __init__(self, dsn: str):
        self.connection = psycopg2.connect(dsn=dsn)
