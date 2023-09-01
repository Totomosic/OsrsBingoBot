import dataclasses
import random
import re
from typing import Union

class TemplateFormatException(Exception):
    pass

@dataclasses.dataclass
class RandomComponent:
    min: int
    max: int
    rounding: int = 1

    def evaluate(self) -> int:
        value = random.randint(self.min, self.max)
        return int(round(value / float(self.rounding)) * self.rounding)

class ParsedTemplate:
    def __init__(self, template: str):
        self.template = template
        self.parts: list[Union[str, None]] = []
        self.random_components: list[RandomComponent] = []
        self._parse_template(template)

    def get_template(self) -> str:
        return self.template

    def evaluate(self) -> str:
        result = ""
        component_index = 0
        for part in self.parts:
            if part is None:
                component = self.random_components[component_index]
                result += str(component.evaluate())
                component_index += 1
            else:
                result += part
        return result

    def _parse_template(self, template: str):
        self.parts = []
        self.random_components = []
        detection_pattern = re.compile(r"\{[^\}]*\}")
        parsing_pattern = re.compile(r"\{\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*(\d+)\s*)?\s*\}")
        current_index = 0
        match_result = detection_pattern.search(template, current_index)
        while match_result is not None:
            match_index = match_result.start()
            if match_index > current_index:
                self.parts.append(template[current_index : match_index])
            parsed_result = parsing_pattern.match(template[match_index : match_result.end()])
            if not parsed_result:
                raise TemplateFormatException(f"Invalid template: {template}")
            parsed_groups = parsed_result.groups()
            self.parts.append(None)
            self.random_components.append(RandomComponent(
                min=int(parsed_groups[0]),
                max=int(parsed_groups[1]),
                rounding=int(parsed_groups[2] or 1),
            ))
            current_index = match_result.end()
            match_result = detection_pattern.search(template, current_index)
        if current_index < len(template):
            self.parts.append(template[current_index:])
