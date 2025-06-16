import json
from types import SimpleNamespace
import app
from app import socketio, run_chat_logic

class DummyToolCall:
    def __init__(self, name, arguments, id="1"):
        self.id = id
        self.function = SimpleNamespace(name=name, arguments=arguments)
    def model_dump(self, exclude_none=True):
        return {"id": self.id, "function": {"name": self.function.name, "arguments": self.function.arguments}}

class DummyChoice:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        self.message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
        self.finish_reason = finish_reason

class DummyResponse:
    def __init__(self, choice):
        self.choices = [choice]

class DummyCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
    def create(self, *args, **kwargs):
        return self._responses.pop(0)

class DummyChat:
    def __init__(self, responses):
        self.completions = DummyCompletions(responses)

class DummyClient:
    def __init__(self, responses):
        self.chat = DummyChat(responses)


def test_orc_tool_event(monkeypatch):
    router_resp = DummyResponse(DummyChoice(None, [DummyToolCall('route', json.dumps({'action':'hand_off'}))], 'tool_calls'))
    orc_resp1 = DummyResponse(DummyChoice(None, [DummyToolCall('write_command', json.dumps({'command':'echo hi'}))], 'tool_calls'))
    orc_resp2 = DummyResponse(DummyChoice("done", [], 'stop'))

    coder_client = DummyClient([router_resp])
    orc_client = DummyClient([orc_resp1, orc_resp2])

    def fake_get_client(provider: str):
        if provider == 'orc':
            return orc_client
        return coder_client

    monkeypatch.setattr(app, 'get_client', fake_get_client)
    test_client = socketio.test_client(app.app)

    data = {
        'prompt': 'hi',
        'orc_provider': 'orc',
        'coder_provider': 'coder',
        'orchestrator_model': 'm',
        'coder_model': 'c',
        'workers': 1,
        'orc_enabled': True,
    }
    run_chat_logic(data)

    events = test_client.get_received()
    names = [e['name'] for e in events]
    assert 'orc_tool' in names
