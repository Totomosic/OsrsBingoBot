import dataclasses
import json
import random
from typing import Union, Callable, Any
import templates

def dataclass_from_dict(cls, values: dict, field_mappings: dict[str, Callable[[Any], Any]] = {}, exclude: list[str] = []):
    datacls = cls(**{
        k: (field_mappings[k](v) if k in field_mappings else v) for k, v in values.items() if k not in exclude
    })
    return datacls

class TaskValidationException(Exception):
    pass

@dataclasses.dataclass
class OsrsTask:
    id: int
    weight: int
    task: templates.ParsedTemplate

    def to_dict(self):
        return {
            "id": self.id,
            "weight": self.weight,
            "task": self.task.get_template(),
        }

    @classmethod
    def from_dict(cls, values: dict):
        return dataclass_from_dict(cls, values, {
            "task": templates.ParsedTemplate,
        })

class TaskDatabase:
    def __init__(self):
        self.tasks: list[OsrsTask] = []
        self.max_task_id = 0

    def get_tasks(self) -> list[OsrsTask]:
        return self.tasks

    def get_task_by_id(self, task_id: int) -> Union[OsrsTask, None]:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_next_task_id(self):
        self.max_task_id += 1
        return self.max_task_id

    def add_task(self, task: OsrsTask):
        existing_task = self.get_task_by_id(task.id)
        if existing_task:
            raise TaskValidationException(f"A task with ID {task.id} already exists")
        if task.id > self.max_task_id:
            self.max_task_id = task.id
        self.tasks.append(task)

    def load_task_file(self, filename: str):
        with open(filename, "r") as f:
            tasks_data = json.load(f)
        for task in tasks_data["tasks"]:
            self.add_task(OsrsTask.from_dict(task))

    def save(self, filename: str):
        print(f"Saving tasks to {filename}")
        with open(filename, "w") as f:
            tasks_data = {
                "tasks": [task.to_dict() for task in self.get_tasks()],
            }
            json.dump(tasks_data, f)

    def get_random_task(self) -> OsrsTask:
        if len(self.tasks) == 0:
            raise TaskValidationException(f"There are no tasks in the database")
        return random.choice(self.get_tasks())
