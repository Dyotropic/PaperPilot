"""GPT-6 transport using the official OpenAI Responses SDK.

The public PaperPilot chat/tool contract stays unchanged. Responses are stateless;
encrypted reasoning and function items are replayed from the local tool journal.
"""
import copy

from paperpilot.llm_client import (LLMClient, ChatResult, _tool_options,
                                  _managed_async_stream, reasoning_output_budget)
from paperpilot.llm_usage import normalize_usage
from paperpilot.agent_runtime import OperationCancelled, publish_reply


def _input_items(messages):
    result = []
    for message in messages:
        role = message.get("role", "user")
        if role == "tool":
            result.append(dict(type="function_call_output", call_id=message["tool_call_id"],
                               output=message.get("content", "")))
            continue
        blocks = message.get("provider_blocks") or []
        if role == "assistant" and blocks and all(
                b.get("type") in {"message", "reasoning", "function_call"} for b in blocks):
            result.extend(copy.deepcopy(blocks))
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            converted = []
            for part in content:
                if part.get("type") == "text":
                    converted.append(dict(type="input_text", text=part.get("text", "")))
                elif part.get("type") == "image_url":
                    image = part["image_url"]
                    converted.append(dict(type="input_image", image_url=image["url"],
                                          detail=image.get("detail", "auto")))
                else:
                    raise ValueError("Unsupported Responses input content")
            content = converted
        if content or not message.get("tool_calls"):
            result.append(dict(role=role, content=content))
        for call in message.get("tool_calls") or []:
            result.append(dict(type="function_call", call_id=call["id"],
                name=call["function"]["name"], arguments=call["function"]["arguments"]))
    return result


class OpenAIResponsesClient(LLMClient):
    def __init__(self, api_key, model, base_url=""):
        super().__init__(model)
        self.provider = "openai"
        self.api_key, self.base_url = api_key, base_url

    def _request_kwargs(self, messages, model, max_tokens, thinking):
        kwargs = dict(model=model, input=_input_items(messages),
                      max_output_tokens=reasoning_output_budget("openai", model, max_tokens),
                      store=False, include=["reasoning.encrypted_content"])
        if thinking is not None:
            effort = "high" if thinking else "low" if model in {"gpt-6.1-sol", "gpt-6-astra"} else "none"
            kwargs["reasoning"] = dict(effort=effort)
        options = _tool_options.get()
        if options.get("tools"):
            # Existing tools contain optional properties. Explicit non-strict mode
            # avoids Responses silently making all optional fields required.
            kwargs["tools"] = []
            for tool in options["tools"]:
                definition = dict(type="function", **copy.deepcopy(tool["function"]))
                definition.setdefault("strict", False)
                kwargs["tools"].append(definition)
            kwargs["parallel_tool_calls"] = False
            if options.get("tool_choice"):
                choice = options["tool_choice"]
                if isinstance(choice, dict) and choice.get("type") == "function":
                    choice = dict(type="function", name=choice["function"]["name"])
                kwargs["tool_choice"] = choice
        return kwargs

    @staticmethod
    def _apply_response(result, response):
        result.content = response.output_text or ""
        result.request_id, result.model = response.id, response.model
        result.usage = normalize_usage(response.usage, "openai")
        result.provider_blocks = [item.model_dump(exclude_none=True) for item in response.output]
        result.tool_calls = [dict(id=item.call_id, type="function", function=dict(
            name=item.name, arguments=item.arguments)) for item in response.output if item.type == "function_call"]
        result.reasoning = "\n".join(part.text for item in response.output if item.type == "reasoning"
                                    for part in item.summary if part.type == "summary_text")
        if response.status == "incomplete":
            reason = getattr(response.incomplete_details, "reason", None)
            result.finish_reason = "length" if reason == "max_output_tokens" else reason or "incomplete"
        elif response.status == "failed":
            raise RuntimeError("OpenAI Responses request failed")
        else:
            result.finish_reason = "tool_calls" if result.tool_calls else "stop"
        return result

    def _client_kwargs(self, timeout):
        return dict(api_key=self.api_key, base_url=self.base_url or None,
                    timeout=timeout, max_retries=0)

    def _do_chat(self, messages, temperature, max_tokens, timeout, model, thinking):
        from openai import OpenAI
        with OpenAI(**self._client_kwargs(timeout)) as client:
            response = client.responses.create(**self._request_kwargs(messages, model, max_tokens, thinking))
        return self._apply_response(ChatResult(), response)

    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        from openai import AsyncOpenAI
        from openai.types.responses import ResponseStreamEvent
        ManagedResponseStream = _managed_async_stream(ResponseStreamEvent)
        result = self.last_result = ChatResult(model=model)
        async def request():
            async with AsyncOpenAI(**self._client_kwargs(timeout)) as client:
                kwargs = self._request_kwargs(messages, model, max_tokens, thinking)
                async with client.responses.with_streaming_response.create(**kwargs, stream=True) as response:
                    stream = await response.parse(to=ManagedResponseStream)
                    final = None
                    async for event in stream:
                        if event.type == "response.output_text.delta":
                            result.content += event.delta
                            publish_reply(result.content)
                        elif event.type == "response.reasoning_summary_text.delta":
                            result.reasoning += event.delta
                        elif event.type in {"response.completed", "response.incomplete", "response.failed"}:
                            final = event.response
                    if final is None:
                        raise RuntimeError("OpenAI response ended before a terminal event")
                    return self._apply_response(result, final)
        try:
            return token.run_async(request)
        except OperationCancelled:
            raise OperationCancelled(result) from None

    def _do_stream(self, messages, temperature, max_tokens, timeout, model, thinking):
        from openai import OpenAI
        result = self.last_result = ChatResult(model=model)
        with OpenAI(**self._client_kwargs(timeout)) as client:
            with client.responses.stream(**self._request_kwargs(messages, model, max_tokens, thinking)) as stream:
                final = None
                for event in stream:
                    if event.type == "response.output_text.delta":
                        result.content += event.delta
                        yield event.delta
                    elif event.type == "response.reasoning_summary_text.delta":
                        result.reasoning += event.delta
                    elif event.type in {"response.completed", "response.incomplete", "response.failed"}:
                        final = event.response
                self._apply_response(result, final if final is not None else stream.get_final_response())
