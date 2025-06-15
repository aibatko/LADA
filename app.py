"""
LADA – Local Agent Driven Assistant  v0.2
"""
import os, json, pathlib, subprocess, webbrowser, datetime, shlex, tempfile
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import concurrent.futures
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

ROOT_DIR = pathlib.Path.cwd().resolve()

def within_root(path: pathlib.Path) -> bool:
    """Return True if *path* is within the starting directory."""
    try:
        path.resolve(strict=False).relative_to(ROOT_DIR)
        return True
    except ValueError:
        return False

def token_is_path(token: str) -> bool:
    if token.startswith("-"):
        return False
    return token.startswith(('.', '/', '~')) or '/' in token

def run_cmd(command: str) -> str:
    tokens = shlex.split(command)
    for t in tokens:
        if token_is_path(t):
            p = pathlib.Path(t).expanduser()
            if not within_root(p):
                return "Blocked: path outside working directory."
    try:
        res = subprocess.run(tokens,
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
    },
    {
      "type": "function",
      "function": {
        "name": "change_file",
        "description": "Apply a git patch to a file. Call read_file first to get the current content and then send a unified diff patch.",
        "parameters": {
          "type": "object",
          "properties": {
            "filename": {"type": "string"},
            "patch": {"type": "string"}
          },
          "required": ["filename", "patch"]
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

def change_file(filename: str, patch: str):
    """Apply a git patch to *filename* and return result."""
    path = pathlib.Path(filename).expanduser()
    if not within_root(path):
        return "Blocked: path outside working directory."
    try:
        res = subprocess.run(
            ["git", "apply", "-"],
            input=patch,
            text=True,
            capture_output=True,
            cwd=ROOT_DIR,
        )
        if res.returncode != 0:
            content = path.read_text() if path.exists() else ""
            return f"Patch failed:\n{res.stderr}\nCurrent file:\n{content}"
        return f"Patch applied to {path}."
    except Exception as exc:
        return f"Error applying patch: {exc}"

# map tool names to callables
TOOL_FUNCS = {
    "write_file": write_file,
    "read_file": read_file,
    "write_command": run_cmd,
    "change_file": change_file,
}

# ---------- routes ---------- #
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/history")
def history():
    """Return full conversation history."""
    return jsonify(HISTORY)

@app.route("/api/chat", methods=["POST"])
def chat():
    data       = request.json
    provider   = data["provider"]
    orc_model  = data["orchestrator_model"]
    coder_model= data["coder_model"]
    workers    = int(data.get("workers", 2))
    user_msg   = data["prompt"]

    client = get_client(provider)

    messages = HISTORY.copy()
    messages.append({"role": "user", "content": user_msg})

    HISTORY.append({"role": "user", "content": user_msg})

    # ----- ask orchestrator for a plan -----
    planner_sys = (
        "You are an orchestrator. Break down the user's request into tasks and "
        "assign them to a number of agents not exceeding the provided worker "
        "count. Respond ONLY with JSON like: "
        "{\"agents\":N,\"tasks\":[{\"agent\":1,\"desc\":\"task\"}]}"
    )
    plan_schema = {
        "type": "object",
        "properties": {
            "agents": {"type": "integer"},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "integer"},
                        "desc": {"type": "string"},
                    },
                    "required": ["agent", "desc"],
                },
            },
        },
        "required": ["agents", "tasks"],
    }

    plan_messages = [{"role": "system", "content": planner_sys}] + messages
    plan_tool = {
        "type": "function",
        "function": {
            "name": "make_plan",
            "description": "Return a plan for the requested tasks.",
            "parameters": plan_schema,
        },
    }
    resp = client.chat.completions.create(
        model=orc_model,
        messages=plan_messages,
        tools=[plan_tool],
        tool_choice={"type": "function", "function": {"name": "make_plan"}},
    )
    plan_text = "{}"
    plan = {"agents": 1, "tasks": [{"agent": 1, "desc": user_msg}]}
    if resp.choices and resp.choices[0].finish_reason == "tool_calls":
        call = resp.choices[0].message.tool_calls[0]
        plan_text = call.function.arguments or "{}"
        try:
            plan = json.loads(plan_text)
        except Exception:
            plan = {"agents": 1, "tasks": [{"agent": 1, "desc": user_msg}]}
    else:
        try:
            plan_text = resp.choices[0].message.content
            plan = json.loads(plan_text)
        except Exception:
            pass

    num_agents = min(int(plan.get("agents", 1)), workers)
    agent_tasks = {i: [] for i in range(1, num_agents + 1)}
    for t in plan.get("tasks", []):
        aid = int(t.get("agent", 1))
        if aid not in agent_tasks:
            aid = 1
        agent_tasks[aid].append(t.get("desc", ""))

    def run_agent(aid: int, tasks: list[str]):
        msgs = [
            {
                "role": "system",
                "content": "You are coder agent %d. Complete the following tasks in order:\n%s"
                % (aid, "\n".join(f"- {t}" for t in tasks)),
            }
        ] + messages
        t_runs = []
        while True:
            r = client.chat.completions.create(
                model=coder_model, messages=msgs, tools=TOOLS, tool_choice="auto"
            )
            c = r.choices[0]
            if c.finish_reason == "tool_calls":
                for call in c.message.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    result = TOOL_FUNCS[call.function.name](**args)
                    label = (
                        args.get("command")
                        if call.function.name == "write_command"
                        else call.function.name
                    )
                    t_runs.append({"cmd": label, "result": result})
                    msgs.append({"role": "assistant", "tool_calls": [call.model_dump(exclude_none=True)]})
                    msgs.append({"role": "tool", "tool_call_id": call.id, "name": label, "content": result})
                continue
            msgs.append({"role": "assistant", "content": c.message.content})
            return {"id": aid, "reply": c.message.content, "tool_runs": t_runs, "messages": msgs}

    # run agents in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_agent, aid, tasks) for aid, tasks in agent_tasks.items() if tasks]
        results = [f.result() for f in futs]

    # persist history: orchestrator plan + each agent conversation
    HISTORY.append({"role": "assistant", "content": plan_text})
    for r in results:
        HISTORY.extend(r["messages"])
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(HISTORY, f, ensure_ascii=False, indent=2)

    return jsonify({"plan": plan_text, "agents": [{"id": r["id"], "reply": r["reply"], "tool_runs": r["tool_runs"]} for r in results]})

@app.route("/api/command", methods=["POST"])
def terminal():
    cmd   = request.json["command"]
    out   = run_cmd(cmd)
    return jsonify({"cmd": cmd, "result": out})

# ---------- main ---------- #
if __name__ == "__main__":
    webbrowser.open("http://127.0.0.1:5000")  # auto-open browser
    socketio.run(app, debug=True)

