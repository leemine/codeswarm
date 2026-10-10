import asyncio
from types import SimpleNamespace
import pytest
from openjiuwen.harness_protocol import DeliveryMode,SendReceipt,TurnEventKind
from openjiuwen.harness_providers.io_adapter import ProjectedOutput
from jiuwenswarm.runtime.harness.output_router import TurnOutputRouter,TurnOutputIncompleteError
from jiuwenswarm.runtime.harness.execution_session import ExecutionSession,ExecutionExitState

@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [TurnEventKind.ABORTED, None])
async def test_provider_stop_terminal_reaches_original_reader(terminal):
    queue=asyncio.Queue();started=asyncio.Event()
    class IO:
        async def output_envelopes(self):
            while (item:=await queue.get()) is not None:yield item
        async def stop(self):
            if terminal is not None:
                await queue.put(ProjectedOutput('turn', terminal=terminal))
            await queue.put(None)
    io=IO();router=TurnOutputRouter(io);router.start()
    async def send():return SendReceipt(message_id='m',turn_id='turn',accepted_mode=DeliveryMode.FOLLOW_UP)
    await router.submit(send)
    async def consume():
        started.set()
        try:return [item.terminal async for item in router.outputs('turn')]
        except TurnOutputIncompleteError:return ['unknown']
    reader=asyncio.create_task(consume());await started.wait()
    session=SimpleNamespace(io=io,_tool_transport=None,_tool_gateway=None,_exit_state=ExecutionExitState.RUNNING)
    try:
        await ExecutionSession._stop_owned_resources(session,router=router,drain_output=True)
        assert await reader == ([terminal] if terminal else ['unknown'])
    finally:
        await router.stop()
        if not reader.done():reader.cancel()
        await asyncio.gather(reader,return_exceptions=True)
