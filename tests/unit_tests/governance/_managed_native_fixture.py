"""Real Native task-loop/ownership; only model, Session IO and event bus are synthetic."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from openjiuwen.core.controller.config import ControllerConfig
from openjiuwen.core.controller.modules.task_manager import TaskManager
from openjiuwen.core.controller.modules.task_scheduler import TaskScheduler
from openjiuwen.core.controller.schema.event import EventType
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.task_loop.loop_coordinator import LoopCoordinator
from openjiuwen.harness.task_loop.loop_queues import LoopQueues
from openjiuwen.harness.task_loop.task_loop_controller import TaskLoopController
from openjiuwen.harness.task_loop.task_loop_event_executor import DEEP_TASK_TYPE, build_deep_executor
from openjiuwen.harness.task_loop.task_loop_event_handler import TaskLoopEventHandler
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session.session_manager import SessionManager


def ownership(child, sid):
    root = object.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    root._session_adapters = {sid: child}
    root._session_adapter_locks = {}
    root._active_session_ids = {}
    root._session_agent_tasks = {}
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = root
    facade._session_manager = SessionManager()
    manager = AgentManager()
    manager.agents['web'] = {'agent': facade}
    assert manager.get_agent_for_session_nowait('web', sid) is facade
    return SimpleNamespace(root=root, facade=facade, manager=manager)


def model_free_agent(react, session, monkeypatch):
    agent = DeepAgent(AgentCard(name='governance-fixture', description='synthetic reactor'))
    agent.configure(DeepAgentConfig(enable_task_loop=True))
    react.register_callback = AsyncMock()
    react.agent_callback_manager.unregister_rail = AsyncMock()
    agent.set_react_agent(react, initialized=True)
    # Session storage/stream are in-memory, but real forwarding consumes the real
    # ordered boundary marker. No forged EOF or terminal/exit receipt.
    state, output = {}, asyncio.Queue()
    session.get_state = lambda key=None: dict(state) if key is None else state.get(key)
    session.update_state = state.update
    session.pre_run = AsyncMock()
    session.post_run = AsyncMock()
    session.write_stream = AsyncMock(side_effect=output.put)
    session.close_stream = AsyncMock(side_effect=lambda: output.put(None))
    session._inner = SimpleNamespace(stream_writer_manager=lambda: SimpleNamespace(
        stream_emitter=lambda: SimpleNamespace(emit=output.put)))
    async def iterator():
        while (item := await output.get()) is not None:
            yield item
    session.stream_iterator = iterator
    agent._loop_coordinator = LoopCoordinator()
    agent._loop_coordinator.reset()
    handler = TaskLoopEventHandler(agent)
    handler.interaction_queues = LoopQueues()
    config = ControllerConfig(schedule_interval=60)
    manager = TaskManager(config)
    loop = TaskLoopController()
    loop._card = agent.card
    loop._event_handler = handler
    loop._task_manager = manager
    agent._loop_controller = loop
    handler.task_manager = manager
    async def publish(_id, actual_session, event):
        inputs = SimpleNamespace(event=event, session=actual_session)
        method = {EventType.INPUT: handler.handle_input,
                  EventType.TASK_COMPLETION: handler.handle_task_completion,
                  EventType.TASK_FAILED: handler.handle_task_failed}[event.event_type]
        return await method(inputs)
    queue = SimpleNamespace(publish_event=publish, stop=AsyncMock(), unsubscribe=AsyncMock())
    loop._event_queue = queue
    scheduler = TaskScheduler(config, manager, Mock(), Mock(), queue, agent.card)
    manager.set_on_task_submitted(scheduler._submit_event.set)
    scheduler._task_executor_registry.add_task_executor(DEEP_TASK_TYPE, build_deep_executor(agent))
    scheduler._ensure_session_completion_signal = AsyncMock()
    loop._task_scheduler = scheduler
    handler.task_scheduler = scheduler
    async def prepare(actual_session):
        scheduler._sessions[actual_session.get_session_id()] = actual_session
        await scheduler.start()
        return agent.loop_coordinator, loop
    monkeypatch.setattr(agent, 'prepare_interaction_task_loop', prepare)
    monkeypatch.setattr(agent, '_write_round_result_to_stream', AsyncMock())
    monkeypatch.setattr(agent, '_has_remaining_tasks', lambda *_: False)
    return agent
