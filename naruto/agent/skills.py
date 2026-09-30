"""Skills: focused instructions + a tool subset + reasoning on or off.

The instructions and the reasoning switch are settings
(``skills.<name>.instructions`` / ``skills.<name>.reasoning``); the tool
subset is fixed here so each request offers only what the skill needs.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    name: str
    label: str
    tools: tuple[str, ...]


READ_TOOLS = ("search_chat", "get_messages_around", "get_earlier_messages")
GROUP_TOOLS = ("update_board", "pin_message", "unpin_message", "propose_plan", "create_poll")

SKILLS: dict[str, Skill] = {
    "banter": Skill("banter", "Banter", READ_TOOLS + GROUP_TOOLS),
}
DEFAULT_SKILL = "banter"


def get_skill(name: str) -> Skill:
    return SKILLS.get(name) or SKILLS[DEFAULT_SKILL]
