# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuWenSwarm Rails for DeepAgent integration.

注意：工具权限护栏已切换为 openjiuwen 实现；此处保留同名导出以维持兼容。
"""

from importlib import import_module

_PACKAGE = "jiuwenswarm.agents.harness.common.rails"
_EXPORTS = {
    "PermissionInterruptRail": "openjiuwen.harness.rails.security",
    "AvatarPromptRail": f"{_PACKAGE}.avatar_rail",
    "BrowserTaskPromptRail": f"{_PACKAGE}.browser_task_prompt_rail",
    "ProjectMemoryRail": f"{_PACKAGE}.project_memory_rail",
    "ResponsePromptRail": f"{_PACKAGE}.response_prompt_rail",
    "RuntimePromptRail": f"{_PACKAGE}.runtime_prompt_rail",
    "SymphonyOrchestrationRail": f"{_PACKAGE}.symphony",
    "MemberSkillToolkitRail": "jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail",
    "StructuredAskUserRail": f"{_PACKAGE}.ask_user_rail",
    "MultimodalImageRail": f"{_PACKAGE}.multimodal_image_rail",
    "JiuSwarmStreamEventRail": f"{_PACKAGE}.stream_event_rail",
}

__all__ = [
    "JiuSwarmStreamEventRail",
    "MultimodalImageRail",
    "PermissionInterruptRail",
    "AvatarPromptRail",
    "BrowserTaskPromptRail",
    "ProjectMemoryRail",
    "ResponsePromptRail",
    "RuntimePromptRail",
    "SymphonyOrchestrationRail",
    "MemberSkillToolkitRail",
    "StructuredAskUserRail",
]


def __getattr__(name: str) -> object:
    """Keep storage-only workers independent of unrelated agent construction."""
    try:
        module = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
