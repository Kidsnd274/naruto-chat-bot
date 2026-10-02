"""Skills: focused instructions + a tool subset + reasoning on or off.

The instructions and the reasoning switch are settings
(``skills.<name>.instructions`` / ``skills.<name>.reasoning``); the tool
subset is fixed here so each request offers only what the skill needs.
Commands pick a skill directly; for a free-form mention the default skill
(banter) can hand over to a focused one with the use_skill tool.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    name: str
    label: str
    tools: tuple[str, ...]
    description: str = ""  # for use_skill


READ = ("search_chat", "get_messages_around")

SKILLS: dict[str, Skill] = {skill.name: skill for skill in (
    Skill("banter", "Banter", READ + (
        "search_history_summaries", "search_memory", "remember", "forget", "describe_image",
        "set_reminder", "cancel_reminder", "create_poll", "pin_message", "unpin_message",
        "update_board", "use_skill")),
    Skill("summarize", "Summarize", READ + (
        "get_earlier_messages", "search_history_summaries", "search_memory", "describe_image"),
        "summarize a discussion or what was talked about (optionally since a time)"),
    Skill("catchup", "Catch-up", READ + ("get_earlier_messages", "search_memory")),
    Skill("plan", "Plan", READ + (
        "get_earlier_messages", "propose_plan", "update_board", "create_poll"),
        "work out the plan being discussed and propose it with Confirm buttons"),
    Skill("questions", "Open questions", READ + ("get_earlier_messages", "update_board"),
          "list the group's open questions and keep them on the board"),
    Skill("decide", "Decide", READ + ("propose_plan", "create_poll", "update_board",
                                      "pin_message"),
          "confirm a settled plan or take a vote"),
    Skill("remind", "Reminders", ("set_reminder", "cancel_reminder")),
    Skill("remember", "Memory", ("remember", "forget", "search_memory")),
)}
DEFAULT_SKILL = "banter"
# Skills banter can hand over to.
ROUTABLE = ("summarize", "plan", "questions", "decide")


def get_skill(name: str) -> Skill:
    return SKILLS.get(name) or SKILLS[DEFAULT_SKILL]
