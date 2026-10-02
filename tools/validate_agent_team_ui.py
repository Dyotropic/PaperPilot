"""Native Flet team acceptance: independent children, stop, full replies, chat switch.

Run with tools/run_validation.py in an approved desktop session. All materials,
database and model responses are synthetic; keyboard and pointer events are real.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback
from unittest.mock import patch

import flet as ft
from paperpilot import library
from paperpilot.agent_runtime import checkpoint, publish_reply
from paperpilot.agent_team import WORKER_PROMPT, saved_teams
from paperpilot.llm_client import LLMClient, ChatResult
from paperpilot.llm_usage import TokenUsage
from pages.context import ctx, apply_theme
from tools.agent_ui_capture import screenshot
import app
import pages.agent_panel as panel

project=library.create_project("团队原生验收","光谱与标定研究")
blocked=threading.Event()
requests=[]
failures=[]


class Client(LLMClient):
    def __init__(self):
        super().__init__('deepseek-flash');self.provider='deepseek'
    def _do_cancellable(self,messages,*args):return self._do_chat(messages)
    def _do_chat(self,messages,*args):
        requests.append(copy.deepcopy(messages))
        if messages[0]['content']==WORKER_PROMPT:
            task=next(m['content'] for m in reversed(messages) if '主 Agent 分配的任务' in str(m['content']))
            if '偏差' in task:
                blocked.set();publish_reply('正在核对仪器漂移与标定记录。')
                while True:
                    threading.Event().wait(.03);checkpoint()
            if '英文' in task:text='Evidence s1: sensitivity is insufficient to establish clinical specificity.'
            else:text='证据 s1：光谱峰值不同；测量精度 0.12 不等于检测准确率，需补充标定条件。'
        elif '系统返回的子 Agent 结果' in str(messages[-1]['content']):
            text=('Main review: sensitivity and clinical specificity describe different evidence.'
                if 'sensitivity' in str(messages) else
                '主 Agent 已审查：光谱测量需要独立标定；偏差核验已停止，不能宣称该项完成。')
        elif '第二个' in str(messages[-1]['content']):
            text='[TEAM]{"tasks":[{"name":"英文证据","instruction":"英文说明 sensitivity 与 specificity 的差异"}]}[/TEAM]'
        else:
            text='[TEAM]{"tasks":[{"name":"光谱方法","instruction":"核对光谱方法与测量精度"},{"name":"常见偏差","instruction":"检查偏差与标定记录"}]}[/TEAM]'
        return ChatResult(content=text,finish_reason='stop',usage=TokenUsage(5000,100,2048,2952))


async def main(page):
    client_factory=lambda task=None:Client()
    with patch('paperpilot.ai_service.get_client',side_effect=client_factory):app.main(page)
    page.title=f'PaperPilot Native Team {os.getpid()}'
    page.window.width,page.window.height=1000,750
    panel.set_agent_project(project.id,project.name,project.description);page.update()
    with patch('paperpilot.ai_service.get_client',side_effect=client_factory), \
         patch('paperpilot.ai_service.get_task_model',return_value='deepseek-flash'), \
         patch('paperpilot.ai_service.get_task_model_override',return_value=''):
        async def inputs(actions):
            proc=await asyncio.create_subprocess_exec(sys.executable,'-B',str(Path(__file__).with_name('agent_native_input.py')),
                json.dumps(actions),page.title,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            out,err=await proc.communicate()
            assert proc.returncode==0,err.decode()
            await asyncio.sleep(.5)
        async def idle():
            for _ in range(60):
                if not panel._thinking_active:return
                await asyncio.sleep(.1)
            raise AssertionError('Main turn did not settle')
        try:
            await asyncio.sleep(2)
            await inputs([['click',1190,1054],['text','并行核对光谱方法和常见偏差，测量精度 0.12，标定记录缺失。'],['enter']])
            for _ in range(40):
                if blocked.is_set() and panel._team_view.records:break
                await asyncio.sleep(.1)
            assert blocked.is_set()
            await asyncio.sleep(.8)
            await screenshot(page,'native-team-running.png')
            print('READY team roster',page.title,flush=True)
            if os.environ.get('PAPERPILOT_UI_REVIEW'):await asyncio.sleep(20)
            # Roster is under the session/usage rows on the right panel.
            await inputs([['click',1200,438]])
            assert panel._team_view.dialog and panel._team_view.dialog.open,'Native child row click failed'
            await screenshot(page,'native-team-child-running.png')
            print('READY child dialog',page.title,flush=True)
            if os.environ.get('PAPERPILOT_UI_REVIEW'):await asyncio.sleep(20)
            await inputs([['click',1070,1000]])
            await idle()
            cm=ctx.ai_service.get_conversation(project.id,project.name,session_id=panel._agent_session_id)
            team=saved_teams(cm)[0]
            assert {a['state'] for a in team['agents']}=={'completed','cancelled'},team
            assert panel._team_view.dialog is not None
            await screenshot(page,'native-team-child-stopped.png')
            await inputs([['click',1250,1017]])
            assert panel._team_view.dialog is None,'Native close button failed'
            await screenshot(page,'native-team-reviewed.png')
            # A different independent chat and language/task, rather than replaying the first fixture.
            panel._new_agent_session();page.update()
            assert not panel._team_view.records
            await inputs([['click',1190,1054],['text','第二个任务：用英文分析 sensitivity 与 specificity 的区别。'],['enter']])
            await idle()
            current=ctx.ai_service.get_conversation(project.id,project.name,session_id=panel._agent_session_id)
            assert len(saved_teams(current)[0]['agents'])==1
            assert saved_teams(current)[0]['agents'][0]['name']=='英文证据'
            ctx.state.dark_mode=False;apply_theme(page,'slate',False)
            panel.refresh_agent_panel_theme();panel._set_agent_panel_width(320);page.update()
            await screenshot(page,'native-team-narrow-light.png')
            await inputs([['click',1250,366]])
            assert panel._team_view.dialog and '英文证据' in panel._team_view.dialog.title.value
            full=' '.join(m['content'] for m in saved_teams(current)[0]['agents'][0]['messages'])
            assert 'clinical specificity' in full
            await screenshot(page,'native-team-child-complete.png')
            panel._team_view.close()
            result=dict(synthetic_model=True,native_pointer_keyboard=True,
                live_status=True,full_child_conversation=True,child_only_stop=True,
                main_review=True,chat_isolation=True,narrow_light=True,requests=len(requests))
            (Path.cwd()/'native-team-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print('PASS native team flow',json.dumps(result),flush=True)
        except Exception:
            failures.append(True);traceback.print_exc()
            await screenshot(page,'native-team-failure.png')
        finally:
            panel.stop_agent_run()
            await asyncio.sleep(.3)
            await page.window.close()


ft.run(main)
if failures:raise SystemExit(1)
