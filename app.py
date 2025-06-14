"""
LADA – Local Agent Driven Assistant  v0.2
"""
import os, re, json, pathlib, subprocess, webbrowser, datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from openai import OpenAI  # new 1.x import

app = Flask(__name__, static_folder="static", template_folder="templates")
socketio = SocketIO(app, cors_allowed_origins="*")

HISTORY_FILE = "history.json"
USE_SESSION_HISTORY = False  
if USE_SESSION_HISTORY:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            HISTORY: list[dict] = json.load(f)
    except FileNotFoundError:
        HISTORY = []
else:
    # Erase history file on start if not using session history
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("[]")
    HISTORY = []
# ---------- helpers ---------- #
def get_client(provider: str):
    if provider.lower() == "ollama":
        return OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
    return OpenAI()

SAFE_CMD = re.compile(r"^[a-zA-Z0-9_\-./]+$")  # naïve allow-list; extend for prod

def run_cmd(command: str) -> str:
    if not SAFE_CMD.match(command):
        return "Blocked: unsafe characters in command."
    try:
        res = subprocess.run(command.split(),
                             capture_output=True,
                             text=True,
                             timeout=30)
        return (res.stdout or "") + (res.stderr or "")
    except Exception as exc:
        return f"Command error: {exc}"

# ---------- OpenAI tools ---------- #
TOOLS = [
    {
      "type": "function",
      "function": {
        "name": "write_file",
        "description": "Create or overwrite a file",
        "parameters": {
          "type": "object",
          "properties": {
            "filename": {"type": "string"},
            "content":  {"type": "string"}
          },
          "required": ["filename","content"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "Return the contents of a text file",
        "parameters": {
          "type": "object",
          "properties": { "filename": {"type": "string"} },
          "required": ["filename"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "write_command",
        "description": "Execute a Unix command and capture stdout/stderr",
        "parameters": {
          "type": "object",
          "properties": { "command": {"type": "string"} },
          "required": ["command"]
        }
      }
    }
]

def write_file(filename, content):       # ↙ simple helpers
    path = pathlib.Path(filename).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"Wrote {path} ({len(content)} bytes)."

def read_file(filename):
    p = pathlib.Path(filename).expanduser()
    return p.read_text() if p.exists() else f"{p} not found."

# map tool names to callables
TOOL_FUNCS = {"write_file": write_file, "read_file": read_file, "write_command": run_cmd}

# ---------- routes ---------- #
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/chat", methods=["POST"])
def chat():
    data      = request.json
    provider  = data["provider"]
    model     = data["model"]
    messages = HISTORY.copy()                # start with chat history
    messages.append({"role": "user", "content": data["prompt"]})
    client    = get_client(provider)
    tool_runs = []                              # collected command outputs for UI

    while True:  # 🚀 loop until model stops calling tools
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, tool_choice="auto"
        )
        choice = resp.choices[0]

        if choice.finish_reason == "tool_calls":
            for call in choice.message.tool_calls:
                args = json.loads(call.function.arguments or "{}")
                result = TOOL_FUNCS[call.function.name](**args)
                tool_runs.append({"cmd": call.function.name, "result": result})

                # add tool call and result to the conversation history
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
                        "name": call.function.name,
                        "content": result,
                    }
                )
            continue  # ask again with new evidence
        break

    # assistant’s final reply
    messages.append({"role":"assistant","content":choice.message.content})

    # persist history
    HISTORY.clear(); HISTORY.extend(messages)
    with open(HISTORY_FILE,"w",encoding="utf-8") as f:
        json.dump(HISTORY,f,ensure_ascii=False,indent=2)

    return jsonify({"reply": choice.message.content,
                    "tool_runs": tool_runs})

@app.route("/api/command", methods=["POST"])
def terminal():
    cmd   = request.json["command"]
    out   = run_cmd(cmd)
    return jsonify({"cmd": cmd, "result": out})

# ---------- main ---------- #
if __name__ == "__main__":
    webbrowser.open("http://127.0.0.1:5000")  # auto-open browser
    socketio.run(app, debug=True)

