# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lightweight research preserves real Native request and override contracts.

Only model transport is replaced. Core initialization, child creation, bridged
rails and final request schemas remain real; no semantic model quality is claimed.
"""
import copy

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperation, SysOperationCard
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import BrowserTaskPromptRail
from jiuwenswarm.agents.harness.work.research import (
    _RESEARCH_SKILLS,
    build_research_agent_config,
    work_research_instructions,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _system(request):
    return '\n'.join(message.content for message in request[0] if message.role == 'system')


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', [False, True])
@pytest.mark.parametrize('case', ['default', 'research_disabled', 'custom_tools', 'custom_rails', 'custom_system'])
async def test_native_requests_keep_light_research_and_existing_tools(tmp_path, monkeypatch, runtime, case):
    requests = []

    async def invoke(_model, messages, **kwargs):
        requests.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        return AssistantMessage(content='Synthetic inline result.')

    async def stream(_model, messages, **kwargs):
        requests.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        yield AssistantMessageChunk(content='Synthetic inline result.')

    monkeypatch.setattr(Model, 'invoke', invoke)
    monkeypatch.setattr(Model, 'stream', stream)
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider='OpenAI', api_base='https://example.invalid/v1', api_key='unused',
        ),
        model_config=ModelRequestConfig(model='compatibility-capture'),
    )
    operation = SysOperation(SysOperationCard(
        id=f'research-compatibility-{case}-{runtime}', mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(
            sandbox_root=[str(tmp_path), str(_RESEARCH_SKILLS)], restrict_to_sandbox=True,
        ),
    ))
    overrides = {
        'custom_tools': {'tools': []},
        'custom_rails': {'rails': []},
        'custom_system': {'system_prompt': 'Custom child identity.'},
    }.get(case, {})
    spec = build_research_agent_config(
        model, workspace=str(tmp_path), sys_operation=operation, language='en', **overrides,
    )
    spec.enable_read_image_multimodal = False
    rail = JiuWenSwarmDeepAdapter()._build_subagent_rail(
        {'react': {'subagent_runtime': {'enabled': runtime}}},
    )
    assert type(rail) is BrowserTaskPromptRail
    assert rail.enable_subagent_runtime is runtime
    assert rail.synchronous_subagent_types == frozenset({'browser_agent'})
    parent = create_deep_agent(
        model=model, workspace=str(tmp_path), sys_operation=operation, language='en',
        subagents=[] if case == 'research_disabled' else [spec], rails=[rail],
        enable_subagent_runtime=runtime, enable_task_loop=False, enable_read_image_multimodal=False,
        system_prompt='Custom parent identity.',
    )
    child = None
    await Runner.start()
    try:
        # Configuring a research capability does not add an audit workflow to
        # this ordinary Work turn, in either historical or persistent mode.
        await parent.invoke({'query': 'What is 2+2?', 'conversation_id': 'compatibility-parent'})
        async for _ in parent.stream({'query': 'Next ordinary turn', 'conversation_id': 'compatibility-parent'}):
            pass
        assert len(requests) == 2
        for request in requests:
            system = _system(request)
            assert 'Custom parent identity.' in system
            assert 'Work research parent acceptance' not in system
            assert 'work_research_parent_review' not in system
            assert 'review_research_report' not in system
            assert work_research_instructions() not in system
            names = {tool.name for tool in request[1].get('tools') or []}
            if case == 'research_disabled':
                assert not names
            elif runtime:
                assert {'subagent_spawn', 'subagent_wait', 'subagent_send_input'} <= names
                assert 'task_tool' not in names
            else:
                assert names == {'task_tool'}
                assert 'subagent_send_input' not in system

        if case != 'research_disabled':
            child = parent.create_subagent('research_agent', 'compatibility-child')
            await child.invoke({
                'query': 'Read-only research: answer inline; do not create output files.',
                'conversation_id': 'compatibility-child',
            })
            request = requests[-1]
            system = _system(request)
            names = {tool.name for tool in request[1].get('tools') or []}
            assert 'review_research_report' not in names
            assert 'Evidence-to-report procedure' not in system
            if case == 'custom_system':
                assert 'Custom child identity.' in system
                assert work_research_instructions() not in system
            else:
                assert work_research_instructions() in system
                assert 'Read-only research can finish with an inline cited answer.' in ' '.join(system.split())
            if case == 'custom_rails':
                assert not names
                assert spec.rails is overrides['rails']
            else:
                assert {'read_file', 'write_file'} <= names
            if case == 'custom_tools':
                assert spec.tools == overrides['tools']
            assert child.deep_config.sys_operation is operation
    finally:
        if child is not None:
            await child.stop()
        await parent.stop()
        await Runner.stop()
