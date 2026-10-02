"""Isolated business checks: actual SDK transport cancellation and durable resume.

Run: python -B tools/run_validation.py tools/validate_agent_stop.py
Uses only a loopback HTTP fixture and synthetic sessions; no paid API calls.
"""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch

from paperpilot.agent_runtime import AgentRun, OperationCancelled, run_scope, reply_stream, checkpoint
from paperpilot.ai_service import AIService
from paperpilot import library
from paperpilot.llm_client import OpenAICompatClient, AnthropicClient, LLMClient, ChatResult
from paperpilot.llm_usage import UsageStore, usage_scope


class AgentStopTests(unittest.TestCase):
    def setUp(self):
        self.project = library.create_project("停止测试 " + str(time.time_ns()), "Synthetic research")
        self.service = AIService()
        self.cm = self.service.get_conversation(self.project.id, self.project.name)

    def transport(self, provider, before_headers, stop=True):
        arrived, delivered, release, done = [threading.Event() for _ in range(4)]
        requests, errors, response = [], [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                arrived.set()
                if before_headers:
                    release.wait(6)
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    def event(value):
                        prefix = 'event: ' + value['type'] + '\n' if provider == 'anthropic' else ''
                        self.wfile.write((prefix + 'data: ' + json.dumps(value) + '\n\n').encode())
                        self.wfile.flush()
                    if provider == 'anthropic':
                        event(dict(type='message_start', message=dict(id='msg-fixture', type='message',
                            role='assistant', model='fixture', content=[], stop_reason=None,
                            stop_sequence=None, usage=dict(input_tokens=4, output_tokens=0))))
                        event(dict(type='content_block_start',index=0,content_block=dict(type='text',text='')))
                        event(dict(type='content_block_delta',index=0,delta=dict(type='text_delta',text='已收到的文字')))
                    else:
                        event(dict(id='req-fixture', object='chat.completion.chunk', model='fixture',
                            choices=[dict(index=0,delta=dict(content='已收到的文字',reasoning_content='私有推理'),finish_reason=None)]))
                    delivered.set()
                    if stop and not before_headers:
                        release.wait(6)
                    if provider == 'anthropic':
                        event(dict(type='content_block_stop',index=0))
                        event(dict(type='message_delta',delta=dict(stop_reason='end_turn',stop_sequence=None),
                            usage=dict(output_tokens=5)))
                        event(dict(type='message_stop'))
                    else:
                        event(dict(id='req-fixture',object='chat.completion.chunk',model='fixture',choices=[],
                            usage=dict(prompt_tokens=20,completion_tokens=5,total_tokens=25,
                                prompt_cache_hit_tokens=16,prompt_cache_miss_tokens=4)))
                        self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    self.close_connection = True
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/v1'
        client = (AnthropicClient('synthetic', 'fixture', url) if provider == 'anthropic' else
                  OpenAICompatClient('deepseek', url, 'synthetic', 'fixture'))
        run = AgentRun(self.cm,self.project.id,'保留目标并继续')
        self.cm.add_user_message(run.goal); run.user_recorded=True
        def worker():
            try:
                with run_scope(run), reply_stream(), usage_scope(project_id=self.project.id,session_id=self.cm.session_id):
                    response.append(client.chat([dict(role='user',content=run.goal)],timeout=20))
            except OperationCancelled as exc:
                errors.append(exc)
            except BaseException as exc:
                errors.append(exc)
            finally:
                run.finish(); done.set()
        threading.Thread(target=worker,daemon=True).start()
        try:
            self.assertTrue(arrived.wait(5), 'SDK did not reach loopback server')
            if stop:
                if not before_headers:
                    self.assertTrue(delivered.wait(5))
                    deadline=time.monotonic()+3
                    while not run.partial and time.monotonic()<deadline: time.sleep(.02)
                    self.assertEqual(run.partial,'已收到的文字')
                started=time.monotonic(); run.stop()
                self.assertTrue(done.wait(2), 'Transport did not abort its pending await')
                self.assertLess(time.monotonic()-started,2)
                self.assertEqual(len(errors),1)
                self.assertIsInstance(errors[0],OperationCancelled)
                self.assertEqual(self.cm._meta['last_run']['state'],'cancelled')
                record=UsageStore().records(self.project.id)[0]
                self.assertEqual(record['status'],'cancelled')
                self.assertIsNone(record['input_tokens'], 'Missing final usage must remain unknown')
                if not before_headers:
                    self.assertIn('已收到的文字',self.cm.display_messages[-1]['content'])
                    self.assertNotIn('私有推理',self.cm.display_messages[-1]['content'])
            else:
                self.assertTrue(done.wait(5))
                self.assertEqual(errors,[])
                self.assertEqual(response[0].content,'已收到的文字')
                self.assertIsNotNone(response[0].usage)
                self.assertEqual(response[0].usage.output_tokens,5)
            self.assertEqual(len(requests),1, 'Cancellation must not retry')
        finally:
            release.set(); server.shutdown(); server.server_close()

    def test_deepseek_abort_before_headers(self): self.transport('deepseek',True)
    def test_deepseek_abort_midstream(self): self.transport('deepseek',False)
    def test_anthropic_abort_before_headers(self): self.transport('anthropic',True)
    def test_anthropic_abort_midstream(self): self.transport('anthropic',False)
    def test_deepseek_completed_stream_usage(self): self.transport('deepseek',False,False)
    def test_anthropic_completed_stream_usage(self): self.transport('anthropic',False,False)

    def test_children_drain_and_partial_action_is_not_replayed(self):
        finished=[]
        run=AgentRun(self.cm,self.project.id,'检索并导入',on_done=lambda r,n:finished.append(n))
        run.reserve()
        run.partial='检索中\n[ACTION:import]{"unfinished":'
        run.completed('已经保存的旧结果')
        run.stop(); run.finish()
        self.assertEqual(finished,[])
        with self.assertRaises(OperationCancelled): run.reserve()
        run.finish()
        self.assertEqual(len(finished),1)
        loaded=self.service.session_store(self.project.id,self.project.name).open_session(self.cm.session_id)
        self.assertEqual(loaded._meta['last_run']['goal'],'检索并导入')
        self.assertEqual(loaded._meta['last_run']['completed_steps'],['已经保存的旧结果'])
        self.assertNotIn('[ACTION:',loaded.display_messages[-1]['content'])
        class Client(LLMClient):
            def _do_chat(s,messages,*args):
                s.messages=copy.deepcopy(messages)
                return ChatResult(content='继续处理未完成步骤')
        client=Client('fixture')
        next_service=AIService()
        next_cm=next_service.get_conversation(self.project.id,self.project.name,session_id=self.cm.session_id)
        resumed=AgentRun(next_cm,self.project.id,'继续')
        with patch('paperpilot.ai_service.get_client',return_value=client), run_scope(resumed):
            result=next_service.chat(self.project.id,self.project.name,'继续',session_id=self.cm.session_id)
        resumed.finish()
        self.assertEqual(result['reply'],'继续处理未完成步骤')
        self.assertTrue(any('本轮已由用户停止' in m['content'] for m in client.messages))
        self.assertTrue(client.messages[-1]['content'].endswith('继续'))
        self.assertIn('原目标：检索并导入',client.messages[-1]['content'])
        self.assertEqual(next_cm._meta['last_run']['goal'],'检索并导入')

    def test_cancelled_compression_preserves_incoming_goal(self):
        entered=threading.Event()
        class Client(LLMClient):
            def _do_cancellable(s,*args):
                entered.set()
                run.token._event.wait(5)
                checkpoint()
            def _do_chat(s,*args): raise AssertionError('Unexpected sync call')
        for _ in range(7):
            self.cm.add_user_message('旧问题')
            self.cm.add_assistant_message('旧答复')
        client=Client('fixture')
        run=AgentRun(self.cm,self.project.id,'新目标不能因摘要中断丢失')
        done=threading.Event()
        def worker():
            try:
                with patch.object(self.cm,'needs_compression',return_value=True), \
                     patch('paperpilot.ai_service.get_client',return_value=client),run_scope(run):
                    self.service.chat(self.project.id,self.project.name,run.goal,session_id=self.cm.session_id)
            except OperationCancelled: pass
            finally: run.finish(); done.set()
        threading.Thread(target=worker,daemon=True).start()
        self.assertTrue(entered.wait(5)); run.stop(); self.assertTrue(done.wait(2))
        self.assertEqual(self.cm.display_messages[-2]['content'],run.goal)
        self.assertEqual(self.cm._meta['compressed_count'],0)

    def test_restart_recovers_unfinished_turn_once(self):
        run=AgentRun(self.cm,self.project.id,'未完成的目标')
        loaded=self.service.session_store(self.project.id,self.project.name).open_session(self.cm.session_id)
        loaded.recover_interrupted_run(); loaded.recover_interrupted_run()
        self.assertEqual(loaded._meta['last_run']['state'],'interrupted')
        self.assertEqual(len(loaded.display_messages),2)
        self.assertEqual(loaded.display_messages[0]['content'],run.goal)

    def test_cancel_stops_search_cascade_before_next_query(self):
        from paperpilot import fetcher
        run=AgentRun(self.cm,self.project.id,'检索')
        calls=[]
        def source(*args,**kwargs):
            calls.append(args)
            run.stop()
            return []
        with patch.dict(fetcher._FETCH_RAW,{'arxiv':source}),run_scope(run):
            with self.assertRaises(OperationCancelled):
                fetcher.fetch_with_cascade(['primary'],['secondary'],['regular'])
        run.finish()
        self.assertEqual(len(calls),1)

    def test_cancel_reaches_batch_abstract_request(self):
        from paperpilot.sources import openalex_source as source
        run=AgentRun(self.cm,self.project.id,'补齐摘要')
        observed=[]
        lock=threading.Lock()
        def request(*args,**kwargs):
            from paperpilot.agent_runtime import current_run
            with lock:
                observed.append(current_run())
            run.token.cancel()
            return type('Response',(),{'status_code':200,'json':lambda s:dict(abstract_inverted_index={'late':[0]})})()
        papers=[dict(openalex_id=f'https://openalex.org/W{i}',abstract='') for i in range(12)]
        with patch.object(source,'_oa_cache',None),patch.object(source.requests,'get',side_effect=request),run_scope(run):
            with self.assertRaises(OperationCancelled): source._fetch_missing_abstracts(papers)
        run.finish()
        self.assertTrue(observed)
        self.assertTrue(all(owner is run for owner in observed))
        self.assertEqual(len(observed),1)
        self.assertTrue(all(not p['abstract'] for p in papers))

    def test_parallel_job_completion_emits_one_terminal_record(self):
        terminal=[]
        run=AgentRun(self.cm,self.project.id,'并发任务',on_done=lambda r,n:terminal.append(n))
        run.reserve(); run.stop()
        barrier=threading.Barrier(2)
        def finish(): barrier.wait(); run.finish()
        workers=[threading.Thread(target=finish) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(3); self.assertFalse(worker.is_alive())
        self.assertEqual(len(terminal),1)
        self.assertEqual(sum('本轮已由用户停止' in m['content'] for m in self.cm.display_messages),1)


if __name__=='__main__':
    try:
        unittest.main()
    finally:
        # Release only this isolated process's cache/DB handles before cleanup.
        if library._engine is not None:
            library._engine.dispose()
        from paperpilot.sources import openalex_source, europepmc_source
        for cache in (openalex_source._oa_cache, europepmc_source._epmc_cache):
            if cache is not None:
                cache.close()
