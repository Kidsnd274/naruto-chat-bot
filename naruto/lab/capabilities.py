"""What the lab can do in this version of the bot: tunable settings with
their ranges and current values, skills and tools (and how each runs in a
sandbox), commands, checks, the scenario format, limits, and what isn't
simulated. An agent reads this first instead of the source code."""

from naruto.agent.skills import DEFAULT_SKILL, ROUTABLE, SKILLS
from naruto.agent.tools import default_registry
from naruto.lab import checks, config, scenario
from naruto.lab.sandbox import DEFERRED, NOT_SIMULATED
from naruto.lab.service import MAX_BATCH, MAX_REPEAT
from naruto.settings.registry import REGISTRY

# How each tool behaves in a sandbox.
SIMULATED = {"create_poll", "pin_message", "unpin_message", "update_board", "propose_plan"}
NEEDS_IMAGE = {"describe_image"}

COMMANDS = [
    {"command": "/summary", "skill": "summarize",
     "args": "nothing, today, yesterday, week, 3h, 2 days or a topic; or reply to a message"},
    {"command": "/plan", "skill": "plan", "args": "optional details"},
    {"command": "/questions", "skill": "questions", "args": ""},
    {"command": "/remember", "skill": "remember", "args": "the fact, or reply to a message"},
    {"command": "/remind", "skill": "remind", "args": "when and what"},
    {"command": "/catchup", "skill": "catchup",
     "args": "none; ephemeral: the answer is recorded, not posted to the group"},
]

OUTCOMES = {
    checks.PASS: "every deterministic check passed",
    checks.FAIL: "a check failed: a wrong answer, a wrong tool, the wrong skill",
    checks.ACTION_FAILED: "a tool the turn needed was called but failed",
    checks.ERROR: "the model server, a time-out, the queue or a crash: not the model's judgment",
    checks.SKIPPED: "not run or not checkable: an unsupported feature, or the budget",
    checks.CANCELLED: "stopped before it finished",
    checks.UNJUDGED: "ran, but has no deterministic checks (judge it)",
}

EXAMPLE = {
    "id": "banter-teasing-1",
    "origin": "synthetic",
    "category": "character",
    "description": "Wei teases Naruto about losing at bowling.",
    "time": "2026-10-04T18:05:00+08:00",
    "timezone": "Asia/Singapore",
    "chat": {"title": "BBQ crew", "type": "group"},
    "members": [{"id": 7, "name": "Alice", "username": "alice"},
                {"id": 9, "name": "Wei", "aliases": ["Always Late"]}],
    "state": {"notes": [{"text": "Wei is always late", "about": 9}],
              "board": {"plans": ["BBQ Sat 6pm"]}},
    "messages": [{"from": "Wei", "from_id": 9, "text": "lol you bowled a 40 last night"}],
    "turns": [
        {"from": "Wei", "from_id": 9, "text": "@naruto_bot admit it, I'm better",
         "expect": {"max_chars": 300, "tool_calls": [],
                    "judge": {"good": "A cheeky comeback that stays friendly.",
                              "bad": "Sulking, or a lecture."}}},
        {"after": "2m", "from": "Alice", "from_id": 7, "reply_to_answer": True,
         "text": "ok but seriously, rematch saturday?",
         "expect": {"forbidden_tools": ["update_board"]}},
    ],
}


def _tool_support(name: str) -> str:
    if name in SIMULATED:
        return ("simulated Telegram action: recorded, and its result follows the sandbox; make "
                "it fail with the scenario's \"simulate\"")
    if name in NEEDS_IMAGE:
        return "needs the image in the scenario (\"image\" on the message); skipped otherwise"
    return "runs on the sandbox's data"


def capabilities(lab) -> dict:
    services = lab.services
    settings = services.settings
    registry = default_registry()
    tunable = set(config.tunable_keys())
    overriding = lab.overriding_chats(list(REGISTRY))
    setting_rows = []
    for key in REGISTRY:
        if key not in tunable and not config.is_extended(key):
            continue
        setting = REGISTRY[key]
        setting_rows.append({
            "key": key, "label": setting.label, "section": setting.section,
            "description": setting.description, "type": setting.type,
            "min": setting.min, "max": setting.max, "choices": list(setting.choices),
            "nullable": setting.nullable, "default": setting.default,
            "current": settings.get(key),
            "tunable": "always" if key in tunable else "when the run's scope names it",
            "file": config.file_name(key),
            "per_chat_overrides": overriding.get(key, []),
            "also_used_by_background_tasks": key in config.SHARED_MODEL_KEYS,
        })
    servers = settings["lab.model_servers"] or {}
    return {
        "api_version": 1,
        "code_fingerprint": config.code_fingerprint(),
        "schema_version": services.db.schema_version,
        "model": {
            "endpoint": config.redact_endpoint(settings["model.endpoint_url"]),
            "name": settings["model.name"] or "(the first model the server lists)",
            "other_servers": sorted(servers),
            "server_lists": services.status.llm_models,
            "note": "A run tests one model. Changing the model is a new run, not a candidate.",
        },
        "settings": setting_rows,
        "skills": [{"name": skill.name, "label": skill.label, "tools": list(skill.tools),
                    "description": skill.description,
                    "instructions_setting": f"skills.{skill.name}.instructions",
                    "reasoning_setting": f"skills.{skill.name}.reasoning"}
                   for skill in SKILLS.values()],
        "routing": {"default_skill": DEFAULT_SKILL, "hand_over_to": list(ROUTABLE),
                    "how": "Mentions start in banter, which may hand over with use_skill. "
                           "Commands pick their skill directly. A scenario's \"skill\" forces "
                           "one and makes it a focused experiment."},
        "commands": COMMANDS,
        "tools": [{"name": tool.name, "description": tool.description,
                   "sandbox": _tool_support(tool.name)}
                  for tool in registry.tools.values()],
        "checks": {"expect": sorted(scenario.EXPECT_KEYS),
                   "state": list(scenario.STATE_CHECK_KEYS)},
        "outcomes": OUTCOMES,
        "scenario": {
            "fields": sorted(scenario.SCENARIO_KEYS), "turn_fields": sorted(scenario.TURN_KEYS),
            "categories": list(scenario.CATEGORIES), "origins": list(scenario.ORIGINS),
            "state": list(scenario.STATE_KEYS), "media": list(scenario.MEDIA_KINDS),
            "simulate": {"methods": list(scenario.SIMULATED_METHODS),
                         "failures": list(scenario.SIMULATED_FAILURES)},
            "requires": ["vision", *sorted(registry.tools)],
            "images": "inline, as data: URIs (data:image/png;base64,...)",
            "example": EXAMPLE,
        },
        "limits": {"max_attempts_per_run": settings["lab.max_attempts_per_run"],
                   "default_budget": settings["lab.default_budget"],
                   "max_batch": MAX_BATCH, "max_repeat": MAX_REPEAT,
                   "parallel_attempts": settings["lab.parallel_attempts"],
                   "cost": "not applicable: the model is local. The agent's own usage isn't "
                           "measured."},
        "measurements": {
            "available": ["model time per request (latency_ms)", "queue wait per attempt",
                          "tokens when the server reports usage", "model requests and tool "
                                                                  "calls per turn"],
            "unavailable": ["time to first token (replies aren't streamed, as in production)",
                            "hidden reasoning the server doesn't return"],
        },
        "not_simulated": NOT_SIMULATED,
        "deferred": DEFERRED,
    }
