from reviewer.agents.base import TemplateAgent


class Agent(TemplateAgent):
    def __init__(self):
        super().__init__("correctness", "strong", "file_group")
