# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Successful execution, actual reads and artifacts are separate from truth."""
import pytest

from tests.system_tests.test_work_research_remote import (
    _ResearchTrace, _RUN_BUDGET_S, _CLEANUP_BUDGET_S, _check_execution_delivery,
    _native_research_parent, _native_research_work_config, _write_sources,
)


@pytest.fixture
def delivery(tmp_path):
    root = tmp_path / 'workspace'
    _write_sources(root)
    (root / 'research-report.md').write_text('Candidate source-a.md:3; source-b.md:3-4.')
    trace = _ResearchTrace('native', root)
    trace.parent_terminal = 'completed'
    trace.parent_final_text = 'Saved research-report.md.'
    trace.spawned_child_ids = {'parent_sub_research'}
    trace.child_providers = {'parent_sub_research': 'native'}
    trace.child_statuses = {'parent_sub_research': 'completed'}
    for name in ('source-a.md', 'source-b.md'):
        trace.observe_read('parent_sub_research', 'read_file', {'file_path': name}, {'content': (root / name).read_text()})
    return trace


def test_light_delivery_needs_no_review_tool_extra_artifacts_or_parent_rereads(delivery):
    _check_execution_delivery(delivery)
    assert delivery.outcome == 'passed'
    assert _RUN_BUDGET_S == 360 and _CLEANUP_BUDGET_S == 30
    assert not hasattr(delivery, 'enable_acceptance_budget')


@pytest.mark.parametrize('defect', ['parent_failed', 'parent_eof', 'child_failed', 'child_eof',
                                   'no_child', 'different_provider', 'unidentified_child', 'parent_only_reads', 'no_path'])
def test_report_file_cannot_hide_execution_or_read_failure(delivery, defect):
    if defect == 'parent_failed':
        delivery.parent_terminal = 'failed'
    elif defect == 'parent_eof':
        delivery.parent_terminal = None
    elif defect == 'child_failed':
        delivery.child_statuses['parent_sub_research'] = 'failed'
    elif defect == 'child_eof':
        delivery.child_statuses.clear()
    elif defect == 'no_child':
        delivery.spawned_child_ids.clear()
    elif defect == 'different_provider':
        delivery.child_providers['parent_sub_research'] = 'codex'
    elif defect == 'unidentified_child':
        delivery.child_providers.clear()
    elif defect == 'parent_only_reads':
        for item in delivery.source_reads:
            item['scope'] = 'parent'
    elif defect == 'no_path':
        delivery.parent_final_text = 'done'
    with pytest.raises(AssertionError):
        _check_execution_delivery(delivery)


@pytest.mark.asyncio
async def test_native_canary_uses_product_builders_and_real_stream_without_remote_model(tmp_path):
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.rail.base import AgentRail
    from openjiuwen.core.sys_operation import SysOperationCard, OperationMode
    from openjiuwen.core.sys_operation.cwd import init_cwd
    seen = []
    class BeforeModel(AgentRail):
        async def before_model_call(self, ctx):
            seen.extend(tool.name for tool in ctx.inputs.tools or [])
            ctx.request_force_finish({'result_type': 'answer', 'output': 'fixture: no model called'})
    card = SysOperationCard(id='research-light-tools', mode=OperationMode.LOCAL,
                           work_config=_native_research_work_config(tmp_path))
    parent = None
    await Runner.start()
    try:
        Runner.resource_mgr.add_sys_operation(card)
        operation = Runner.resource_mgr.get_sys_operation(card.id)
        init_cwd(str(tmp_path), workspace=str(tmp_path), project_root=str(tmp_path))
        model = Model(model_client_config=ModelClientConfig(client_provider="OpenAI", api_base="https://example.invalid/v1", api_key="unused"),
                      model_config=ModelRequestConfig(model="fixture"))
        parent = _native_research_parent(model, tmp_path, operation, BeforeModel(), BeforeModel())
        outputs = [chunk async for chunk in Runner.run_agent_streaming(parent, {'query': 'Inspect available tools'},
                                                                     session='r1-light-builder-fixture')]
        assert {'subagent_spawn', 'subagent_wait', 'read_file'} <= set(seen)
        assert 'review_research_report' not in seen
        assert outputs
        assert any(spec.agent_card.name == 'research_agent' for spec in parent.deep_config.subagents)
    finally:
        if parent is not None:
            await parent.stop()
        Runner.resource_mgr.remove_sys_operation(sys_operation_id=card.id)
        await Runner.stop()


def test_source_mutation_after_a_real_read_still_rejects_delivery(delivery):
    (delivery.root / 'source-a.md').write_text('mutated after read')
    with pytest.raises(AssertionError, match='original source was changed'):
        _check_execution_delivery(delivery)
