"""Tools the model can call during a run. Each skill offers a subset."""

from naruto.agent.tools import archive, group, history, media, memory, reminders, skills
from naruto.agent.tools.base import (
    RunState,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
)

ALL_TOOLS: list[Tool] = [*history.TOOLS, *archive.TOOLS, *group.TOOLS, *memory.TOOLS,
                         *reminders.TOOLS, *media.TOOLS, *skills.TOOLS]

__all__ = ["ALL_TOOLS", "RunState", "Tool", "ToolContext", "ToolError", "ToolRegistry",
           "default_registry"]


def default_registry() -> ToolRegistry:
    return ToolRegistry(ALL_TOOLS)
