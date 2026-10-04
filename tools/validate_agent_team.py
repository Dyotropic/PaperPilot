"""Business acceptance for isolated research teams, with diverse failure boundaries.

Run through tools/run_validation.py. Synthetic model replies are deliberately
different by task; barriers prove overlap, loopback SDK transport proves aborts.
No production database, credentials, project records or external API calls.
"""
import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from paperpilot import library
from paperpilot.agent_runtime import AgentRun, OperationCancelled, run_scope, checkpoint
from paperpilot.agent_team import (AgentTeam, SourceCatalog, parse_team_request,
    saved_teams, WORKER_PROMPT, result_observation, calculate, _live)
from paperpilot.ai_service import AIService
from paperpilot.conversation import ConversationManager
from paperpilot.llm_client import LLMClient, ChatResult, OpenAICompatClient, AnthropicClient, tools_scope
from paperpilot.llm_client import _tool_options
from paperpilot.llm_usage import TokenUsage, UsageStore
from paperpilot.config import load_config


def request(tasks):
    return "[TEAM]" + json.dumps(dict(tasks=tasks), ensure_ascii=False) + "[/TEAM]"


class ScriptClient(LLMClient):
    def __init__(self, callback):
        super().__init__("deepseek-flash")
        self.provider = "deepseek"
        self.callback = callback
    def _do_chat(self, messages, *args):
        value = self.callback(copy.deepcopy(messages))
        return value if isinstance(value, ChatResult) else ChatResult(content=value,
            finish_reason="stop", usage=TokenUsage(300, 20, 256, 44))
    def _do_cancellable(self, messages, *args):
        return self._do_chat(messages)


class TeamBusinessTests(unittest.TestCase):
    def setUp(self):
        self.project = library.create_project("团队业务 " + str(time.time_ns()), "Evidence-first research")
        self.service = AIService()
        self.cm = self.service.get_conversation(self.project.id, self.project.name)
        self.config = load_config()
        self.config_patch = patch("paperpilot.config.load_config", return_value=self.config)
        self.config_patch.start()
        self.before = copy.deepcopy(self.config.get("agent", {}))
        self.config.setdefault("agent", {}).setdefault("team", {}).update(
            enabled=True, max_parallel=3, max_agents=6, max_batches=3, timeout_seconds=10)
    def tearDown(self):
        self.config["agent"] = self.before
        self.config_patch.stop()
        self.assertFalse(any(k[0] == self.cm.session_id for k in _live))

    def chat(self, callback, message, **kwargs):
        with patch("paperpilot.ai_service.get_client", side_effect=lambda task=None: ScriptClient(callback)), \
             patch("paperpilot.ai_service.get_task_model", return_value="deepseek-flash"), \
             patch("paperpilot.ai_service.get_task_model_override", return_value=""):
            return self.service.chat(self.project.id, self.project.name, message,
                session_id=self.cm.session_id, **kwargs)

    def team(self, callback, *, parent=None, messages=None):
        return AgentTeam(self.cm, self.project.id, parent,
            messages or [dict(role="user", content="论文 A: accuracy .84; 论文 B: .88 on a different dataset.")],
            lambda: ScriptClient(callback), "deepseek-flash")

    def test_compare_methods_concurrent_reads_followup_and_main_review(self):
        barrier = threading.Barrier(2)
        child_calls, main_calls, instances = [], [], []
        def model(messages):
            if messages[0]["content"] == WORKER_PROMPT:
                child_calls.append(messages)
                instructions = [m["content"] for m in messages if m["role"] == "user" and "主 Agent 分配的任务" in str(m["content"])]
                if "进一步" in instructions[-1]:
                    self.assertIn("不同数据集", str(messages))
                    return "进一步核验：s1 原文表明评估数据集不同，不能直接比较 .84 和 .88。"
                if not any(m["role"] == "assistant" for m in messages):
                    barrier.wait(3)
                    return '[READ_SOURCE]{"source_id":"s1","start":0,"length":6000}[/READ_SOURCE]'
                if "方法" in instructions[-1]:
                    return "方法分析：s1 中 A=.84，B=.88，但不同数据集；需要核验。[ACTION:import]{\"project\":\"禁止子写入\"}[/ACTION]"
                return "独立核验：s1 缺少相同测试集和误差条，数值不可直接排序。"
            main_calls.append(messages)
            if len(main_calls) == 1:
                return request([dict(name="方法分析", instruction="比较方法，读取来源"),
                                dict(name="实验核验", instruction="核验评估数据集和误差条")])
            if len(main_calls) == 2:
                raw = messages[-1]["content"].split("\n")[1]
                rows = json.loads(raw)
                return request([dict(agent_id=rows[0]["agent_id"], instruction="进一步查明可比性限制")])
            self.assertIn("不能直接比较", str(messages))
            return "主 Agent 审查：不同数据集不能据此排名；应在相同数据集补充对照实验。"
        answer = self.chat(model, "请并行比较论文 A 和 B，并核验实验可比性。论文 A=.84、B=.88，来自不同数据集。")
        self.assertIn("主 Agent 审查", answer["reply"])
        team = saved_teams(self.cm)[0]
        self.assertEqual(team["state"], "completed")
        self.assertEqual([a["turns"] for a in team["agents"]], [2,1])
        self.assertTrue(all(a["released"] for a in team["agents"]))
        self.assertTrue(all(a["sources_read"] for a in team["agents"]))
        self.assertEqual(len(self.cm.display_messages), 2)
        self.assertEqual(self.cm.total_rounds, 1)
        rebuilt = ConversationManager(self.project.name, session_id=self.cm.session_id, storage_path=self.cm._path)
        self.assertEqual(rebuilt._history, self.cm._history)
        records = UsageStore().records(self.project.id)
        self.assertEqual(sum(r["task"] == "subagent" for r in records), 5)
        self.assertTrue(all(r["session_id"] == self.cm.session_id for r in records))
        self.assertTrue(all(r["operation"].startswith("team:") for r in records if r["task"] == "subagent"))
        self.assertNotIn("[ACTION:import]", answer["reply"])

    def test_simple_french_question_never_creates_team(self):
        answer = self.chat(lambda messages: "Une étude longitudinale suit les mêmes sujets dans le temps.",
            "Explique en français ce qu'est une étude longitudinale.")
        self.assertIn("longitudinale", answer["reply"])
        self.assertEqual(saved_teams(self.cm), [])

    def test_prior_native_exchanges_do_not_displace_original_uploaded_evidence(self):
        messages=[dict(role='user',content='原始测序资料：样本ID X17；仅提供摘要，不含原始reads。')]
        for n in range(32):
            messages.extend([dict(role='assistant',content='',tool_calls=[dict(id=str(n),function=dict(name='calculate',arguments='{}'))]),
                dict(role='tool',tool_call_id=str(n),content='旧团队中间结果')])
        messages.extend([dict(role='assistant',content='此前主审查：reads未提供。'),dict(role='user',content='继续核验可重复性')])
        catalog=SourceCatalog(messages)
        self.assertIn('样本ID X17',catalog.index)
        self.assertNotIn('旧团队中间结果',catalog.index)
        self.assertEqual(len(catalog.sources),3)

    def test_checkpoint_inside_team_scope_cannot_dispatch_or_keep_tool_proposals(self):
        requests=[]
        def model(messages):
            requests.append(copy.deepcopy(_tool_options.get()))
            return '## 用户目标与需求\n核验纳米材料，原文缺页仍未验证。'
        with patch('paperpilot.ai_service.get_client',return_value=ScriptClient(model)),tools_scope([dict(type='function',function=dict(name='team_dispatch'))]):
            summary=self.service._compress_messages([dict(role='user',content='合成纳米材料摘录，缺少第六页。')])
            self.assertIn('缺页',summary)
            self.assertTrue(_tool_options.get()['tools'])
        self.assertEqual(requests[0]['tools'],None)
        with patch('paperpilot.ai_service.get_client',return_value=ScriptClient(lambda m:'[TEAM]{"tasks":[]}[/TEAM]')):
            self.assertIsNone(self.service._compress_messages([dict(role='user',content='不得把压缩误派发为团队')]))

    def test_native_function_dispatch_and_fragmented_worker_calls_with_real_sdk(self):
        captured=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(handler):
                body=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                captured.append(body);messages=body['messages']
                worker=messages[0]['content']==WORKER_PROMPT
                has_result=any(m['role']=='tool' for m in messages)
                if has_result:
                    text='核验结果 s1：精度指标与准确率含义不同。' if worker else '主 Agent 审查后确认两类指标不能混用。'
                    call=None
                else:
                    name='read_source' if worker else 'team_dispatch'
                    args=dict(source_id='s1',start=0,length=6000) if worker else dict(tasks=[
                        dict(name='定义核验',instruction='读取并核验指标定义'),dict(name='评估限制',instruction='读取并分析测量边界')])
                    call=dict(id='call_'+str(len(captured)),type='function',function=dict(name=name,arguments=json.dumps(args,ensure_ascii=False)))
                    text=''
                handler.send_response(200)
                handler.send_header('Content-Type','text/event-stream' if body.get('stream') else 'application/json')
                handler.send_header('Connection','close');handler.end_headers()
                usage=dict(prompt_tokens=700,completion_tokens=70,total_tokens=770,prompt_cache_hit_tokens=256,prompt_cache_miss_tokens=444)
                if body.get('stream'):
                    def event(delta,reason=None):
                        handler.wfile.write(('data: '+json.dumps(dict(id='r',object='chat.completion.chunk',model='deepseek-flash',
                            choices=[dict(index=0,delta=delta,finish_reason=reason)]))+'\n\n').encode());handler.wfile.flush()
                    if call:
                        args=call['function']['arguments'];middle=len(args)//2
                        event(dict(tool_calls=[dict(index=0,id=call['id'],type='function',function=dict(name=call['function']['name'],arguments=args[:middle]))]))
                        event(dict(tool_calls=[dict(index=0,function=dict(arguments=args[middle:]))]))
                        event({},'tool_calls')
                    else:event(dict(content=text),'stop')
                    handler.wfile.write(('data: '+json.dumps(dict(id='r',object='chat.completion.chunk',model='deepseek-flash',choices=[],usage=usage))+'\n\ndata: [DONE]\n\n').encode())
                else:
                    message=dict(role='assistant',content=text)
                    if call:message['tool_calls']=[call]
                    handler.wfile.write(json.dumps(dict(id='r',object='chat.completion',model='deepseek-flash',usage=usage,
                        choices=[dict(index=0,message=message,finish_reason='tool_calls' if call else 'stop')])).encode())
                handler.wfile.flush();handler.close_connection=True
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with patch('paperpilot.ai_service.get_client',side_effect=lambda task=None:OpenAICompatClient('deepseek',
                    f'http://127.0.0.1:{server.server_port}/v1','synthetic','deepseek-flash')), \
                    patch('paperpilot.ai_service.get_task_model',return_value='deepseek-flash'), \
                    patch('paperpilot.ai_service.get_task_model_override',return_value=''):
                answer=self.service.chat(self.project.id,self.project.name,'请并行核验精度与准确率，来源给出 precision .12，accuracy .82。',session_id=self.cm.session_id)
            self.assertIn('审查后',answer['reply'])
            rows=saved_teams(self.cm)[0]['agents']
            self.assertTrue(all(a['sources_read'] for a in rows))
            self.assertTrue(any(m['role']=='tool' for m in self.cm._messages))
            self.assertEqual(self.cm.total_rounds,1)
            self.assertEqual(len(self.cm.display_messages),2)
            self.assertEqual(len(captured),6)
            self.assertTrue(all(r['tools'] for r in captured))
            self.assertTrue(all(r['thinking']['type']=='disabled' for r in captured if r['messages'][0]['content']==WORKER_PROMPT))
        finally:server.shutdown();server.server_close()

    def test_anthropic_native_tool_blocks_preserve_signature_and_image_results(self):
        client=AnthropicClient('synthetic','claude-sonnet-5')
        native_blocks=[dict(type='thinking',thinking='private analysis',signature='signature-fixture'),
            dict(type='tool_use',id='tool-read',name='read_source',input=dict(source_id='s1',start=0,length=20))]
        image=dict(type='image_url',image_url=dict(url='data:image/png;base64,ZmFrZQ=='))
        messages=[dict(role='system',content='worker'),dict(role='user',content='source'),
            dict(role='assistant',content='',provider_blocks=native_blocks),
            dict(role='tool',tool_call_id='tool-read',content='source evidence'),
            dict(role='user',content=[dict(type='text',text='image evidence'),image])]
        with tools_scope([dict(type='function',function=dict(name='read_source',description='read',parameters=dict(type='object')))],'none'):
            system,converted=client._split_messages(messages)
            options=client._tool_kwargs()
        self.assertEqual(converted[1]['content'],native_blocks)
        self.assertEqual(converted[2]['content'][0]['type'],'tool_result')
        self.assertEqual(converted[2]['content'][0]['tool_use_id'],'tool-read')
        self.assertEqual(converted[3]['content'][1]['type'],'image')
        self.assertEqual(options['tool_choice'],dict(type='none'))
        self.assertEqual(options['tools'][0]['input_schema'],dict(type='object'))

    def test_numeric_review_corrects_child_arithmetic_without_executing_code(self):
        mains=[]
        def model(messages):
            if messages[0]['content']==WORKER_PROMPT:
                return '待审查统计结论：标准误 0.11347（未经计算工具核验）。'
            mains.append(messages)
            if len(mains)==1:return request([dict(name='统计结果',instruction='核对标准误')])
            if len(mains)==2:
                return ChatResult(tool_calls=[dict(id='calc-check',type='function',function=dict(name='calculate',
                    arguments=json.dumps(dict(expression='sqrt((4/38)*(1-4/38)/38+(7/41)*(1-7/41)/41)'))))],finish_reason='tool_calls')
            value=json.loads(messages[-1]['content'])['value']
            self.assertAlmostEqual(value,.07701768931129796,places=8)
            return '主 Agent 复算后纠正子结果：标准误约 0.07702，原来的 0.11347 未通过核验。'
        answer=self.chat(model,'A=4/38、B=7/41，复算风险差标准误并审查独立分析。')
        self.assertIn('纠正',answer['reply'])
        self.assertEqual(self.cm.total_rounds,1)
        self.assertTrue(any(m.get('tool_call_id')=='calc-check' for m in self.cm._messages))
        for expression in ['1/0','__import__("os")','(1).__class__','[x for x in range(3)]','2**1000','comb(90000,2)','True','sqrt(-1)']:
            with self.subTest(expression=expression),self.assertRaises(ValueError):calculate(dict(expression=expression))

    def test_partial_failure_is_explicit_in_review_with_library_snapshot(self):
        mains = []
        def model(messages):
            if messages[0]["content"] == WORKER_PROMPT:
                return "趋势：两篇摘要均研究 domain shift；只能分析摘要。" if "趋势" in str(messages[-1]) else ""
            mains.append(messages)
            if len(mains) == 1:
                return request([dict(name="趋势",instruction="梳理趋势"),dict(name="数据",instruction="核验数据来源")])
            self.assertIn('"state": "failed"', str(messages[-1]))
            return "趋势分析已返回，数据核验失败；摘要不足以确认实验细节。"
        answer = self.chat(model, "梳理研究现状并核验数据", include_library_context=True,
            project_papers=[dict(title="Adaptation study", abstract="Investigates domain shift."),
                            dict(title="Robust learning", abstract="Domain shift in healthcare.")])
        self.assertIn("失败", answer["reply"])
        self.assertEqual({a["state"] for a in saved_teams(self.cm)[0]["agents"]}, {"completed","failed"})

    def test_multiple_native_read_and_calculation_results_match_all_ids(self):
        mains=[]
        def call(name, expression, ident):
            return dict(id=ident,type='function',function=dict(name=name,arguments=json.dumps(expression)))
        def model(messages):
            if messages[0]['content']==WORKER_PROMPT:
                tools=[m for m in messages if m['role']=='tool']
                if not tools:
                    return ChatResult(tool_calls=[
                        call('read_source',dict(source_id='s1',start=0,length=200),'source-calibration'),
                        call('calculate',dict(expression='(120-4)/8'),'calibration-value'),
                        call('calculate',dict(expression='sqrt(9+16)'),'calibration-error')])
                self.assertEqual([m['tool_call_id'] for m in tools],['source-calibration','calibration-value','calibration-error'])
                self.assertEqual(json.loads(tools[1]['content'])['value'],14.5)
                return '证据 s1：校准后浓度14.5，合成不确定度5；单位与独立性仍须核验。'
            mains.append(messages)
            if len(mains)==1:return request([dict(name='校准',instruction='读取仪器记录并复算浓度和误差')])
            if len(mains)==2:
                return ChatResult(tool_calls=[call('calculate',dict(expression='(120-4)/8'),'main-concentration'),
                    call('calculate',dict(expression='sqrt(9+16)'),'main-uncertainty')])
            self.assertEqual([m['tool_call_id'] for m in messages[-2:]],['main-concentration','main-uncertainty'])
            self.assertEqual(json.loads(messages[-1]['content'])['value'],5)
            return '主 Agent 独立复算14.5与5，量纲和独立性尚未验证。'
        answer=self.chat(model,'仪器强度120，空白4，斜率8；复算校准浓度，并核验误差分量3、4的合成。')
        self.assertIn('14.5与5',answer['reply'])
        row=saved_teams(self.cm)[0]['agents'][0]
        self.assertEqual(len(row['calculations']),2)
        self.assertEqual(len(row['sources_read']),1)
        self.assertEqual(row['state'],'completed')

    def test_native_stop_and_restart_repair_pending_calls_before_continue(self):
        arrived=threading.Event();errors=[]
        parent=AgentRun(self.cm,self.project.id,'核验荧光测量和缺失的实验记录')
        def model(messages):
            if messages[0]['content']==WORKER_PROMPT:
                arrived.set()
                while True:
                    time.sleep(.01);checkpoint()
            return ChatResult(tool_calls=[dict(id='dispatch-lab',type='function',function=dict(name='team_dispatch',
                arguments=json.dumps(dict(tasks=[dict(name='荧光测量',instruction='核验信号'),dict(name='记录缺口',instruction='核验缺失记录')]))))])
        def execute():
            try:
                with run_scope(parent):self.chat(model,'核验荧光测量和缺失的实验记录')
            except BaseException as exc:errors.append(exc)
            finally:parent.finish()
        thread=threading.Thread(target=execute);thread.start()
        try:
            self.assertTrue(arrived.wait(3));parent.stop();thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(errors[0],OperationCancelled)
            self.assertTrue(any(m.get('tool_call_id')=='dispatch-lab' and '未完成' in m['content'] for m in self.cm._messages))
            self.assertEqual(self.cm._messages[-1]['role'],'assistant')
            continued=self.chat(lambda m:'已核对停止记录，缺失实验数据需补充后再继续。','继续核对缺失资料')
            self.assertIn('缺失实验数据',continued['reply'])
        finally:parent.stop();thread.join(4)
        # Simulate a crash after only one of two native tool results was persisted.
        self.cm.add_user_message('重启前核验第二套仪器数据')
        self.cm.add_internal_message('assistant','',team_id='restart-fixture',tool_calls=[
            dict(id='done-calc',type='function',function=dict(name='calculate',arguments='{"expression":"3+5"}')),
            dict(id='pending-calc',type='function',function=dict(name='calculate',arguments='{"expression":"7+9"}'))])
        self.cm.add_internal_message('tool','{"value":8}',team_id='restart-fixture',tool_call_id='done-calc')
        rebuilt=ConversationManager(self.project.name,session_id=self.cm.session_id,storage_path=self.cm._path)
        rebuilt.recover_interrupted_run()
        self.assertEqual(rebuilt._messages[-1]['tool_call_id'],'pending-calc')
        self.assertIn('未完成',rebuilt._messages[-1]['content'])
        before=len(rebuilt._messages);rebuilt.recover_interrupted_run()
        self.assertEqual(len(rebuilt._messages),before)

    def test_missing_source_then_corrected_multilingual_evidence(self):
        calls = []
        def model(messages):
            calls.append(messages)
            if len(calls) == 1:
                return '[READ_SOURCE]{"source_id":"不存在","start":0,"length":20}[/READ_SOURCE]'
            if len(calls) == 2:
                self.assertIn("资料不存在", messages[-1]["content"])
                return '[READ_SOURCE]{"source_id":"s1","start":0,"length":200}[/READ_SOURCE]'
            self.assertIn("Sample size: 12", str(messages[-1]))
            return "证据 s1: Sample size 12；12 人样本只支持初步探索，中文结论与原文一致。"
        team = self.team(model, messages=[dict(role="user",content="样本量说明 / Sample size: 12; exploratory only.")])
        try:
            rows = team.dispatch([dict(name="双语核验",instruction="核对中英文的样本量和结论")])
            self.assertEqual(rows[0]["state"],"completed")
            self.assertEqual(len(rows[0]["sources_read"]),1)
        finally:
            team.close()

    def test_output_truncation_and_oversize_context_do_not_claim_success(self):
        team = self.team(lambda m: ChatResult(content="分析未结束", finish_reason="length"))
        try:
            rows = team.dispatch([dict(name="长综述",instruction="分析完整证据")])
            self.assertEqual(rows[0]["state"],"failed")
            self.assertIn("截断",rows[0]["error"])
        finally:team.close("failed")
        with patch("paperpilot.agent_team.context_policy", return_value=type("P",(),dict(provider="deepseek",window=1024,output_reserve=128))()):
            team = self.team(lambda m: self.fail("Oversize materials must not reach provider"),
                messages=[dict(role="user",content="Large source " * 1000)] * 8)
            try:
                rows=team.dispatch([dict(name="超限",instruction="分析资料")])
                self.assertEqual(rows[0]["state"],"failed")
                self.assertIn("上下文预算",rows[0]["error"])
            finally:team.close("failed")

    def test_invalid_empty_duplicate_and_foreign_dispatch_is_atomic(self):
        invalid = ["[TEAM]{}[/TEAM]", request([]), request([dict(name="",instruction="x")]),
            request([dict(name="a",instruction="")]), request([dict(name="a",instruction="x")]*2),
            request([dict(name="a",instruction="x",write=True)]), "示例："+request([dict(name="x",instruction="x")])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):parse_team_request(value)
        team=self.team(lambda m:"未应调用")
        try:
            with self.assertRaises(ValueError):team.dispatch([dict(name="有效",instruction="x"),dict(agent_id="other-session",instruction="x")])
            self.assertEqual(team.data["agents"],[])
        finally:team.close("failed")

    def test_mixed_native_batch_optional_nulls_dispatches_once_and_returns_each_result(self):
        mains=[]
        def model(messages):
            if messages[0]['content']==WORKER_PROMPT:return '证据 s1：地表采样需明确空间覆盖，点测不能代表全区域。'
            mains.append(messages)
            if len(mains)==1:
                return ChatResult(tool_calls=[
                    dict(id='spatial-a',type='function',function=dict(name='team_dispatch',arguments=json.dumps(dict(tasks=[
                        dict(name='采样方法',agent_id=None,instruction='审查采样覆盖')])))),
                    dict(id='area',type='function',function=dict(name='calculate',arguments='{"expression":"12*7"}')),
                    dict(id='spatial-b',type='function',function=dict(name='team_dispatch',arguments=json.dumps(dict(tasks=[
                        dict(name='尺度核验',agent_id='',instruction='核验点测与区域的尺度')]))))])
            if len(mains)==2:
                self.assertEqual([m['tool_call_id'] for m in messages[-3:]],['spatial-a','area','spatial-b'])
                self.assertIn('采样方法',messages[-3]['content'])
                self.assertNotIn('尺度核验',messages[-3]['content'])
                self.assertEqual(json.loads(messages[-2]['content'])['value'],84)
                agent_id=json.loads(messages[-3]['content'].split('\n')[1])[0]['agent_id']
                return ChatResult(tool_calls=[
                    dict(id='spatial-followup',type='function',function=dict(name='team_dispatch',arguments=json.dumps(dict(tasks=[
                        dict(name=None,agent_id=agent_id,instruction='进一步核查采样范围')])))),
                    dict(id='scale-check',type='function',function=dict(name='calculate',arguments='{"expression":"84/4"}'))])
            self.assertEqual([m['tool_call_id'] for m in messages[-2:]],['spatial-followup','scale-check'])
            return '区域面积84，点测的空间代表性尚未验证。'
        answer=self.chat(model,'并行检查地表采样的覆盖与尺度；矩形样区12m乘7m。')
        self.assertIn('面积84',answer['reply'])
        record=saved_teams(self.cm)[0]
        self.assertEqual(record['batches'],2)
        self.assertEqual(len(record['agents']),2)
        self.assertEqual([a['turns'] for a in record['agents']],[2,1])

    def test_queue_limits_child_only_stop_and_parent_resume(self):
        self.config["agent"]["team"]["max_parallel"]=1
        started, release = threading.Event(), threading.Event()
        def model(messages):
            started.set()
            while not release.wait(.02):checkpoint()
            return "后续任务完成，证据不足部分明确保留。"
        parent=AgentRun(self.cm,self.project.id,"拆分生物信息学课题")
        team=self.team(model,parent=parent)
        results,errors=[],[]
        def execute():
            try:
                with run_scope(parent):results.extend(team.dispatch([dict(name="基因方法",instruction="分析基因资料"),dict(name="对照设计",instruction="检查对照")]))
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=execute);thread.start()
        try:
            self.assertTrue(started.wait(3))
            with team.lock:
                self.assertEqual([a["state"] for a in team.data["agents"]],["running","queued"])
                first_id=team.data["agents"][0]["agent_id"]
            team.stop(first_id)
            release.set();thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual([r["state"] for r in results],["cancelled","completed"])
            self.assertFalse(parent.token.cancelled)
            self.config["agent"]["team"]["max_agents"]=1
            team.settings["max_agents"]=2
            with self.assertRaises(ValueError):team.dispatch([dict(name="超额",instruction="x")])
        finally:
            release.set();team.close();parent.finish();thread.join(4)

    def test_parent_stop_aborts_real_sdk_wait_and_queued_actions(self):
        arrived=threading.Event();release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));arrived.set();release.wait(5)
                self.close_connection=True
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.config["agent"]["team"]["max_parallel"]=1
        parent=AgentRun(self.cm,self.project.id,"审查流行病学证据")
        team=AgentTeam(self.cm,self.project.id,parent,[dict(role="user",content="Cohort study n=320")],
            lambda: OpenAICompatClient('deepseek',f'http://127.0.0.1:{server.server_port}/v1','synthetic','deepseek-flash'),"deepseek-flash")
        errors=[]
        def execute():
            try:
                with run_scope(parent):team.dispatch([dict(name="队列",instruction="检查队列设计"),dict(name="偏差",instruction="检查偏差")])
            except BaseException as exc:errors.append(exc)
            finally:team.close("cancelled");parent.finish()
        thread=threading.Thread(target=execute);thread.start()
        try:
            self.assertTrue(arrived.wait(4));started=time.monotonic();parent.stop();thread.join(3)
            self.assertFalse(thread.is_alive(),"SDK transport did not abort before response headers")
            self.assertLess(time.monotonic()-started,3)
            self.assertIsInstance(errors[0],OperationCancelled)
            self.assertEqual({a["state"] for a in saved_teams(self.cm)[0]["agents"]},{"cancelled"})
            self.assertEqual(self.cm._meta["last_run"]["state"],"cancelled")
            resumed=AgentRun(self.cm,self.project.id,"继续")
            self.assertEqual(resumed.goal,"审查流行病学证据");resumed.finish()
        finally:
            release.set();parent.stop();thread.join(4);server.shutdown();server.server_close()

    def test_timeout_and_closed_team_reclaims_runtime_keeps_reply(self):
        started=threading.Event()
        def wait_forever(m):
            started.set()
            while True:
                time.sleep(.01);checkpoint()
        team=self.team(wait_forever)
        team.settings["timeout_seconds"]=.15  # Deterministic short deadline, production config stays bounded.
        try:
            rows=team.dispatch([dict(name="等待服务",instruction="读取实验记录")])
            self.assertEqual(rows[0]["state"],"timed_out")
        finally:team.close("failed")
        self.assertTrue(started.is_set())
        self.assertTrue(saved_teams(self.cm)[0]["agents"][0]["released"])
        with self.assertRaises(ValueError):team.dispatch([dict(name="已回收",instruction="x")])

    def test_corrupt_saved_conversation_is_reported_without_overwrite(self):
        team=self.team(lambda m:'只读环境资料分析完成，结果必须审查。')
        team.dispatch([dict(name='环境资料',instruction='核验资料来源')]);team.close()
        data=json.loads(team.path.read_text(encoding='utf-8'))
        data['agents'][0]['messages'].append(dict(role='assistant',content='异常工具记录',tool_calls=[
            dict(id='unexpected-write',type='function',function=dict(name='write_database',arguments='{}'))]))
        original=json.dumps(data,ensure_ascii=False)
        team.path.write_text(original,encoding='utf-8')
        with self.assertRaises(ValueError):saved_teams(self.cm,recover=True)
        self.assertEqual(team.path.read_text(encoding='utf-8'),original)

    def test_restart_session_isolation_compaction_and_legacy_compatibility(self):
        # An interrupted process left a durable pending child; restart never replays work.
        team=self.team(lambda m:"统计方法审查：未给出置信区间，不能确认显著性。")
        rows=team.dispatch([dict(name="统计核验",instruction="核对显著性")]);team.close()
        data=saved_teams(self.cm)[0];data['state']='running';data['agents'][0]['state']='running'
        team.path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        self.assertEqual(saved_teams(self.cm,recover=True)[0]['agents'][0]['state'],'interrupted')
        other_cm=self.service.create_session(self.project.id,self.project.name)
        self.assertEqual(saved_teams(other_cm),[])
        self.cm.add_user_message('原目标：核验统计结果。'+'evidence '*100)
        self.cm.add_internal_message('assistant',request([dict(name='统计',instruction='核验')]),team_id=team.id)
        self.cm.add_internal_message('user',result_observation(rows),team_id=team.id)
        self.cm.add_assistant_message('主 Agent 审查：显著性未验证。')
        plan=self.cm.compaction_plan('research system',manual=True)
        self.assertIsNotNone(plan)
        self.cm.commit_compaction('目标核验统计；显著性未验证；子 Agent 记录在团队目录。',plan,mode='manual',provider='deepseek',model='deepseek-flash')
        self.assertEqual(len(self.cm.display_messages),2)
        self.assertEqual(saved_teams(self.cm)[0]['agents'][0]['result'],rows[0]['result'])

    def test_photos_are_native_on_read_and_never_base64_on_disk(self):
        carrier=dict(type='image_url',image_url=dict(url='data:image/png;base64,ZmFrZQ=='))
        calls=[]
        def model(messages):
            calls.append(messages)
            if len(calls)==1:return '[READ_SOURCE]{"source_id":"s1","start":0,"length":100}[/READ_SOURCE]'
            self.assertEqual(messages[-1]['content'][1],carrier)
            return '图片资料已提供；此合成图片不能用于真实图像质量验收。'
        team=self.team(model,messages=[dict(role='user',content=[dict(type='text',text='Inspect microscopy image'),carrier])])
        with patch('paperpilot.agent_attachments.validate_request'):
            try:
                rows=team.dispatch([dict(name='图像核验',instruction='核对图像资料')])
                self.assertEqual(rows[0]['state'],'completed')
            finally:team.close()
        self.assertNotIn('base64',team.path.read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main(verbosity=2)
