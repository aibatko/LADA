import json
import webbrowser
from flask import render_template, request, jsonify, Flask

from .openai_tools import get_client, TOOLS, TOOL_FUNCS
from .utils import run_cmd

HISTORY_FILE = "history.json"
USE_SESSION_HISTORY = False

if USE_SESSION_HISTORY:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            HISTORY: list[dict] = json.load(f)
    except FileNotFoundError:
        HISTORY = []
else:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("[]")
    HISTORY = []


def register_routes(app: Flask):
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/history")
    def history():
        """Return full conversation history."""
        return jsonify(HISTORY)

    @app.route("/api/chat", methods=["POST"])
    def chat():
        data = request.json
        provider = data["provider"]
        model = data["model"]
        messages = HISTORY.copy()
        messages.append({"role": "user", "content": data["prompt"]})
        client = get_client(provider)
        tool_runs = []

        while True:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=TOOLS, tool_choice="auto"
            )
            choice = resp.choices[0]

            if choice.finish_reason == "tool_calls":
                for call in choice.message.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    result = TOOL_FUNCS[call.function.name](**args)
                    tool_label = (
                        args.get("command")
                        if call.function.name == "write_command"
                        else call.function.name
                    )
                    tool_runs.append({"cmd": tool_label, "result": result})

                    messages.append(
                        {
                            "role": "assistant",
                            "tool_calls": [call.model_dump(exclude_none=True)],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": tool_label,
                            "content": result,
                        }
                    )
                continue
            break

        messages.append({"role": "assistant", "content": choice.message.content})

        HISTORY.clear()
        HISTORY.extend(messages)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(HISTORY, f, ensure_ascii=False, indent=2)

        return jsonify({"reply": choice.message.content, "tool_runs": tool_runs})

    @app.route("/api/command", methods=["POST"])
    def terminal():
        cmd = request.json["command"]
        out = run_cmd(cmd)
        return jsonify({"cmd": cmd, "result": out})


def open_browser():
    webbrowser.open("http://127.0.0.1:5000")
