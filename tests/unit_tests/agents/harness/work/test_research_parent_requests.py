# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Capture final Native requests, beyond builder-section presence.

Only Model.invoke/stream are replaced; DeepAgent initialization, bridged rails,
ReAct prompt construction and tool schemas use the locked core implementation.
No remote model call is made and these tests do not assert semantic acceptance.
"""
import copy

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperation, SysOperationCard
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import BrowserTaskPromptRail
from jiuwenswarm.agents.harness.work.research import (
    _RESEARCH_SKILLS,
    build_research_agent_config,
    work_research_instructions,
)
from jiuwenswarm.agents.harness.work.research_parent import (
    WorkResearchTaskPromptRail,
    work_research_parent_instructions,
)


def _system(request):
    return '\n'.join(message.content for message in request[0] if message.role == 'system')


@pytest.mark.asyncio
@pytest.mark.parametrize('assembly', ['work', 'code', 'explicit_empty_rails'])
async def test_final_native_requests_preserve_work_policy_and_child_contract(tmp_path, monkeypatch, assembly):
    captured = []
    callback_agents = []

    async def invoke(_model, messages, **kwargs):
        captured.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        return AssistantMessage(content='Captured request only.')

    async def stream(_model, messages, **kwargs):
        captured.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        yield AssistantMessageChunk(content='Captured streamed request only.')

    class CallbackProbe(AgentRail):
        async def before_model_call(self, ctx):
            callback_agents.append(ctx.agent)

    monkeypatch.setattr(Model, 'invoke', invoke)
    monkeypatch.setattr(Model, 'stream', stream)
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider='OpenAI', api_base='https://example.invalid/v1', api_key='unused',
        ),
        model_config=ModelRequestConfig(model='request-capture', temperature=0),
    )
    operation = SysOperation(SysOperationCard(
        id=f'parent-request-{assembly}', mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(
            sandbox_root=[str(tmp_path), str(_RESEARCH_SKILLS)], restrict_to_sandbox=True,
        ),
    ))
    spec = build_research_agent_config(
        model, workspace=str(tmp_path), sys_operation=operation, language='en',
    )
    spec.enable_read_image_multimodal = False
    rail = (WorkResearchTaskPromptRail(enable_subagent_runtime=True) if assembly == 'work'
            else BrowserTaskPromptRail(enable_subagent_runtime=True))
    parent = create_deep_agent(
        model=model, workspace=str(tmp_path), sys_operation=operation,
        language='en', subagents=[spec], enable_subagent_runtime=True,
        enable_task_loop=False, enable_read_image_multimodal=False,
        system_prompt='Custom parent identity.',
        rails=[] if assembly == 'explicit_empty_rails' else [rail, CallbackProbe()],
    )
    child = None
    await Runner.start()
    try:
        await parent.invoke({'query': 'First request', 'conversation_id': 'parent-capture'})
        async for _ in parent.stream({'query': 'Next request', 'conversation_id': 'parent-capture'}):
            pass
        assert len(captured) == 2
        for request in captured:
            system = _system(request)
            assert 'Custom parent identity.' in system
            assert (work_research_parent_instructions() in system) is (assembly == 'work')
        if assembly != 'explicit_empty_rails':
            assert callback_agents == [parent._react_agent, parent._react_agent]
            assert not hasattr(callback_agents[0], 'deep_config')

        if assembly == 'work':
            # Live owner removal must remove policy on the next actual request.
            parent.deep_config.subagents = []
            await parent.invoke({'query': 'After unload', 'conversation_id': 'parent-capture'})
            assert work_research_parent_instructions() not in _system(captured[-1])
            parent.deep_config.subagents = [spec]
            child = parent.create_subagent('research_agent', 'captured-child')
            await child.invoke({'query': 'Inspect configured instructions only', 'conversation_id': 'child-capture'})
            child_request = captured[-1]
            assert work_research_instructions() in _system(child_request)
            schemas = {tool.name: tool.parameters for tool in child_request[1]['tools']}
            review = schemas['review_research_report']
            assert {'sources', 'claims', 'question'} <= set(review['properties'])
            assert {'sources', 'claims'} <= set(review['required'])
            assert {'read_file', 'write_file'} <= set(schemas)
    finally:
        if child is not None:
            await child.stop()
        await parent.stop()
        await Runner.stop()
