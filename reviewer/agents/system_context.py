from reviewer.agents.base import TemplateAgent


class Agent(TemplateAgent):
    def __init__(self):
        super().__init__("system_context", "strong", "whole_change")
