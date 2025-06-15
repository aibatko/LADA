"""
LADA – Local Agent Driven Assistant  v0.3

Key changes vs v0.2
-------------------
1. **Pure‑router step**
   • The first LLM call can ONLY invoke the `route` tool (no shell/file tools).
   • It returns a JSON with `{"action": "answer"|"hand_off", "answer"?: str}`.
   • If `answer` → we run the *coder* model with the full tool‑palette.
   • If `hand_off` → we enter the orchestrator pathway unchanged.

2. **Clean HISTORY**
   • We only persist *user* messages, *final* assistant replies, and
     human‑readable `[tool_call] …` traces.
   • System prompts, internal routing/orchestration messages, and raw choices
     are **not** appended – preventing prompt echo & confusion.
"""
import json, pathlib, shlex, subprocess, webbrowser
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from openai import OpenAI
import concurrent.futures

# ---------------------------------------------------------------------------
# Flask + SocketIO setup
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")
socketio = SocketIO(app, cors_allowed_origins="*")

# ---------------------------------------------------------------------------
# Conversation persistence helpers
# ---------------------------------------------------------------------------
ROOT_DIR = pathlib.Path(__file__).parent.resolve()
HISTORY_FILE = ROOT_DIR / "history.json"
HISTORY: list[dict] = []  # user/assistant/tool‑log messages only

if HISTORY_FILE.exists():
    try:
        HISTORY[:] = json.loads(HISTORY_FILE.read_text("utf‑8"))
    except Exception:
        HISTORY_FILE.write_text("[]", "utf‑8")


def _append(role: str, content: str):
    """Add a user/assistant entry to in‑mem history *and* disk."""
    HISTORY.append({"role": role, "content": content})
    HISTORY_FILE.write_text(json.dumps(HISTORY, ensure_ascii=False, indent=2), "utf‑8")


# ---------------------------------------------------------------------------
# OpenAI helpers
# ---------------------------------------------------------------------------

def get_client(provider: str) -> OpenAI:
    if provider.lower() == "ollama":
        return OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
    return OpenAI()


# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------

def within_root(path: pathlib.Path) -> bool:
    """Return True if *path* is inside the project root (no traversal)."""
    try:
        path.resolve(strict=False).relative_to(ROOT_DIR)
        return True
    except ValueError:
        return False


def token_is_path(token: str) -> bool:
    if token.startswith("-"):
        return False
    return token.startswith(("./", "/", "~/")) or "/" in token


def run_cmd(command: str) -> str:
    tokens = shlex.split(command)
    for t in tokens:
        if token_is_path(t):
            p = pathlib.Path(t).expanduser()
            if not within_root(p):
                return "Blocked: path outside working directory."
    try:
        res = subprocess.run(tokens, capture_output=True, text=True, timeout=30)
        return (res.stdout or "") + (res.stderr or "")
    except Exception as exc:
        return f"Command error: {exc}"


# ---------------------------------------------------------------------------
# Tool definitions shared by coder/orchestrator agents
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the project tree",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Return the contents of a text file",
            "parameters": {
                "type": "object",
                "properties": {"filename": {"type": "string"}},
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_command",
            "description": "Execute a Unix command and return stdout+stderr",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_file",
            "description": "Apply a unified‑diff patch to a file (git‑apply)",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "patch": {"type": "string"},
                },
                "required": ["filename", "patch"],
            },
        },
    },
]

# Map tool‑names → callables --------------------------------------------------

def write_file(filename: str, content: str) -> str:
    p = pathlib.Path(filename).expanduser()
    if not within_root(p):
        return "Blocked: path outside working directory."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf‑8")
    return f"Wrote {p} ({len(content)} bytes)."


def read_file(filename: str) -> str:
    p = pathlib.Path(filename).expanduser()
    if not within_root(p):
        return "Blocked: path outside working directory."
    return p.read_text("utf‑8") if p.exists() else f"{p} not found."


def change_file(filename: str, patch: str) -> str:
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
            return f"Patch failed:\n{res.stderr}"
        return f"Patch applied to {path}"
    except Exception as exc:
        return f"Error applying patch: {exc}"


TOOL_FUNCS = {
    "write_file": write_file,
    "read_file": read_file,
    "write_command": run_cmd,
    "change_file": change_file,
}


# ---------------------------------------------------------------------------
# Router: single "route" tool only
# ---------------------------------------------------------------------------
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["answer", "hand_off"],
            "description": "'answer' → handle locally, 'hand_off' → send to orchestrator",
        },
        "answer": {
            "type": "string",
            "description": "Optional direct answer if action=='answer' and no tool is required.",
        },
    },
    "required": ["action"],
}

DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": "route",
        "description": "High‑level routing decision for the assistant.",
        "parameters": DECISION_SCHEMA,
    },
}

ROUTER_SYS = (
    "You are a lightweight **router**. Decide whether the last user message can "
    "be satisfied quickly by a coder agent with local tools (`answer`) or needs "
    "a multi‑step orchestrator (`hand_off`). Respond ONLY by calling the `route` "
    "function. If you pick `answer`, you may include a natural‑language draft in "
    "the `answer` field; the coder agent will refine it or use tools as needed."
)

CODER_SYS = (
    "You are a focused coding assistant. You have read/write access **only** to "
    "the project folder and can run shell commands via `write_command`. Answer "
    "the user request or execute precisely the tool calls needed."
)

# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    user_msg: str = data["prompt"]
    workers = int(data.get("workers", 2))

    # Model / provider selections ---------------------------
    coder_client = get_client(data["coder_provider"])
    orc_client = get_client(data["orc_provider"])
    coder_model = data["coder_model"]
    orc_model = data["orchestrator_model"]

    # Log user message first --------------------------------
    _append("user", user_msg)

    # ---------------- Step 1: ROUTER -----------------------
    route_resp = coder_client.chat.completions.create(
        model=coder_model,
        messages=[
            {"role": "system", "content": ROUTER_SYS},
            {"role": "user", "content": user_msg},
        ],
        tools=[DECISION_TOOL],
        tool_choice={"type": "function", "function": {"name": "route"}},
    )

    tool_call = route_resp.choices[0].message.tool_calls[0]
    route_args = json.loads(tool_call.function.arguments or "{}")
    action = route_args.get("action")

    # ---------------- Step 2A: handle locally --------------
    if action == "answer":
        # Run the coder agent with the full tool set. We seed it with the draft
        draft = route_args.get("answer", "")

        reply, tool_runs = _coder_execute(
            coder_client, coder_model, user_msg, draft
        )
        return jsonify(
            {
                "plans": [],
                "coder": {"reply": reply, "tool_runs": tool_runs},
                "orchestrator": None,
                "agents": [],
            }
        )

    # ---------------- Step 2B: hand off to orchestrator ----
    orc_payload = _orchestrate(
        orc_client, orc_model, coder_client, coder_model, workers, user_msg
    )
    return jsonify(orc_payload)


# ---------------------------------------------------------------------------
# Helper: run coder with tools until final answer
# ---------------------------------------------------------------------------

def _coder_execute(client: OpenAI, model: str, user_msg: str, draft: str):
    """Loop coder agent until it stops calling tools. Returns (reply, runs)."""
    msgs = [
        {"role": "system", "content": CODER_SYS},
        *HISTORY,  # only clean history (user / assistant / tool_call traces)
        {"role": "assistant", "content": draft} if draft else None,
        {"role": "user", "content": user_msg},
    ]
    msgs = [m for m in msgs if m]  # strip Nones

    tool_runs = []
    while True:
        r = client.chat.completions.create(
            model=model,
            messages=msgs,
            tools=TOOLS,
            tool_choice="auto",
        )
        choice = r.choices[0]
        if choice.finish_reason == "tool_calls":
            for call in choice.message.tool_calls:
                args = json.loads(call.function.arguments or "{}")
                result = TOOL_FUNCS[call.function.name](**args)
                tool_runs.append({"cmd": call.function.name, "result": result})
                # Append trace for UI / future context
                _append("assistant", f"[tool_call] {call.function.name} {args}")
            # reflect tool output back to the model
            msgs.append(
                {
                    "role": "assistant",
                    "tool_calls": [tc.model_dump(exclude_none=True) for tc in choice.message.tool_calls],
                }
            )
            for tc, run in zip(choice.message.tool_calls, tool_runs[-len(choice.message.tool_calls):]):
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": run["result"],
                    }
                )
            continue  # another round
        # Final natural‑language answer
        reply = choice.message.content.strip()
        _append("assistant", reply)
        return reply, tool_runs


# ---------------------------------------------------------------------------
# Helper: orchestrator flow (unchanged logic, cleaned history)
# ---------------------------------------------------------------------------

def _orchestrate(
    orc_client: OpenAI,
    orc_model: str,
    coder_client: OpenAI,
    coder_model: str,
    workers: int,
    user_msg: str,
):
    """Reduced copy of the v0.2 orchestrator but without polluting HISTORY."""

    # --- Plan‑making tool
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

    plan_tool = {
        "type": "function",
        "function": {
            "name": "make_plan",
            "description": "Create a parallelisable plan for the request.",
            "parameters": plan_schema,
        },
    }

    planner_sys = (
        "You are an **orchestrator**. You may spawn up to %d parallel coder "
        "agents in this iteration. Return a JSON plan ONLY via the make_plan "
        "tool."
    ) % workers

    orc_msgs = [
        {"role": "system", "content": planner_sys},
        *HISTORY,
        {"role": "user", "content": user_msg},
    ]

    all_plans, all_agents, orc_tool_runs, round_no = [], [], [], 0

    def run_agent(aid: int, tasks: list[str]):
        agent_sys = (
            f"You are coder agent {aid}. Complete ONLY these tasks one by one:\n" +
            "\n".join(f"- {t}" for t in tasks)
        )
        msgs = [{"role": "system", "content": agent_sys}]
        t_runs = []
        while True:
            r = coder_client.chat.completions.create(
                model=coder_model,
                messages=msgs,
                tools=TOOLS,
                tool_choice="auto",
            )
            ch = r.choices[0]
            if ch.finish_reason == "tool_calls":
                for call in ch.message.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    res = TOOL_FUNCS[call.function.name](**args)
                    t_runs.append({"cmd": call.function.name, "result": res})
                    msgs.append({"role": "assistant", "tool_calls": [call.model_dump(exclude_none=True)]})
                    msgs.append({"role": "tool", "tool_call_id": call.id, "name": call.function.name, "content": res})
                continue
            return {"id": aid, "reply": ch.message.content.strip(), "tool_runs": t_runs}

    while True:
        resp = orc_client.chat.completions.create(
            model=orc_model,
            messages=orc_msgs,
            tools=[plan_tool] + TOOLS,
            tool_choice="auto",
        )
        round_no += 1
        ch = resp.choices[0]
        if ch.finish_reason == "tool_calls":
            orc_msgs.append({"role": "assistant", "tool_calls": [c.model_dump(exclude_none=True) for c in ch.message.tool_calls]})
            for call in ch.message.tool_calls:
                if call.function.name == "make_plan":
                    plan_text = call.function.arguments or "{}"
                    plan = json.loads(plan_text)
                    all_plans.append(plan_text)
                    socketio.emit("plan", {"plan": plan_text, "round": round_no})
                    # Distribute tasks --------------------------------------------------
                    num_agents = min(int(plan.get("agents", 0)), workers)
                    if num_agents > 0 and plan.get("tasks"):
                        tasks_by_agent = {i: [] for i in range(1, num_agents + 1)}
                        for t in plan["tasks"]:
                            tasks_by_agent.setdefault(int(t["agent"]), []).append(t["desc"])
                        with concurrent.futures.ThreadPoolExecutor(max_workers=num_agents) as ex:
                            futs = [ex.submit(run_agent, aid, ts) for aid, ts in tasks_by_agent.items() if ts]
                            results = [f.result() for f in futs]
                        for res in results:
                            all_agents.append(res)
                        # Feed back summary to orchestrator
                        summary = "\n".join(f"Agent {r['id']}: {r['reply']}" for r in results)
                        orc_msgs.append({"role": "user", "content": summary})
                    continue  # orchestrator plans another round if needed
                else:
                    args = json.loads(call.function.arguments or "{}")
                    res = TOOL_FUNCS[call.function.name](**args)
                    orc_tool_runs.append({"cmd": call.function.name, "result": res})
                    orc_msgs.append({"role": "tool", "tool_call_id": call.id, "name": call.function.name, "content": res})
            continue
        # Orchestrator produced a final answer -----------------------------
        final_reply = ch.message.content.strip()
        _append("assistant", final_reply)
        break

    return {
        "plans": all_plans,
        "orchestrator": {"reply": final_reply, "tool_runs": orc_tool_runs},
        "agents": all_agents,
    }


# ---------------------------------------------------------------------------
# Terminal endpoint (unchanged)
# ---------------------------------------------------------------------------
@app.route("/api/command", methods=["POST"])
def terminal():
    cmd = request.json["command"]
    out = run_cmd(cmd)
    _append("assistant", f"[tool_call] shell {{'command': '{cmd}'}}")
    _append("assistant", f"[tool_call] shell_result {{'result': '{out}'}}")
    return jsonify({"cmd": cmd, "result": out})


# ---------------------------------------------------------------------------
# Misc routes / main
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/history")
def history():
    return jsonify(HISTORY)


if __name__ == "__main__":
    try:
        webbrowser.open("http://127.0.0.1:5000")
    except Exception:
        pass
    socketio.run(app, debug=True)

