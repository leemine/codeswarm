"""Opt-in controls through independent original Gateway and AgentServer.

Real CLI and remote model; ordinary product Session and Team admission.
No Runtime owner, model, scheduler, or interaction registry is replaced.
"""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
import pytest
import websockets

pytestmark=[pytest.mark.integration,pytest.mark.system,pytest.mark.skipif(
    os.environ.get('RUN_TEAM_PRODUCT_REMOTE')!='1',reason='real Team product controls are opt-in')]

@pytest.mark.asyncio
@pytest.mark.timeout(360)
@pytest.mark.parametrize('provider',['codex','opencode'])
@pytest.mark.parametrize('control',['deny','cancel','disconnect','kill'])
async def test_team_product_pending_control(tmp_path,monkeypatch,provider,control):
    from .test_team_product_remote import configure_team_product
    from .test_heartbeat_channels_remote import web_services,rpc
    from .test_external_codex_web_channel_local import _send_request,_receive_until
    from .goal_browser_remote_support import cleanup_audit
    from openjiuwen.agent_teams.external.interaction_address import decode_interaction_address
    root=Path(os.environ.get('TEAM_PRODUCT_EVIDENCE_DIR',str(tmp_path)));root.mkdir(parents=True,exist_ok=True)
    scope=Path(tempfile.mkdtemp(prefix=f'{provider}-{control}-',dir=root))
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR',str(scope/'data'))
    monkeypatch.setenv('JIUWENSWARM_CONFIG_DIR',str(scope/'data/config'))
    configure_team_product(monkeypatch,shutil.which('google-chrome'),approvals=True)
    evidence={'provider':provider,'control':control,'frames':[],'checks':[]}
    ws=None
    restarted=None
    launches=[]
    from . import test_heartbeat_channels_remote as support
    original_start=support._start_service
    def track_service(command, **kwargs):
        process=original_start(command, **kwargs)
        launches.append((process,command,kwargs))
        return process
    monkeypatch.setattr(support,'_start_service',track_service)
    try:
        async with web_services(scope,provider) as (url,data,profile):
            ws=await websockets.connect(url,max_size=8*1024*1024)
            created=await rpc(ws,'session.create',{'mode':'team.code.normal','work_mode':'code',
                'persist_session':True,'execution_profile_id':profile},'create')
            sid=created['session_id'];evidence['session_id']=sid
            prompt=(f'请用 exec_command/bash 工具执行 printf APPROVAL-CHECK > {scope}/approval-marker.txt。'
                    '直接提交该命令，保持默认权限参数，让原工具权限机制发起审批。不要调用 build_team 或其它工具。'
                    '工具审批由用户处理；若被拒绝不要重试、不要改用别的工具，只报告被拒绝。')
            await _send_request(ws,'start','chat.send',{'session_id':sid,'mode':'team.code.normal',
                'work_mode':'code','query':prompt})
            async with asyncio.timeout(140):
                while True:
                    frame=json.loads(await ws.recv());evidence['frames'].append(frame)
                    assert frame.get('event') != 'team.member_turn', 'Member finished without issuing the requested approval'
                    if frame.get('event')=='chat.ask_user_question':
                        question=frame;frames=[];break
            evidence['frames']+=frames
            q=question['payload'];address=decode_interaction_address(q['request_id'])
            assert address and address[2]==sid and q['session_generation']>0
            evidence['checks'].append('original scoped pending control')
            answer={'session_id':sid,'mode':'team.code.normal','work_mode':'code','query':'',
                'request_id':q['request_id'],'source':q['source'],'session_generation':q['session_generation'],
                'answers':[{'selected_options':['reject' if control=='deny' else 'allow_once']}]}
            if control=='deny':
                # A stale generation must fail without consuming the live question.
                await _send_request(ws,'stale','chat.send',{**answer,'session_generation':q['session_generation']+1})
                rejected,frames=await _receive_until(ws,lambda f:f.get('event')=='chat.error' or
                    (f.get('id')=='stale' and f.get('ok') is False),timeout=30)
                evidence['frames']+=frames
                evidence['checks'].append('stale generation rejected before valid answer')
                await _send_request(ws,'deny','chat.send',answer)
                accepted,frames=await _receive_until(ws,lambda f:f.get('event')=='runtime.accepted' and
                    f.get('payload',{}).get('interaction_id')==q['request_id'],timeout=45)
                evidence['frames']+=frames
                assert accepted['payload'].get('resolved')
                evidence['checks'].append('exact Runtime denial acknowledgement')
                await _send_request(ws,'deny-retry','chat.send',answer)
                replay,frames=await _receive_until(ws,lambda f:f.get('event')=='runtime.accepted' and
                    f.get('payload',{}).get('duplicate') is True,timeout=30)
                evidence['frames']+=frames
                evidence['checks'].append('duplicate decision acknowledged without replay')
            if control=='kill':
                import psutil
                from .test_external_codex_web_channel_local import _wait_for_log
                agent,command,settings=next(item for item in launches if 'jiuwenswarm.server.app_agentserver' in item[1])
                children=psutil.Process(agent.pid).children(recursive=True)
                agent.kill();agent.wait(timeout=10)
                # A crashed parent cannot own child cleanup; the test supervisor
                # reaps only the exact descendants captured before the fault.
                for child in reversed(children):
                    try:child.kill()
                    except psutil.NoSuchProcess:pass
                evidence['killed_agent_pid']=agent.pid
                restarted=original_start(command,env=settings['env'],log_path=scope/'agentserver-restarted.log')
                await _wait_for_log(scope/'agentserver-restarted.log','ready:',timeout=90)
                evidence['checks'].append('independent AgentServer killed and restarted with original persisted data')
            if control=='disconnect':
                from .test_external_codex_web_channel_local import _wait_for_websocket
                gateway,command,settings=next(item for item in launches if 'jiuwenswarm.gateway.app_gateway' in item[1])
                gateway.kill();gateway.wait(timeout=10)
                await asyncio.sleep(3)
                restarted=original_start(command,env=settings['env'],log_path=scope/'gateway-restarted.log')
                await _wait_for_websocket(url,timeout=90)
                evidence['checks'].append('original Gateway disconnected from AgentServer and restarted')
            if control in {'disconnect','kill'}:
                await ws.close();ws=None
                await asyncio.sleep(2)
                ws=await websockets.connect(url,max_size=8*1024*1024)
                await _send_request(ws,'late','chat.send',answer)
                late,frames=await _receive_until(ws,lambda f:f.get('event') in {'chat.error','runtime.accepted'}
                    or (f.get('id')=='late' and f.get('ok') is False),timeout=45)
                evidence['frames']+=frames
                assert late.get('event')!='runtime.accepted', 'Disconnected owner accepted a stale answer'
                evidence['checks'].append('ended owner refuses late answer')
                if control=='kill':
                    await _send_request(ws,'resume-after-crash','chat.send',{
                        'session_id':sid,'mode':'team.code.normal','work_mode':'code','query':'继续之前的任务。'})
                    failure,frames=await _receive_until(ws,lambda f:f.get('event')=='chat.error' or
                        f.get('payload',{}).get('terminal_status')=='failed',timeout=90)
                    evidence['frames']+=frames
                    assert not (scope/'approval-marker.txt').exists()
                    evidence['checks'].append('cold request fails closed without replaying unconfirmed command')
            else:
                await _send_request(ws,'stop','chat.interrupt',{'session_id':sid,'mode':'team.code.normal',
                    'work_mode':'code','team':True,'intent':'cancel'})
                stopped,frames=await _receive_until(ws,lambda f:f.get('event')=='chat.interrupt_result',timeout=60)
                evidence['frames']+=frames
                assert stopped['payload'].get('success') is True,stopped
                evidence['checks'].append('original Runtime cancellation confirmed')
            assert not (scope/'approval-marker.txt').exists(), 'Unapproved command executed'
            evidence['checks'].append('no unapproved tool side effect')
            await ws.close();ws=None
            if restarted is not None:
                support._stop_process(restarted);restarted=None
        await cleanup_audit(scope)
    finally:
        if restarted is not None:
            support._stop_process(restarted)
            support.remove_secret_artifacts(scope)
        if ws is not None:await ws.close()
        (scope/'controls.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
