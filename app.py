"""
LADA – Local Agent Driven Assistant  v0.2
"""
import os, json, pathlib, subprocess, webbrowser, datetime, shlex, tempfile
import threading
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import concurrent.futures
from openai import OpenAI  # new 1.x import

app = Flask(__name__, static_folder="static", template_folder="templates")
socketio = SocketIO(app, cors_allowed_origins="*")

HISTORY_FILE = "../history.json"
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
_hist_lock = threading.Lock()  # lock for HISTORY access
def add_history(role: str, content: str) -> None:
    """Thread-safe append without system/LLM scaffolding."""
    with _hist_lock:
        HISTORY.append({"role": role, "content": content})
# ---------- conversation-logging helpers ---------- #
def log_tool_call(name: str, args: dict) -> None:
    """
    Append a readable trace of a tool/command invocation to HISTORY
    so the frontend can display it in-line with the chat.
    """
    # HISTORY.append({
    #     "role": "assistant",
    #     "content": f"[tool_call] {name} {json.dumps(args, ensure_ascii=False)}"
    # })
    add_history("assistant", f"[tool_call] {name} {json.dumps(args, ensure_ascii=False)}")

def flush_history_to_disk() -> None:
    """Persist the in-memory HISTORY to history.json."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(HISTORY, f, ensure_ascii=False, indent=2)
# ---------- helpers ---------- #
def get_client(provider: str, token: str | None = None):
    if provider.lower() == "ollama":
        return OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
    if token:
        return OpenAI(api_key=token)
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
        "description": "Execute a Unix command and see the result",
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

# ---------- router JSON-schema ---------- #
# Let the LLM decide in one structured call whether to answer directly
# or escalate to the orchestrator, and (if answering) what to say.
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["answer", "hand_off"],
            "description": "Choose 'answer' to respond immediately, or 'hand_off' to hand off the task to the bigger model."
        },
        "answer": {
            "type": "string",
            "description": "Natural-language reply to the user if action == 'answer'."
        }
    },
    "required": ["action"]
}

DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": "route",
        "description": "High-level routing decision for the assistant.",
        "parameters": DECISION_SCHEMA,
    },
}


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
    orc_provider   = data["orc_provider"]
    coder_provider = data["coder_provider"]
    orc_model  = data["orchestrator_model"]
    coder_model= data["coder_model"]
    workers    = int(data.get("workers", 2))
    orc_enabled = data.get("orc_enabled", True)
    api_token   = data.get("api_token")
    user_msg   = data["prompt"]

    orc_client   = get_client(orc_provider, api_token)
    coder_client = get_client(coder_provider, api_token)


    # HISTORY.append({"role": "user", "content": user_msg})
    add_history("user", user_msg)

    # router agent 
    # ----- quick check with coder -----
    # coder_sys = (
    #     "You are a quick answering **router**. "
    #     "If the user's last message can be answered quickly or requires minimal command use, call the `route` function with "
    #     "`{\"action\":\"answer\",\"answer\":\"…\"}`. "
    #     "Otherwise if the task requires careful planning, multiple steps or actions call `route` with `{\"action\":\"hand_off\"}` to hand off the task to a bigger model with more tools and resources."
    # )


    # def quick_coder():
    #     msgs = [{"role": "system", "content": coder_sys}] + HISTORY
    #     t_runs = []
    #     while True:
    #         # r = coder_client.chat.completions.create(model=coder_model, messages=msgs, tools=TOOLS, tool_choice="auto")
    #         r = coder_client.chat.completions.create(
    #             model=coder_model,
    #             messages=msgs,
    #             tools=TOOLS + [DECISION_TOOL],
    #             tool_choice="auto",
    #         )
    #         c = r.choices[0]
    #         if c.finish_reason == "tool_calls":
    #             msgs.append({"role": "assistant", "tool_calls": [tc.model_dump(exclude_none=True) for tc in c.message.tool_calls]})
    #             for a in c.message.tool_calls:
    #                 a_args = json.loads(a.function.arguments or "{}")
    #                 log_tool_call(a.function.name, a_args)
    #                 # res = TOOL_FUNCS[a.function.name](**a_args)
    #                 # label = a_args.get("command") if a.function.name == "write_command" else a.function.name
    #                 # t_runs.append({"cmd": label, "result": res})
    #                 # msgs.append({"role": "tool", "tool_call_id": a.id, "name": label, "content": res})
    #                 # New structured decision
    #                 if a.function.name == "route":
    #                     msgs.append({"role": "tool", "tool_call_id": a.id, "name": "route", "content": json.dumps(a_args)})
    #                     if a_args.get("action") == "answer":
    #                         return a_args.get("answer", "").strip(), t_runs, msgs
    #                     else:  # 'orchestrate'
    #                         return "ORCHESTRATE", t_runs, msgs
    #
    #                 # Existing file/command tools stay unchanged
    #                 res = TOOL_FUNCS[a.function.name](**a_args)
    #                 label = a_args.get("command") if a.function.name == "write_command" else a.function.name
    #                 t_runs.append({"cmd": label, "result": res})
    #                 msgs.append({"role": "tool", "tool_call_id": a.id, "name": label, "content": res})
    #
    #             continue
    #         msgs.append({"role": "assistant", "content": c.message.content})
    #         return c.message.content.strip(), t_runs, msgs
    #
    # decision, coder_runs, coder_msgs = quick_coder()
    # print("Router decision:", decision, "Coder runs:", coder_runs)
    # if decision.upper() != "ORCHESTRATE":
    #     HISTORY.extend(coder_msgs)
    #     # with open(HISTORY_FILE, "w", encoding="utf-8") as f:
    #     #     json.dump(HISTORY, f, ensure_ascii=False, indent=2)
    #     flush_history_to_disk()
    #     return jsonify({"plans": [], "coder": {"reply": decision, "tool_runs": coder_runs}, "orchestrator": None, "agents": []})
    # ---------------- Router (decision-only) ---------------- #
    router_sys = (
        "You are a routing assistant. Decide **only** whether the last user "
        "message should be handled directly by the lightweight coder model "
        "(`answer`) or forwarded to the orchestrator (`hand_off`). "
        "Return the decision by calling the `route` function and nothing else."
    )

    router_messages = [{"role": "system", "content": router_sys}] + HISTORY[-6:]
    router_resp = coder_client.chat.completions.create(
        model=coder_model,
        messages=router_messages,
        tools=[DECISION_TOOL],
        tool_choice={"type": "function", "function": {"name": "route"}},
    )
    print("Router response:", router_resp)
    router_call = router_resp.choices[0].message.tool_calls[0]
    decision_args = json.loads(router_call.function.arguments or "{}")
    decision = decision_args.get("action", "hand_off")


    # If the lightweight coder should answer immediately or the orchestrator is disabled
    if decision == "answer" or not orc_enabled:
        target_client = coder_client if decision == "answer" else orc_client
        target_model = coder_model if decision == "answer" else orc_model
        coder_messages = (
            [{"role": "system", "content": "You are a helpful coding assistant."}]
            + HISTORY
        )
        coder_tool_runs = []
        while True:
            c_resp = target_client.chat.completions.create(
                model=target_model,
                messages=coder_messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            c_choice = c_resp.choices[0]
            if c_choice.finish_reason == "tool_calls":
                for tc in c_choice.message.tool_calls:
                    t_args = json.loads(tc.function.arguments or "{}")
                    log_tool_call(tc.function.name, t_args)
                    res = TOOL_FUNCS[tc.function.name](**t_args)
                    label = (
                        t_args.get("command")
                        if tc.function.name == "write_command"
                        else tc.function.name
                    )
                    coder_tool_runs.append({"cmd": label, "result": res})
                    coder_messages.extend(
                        [
                            {"role": "assistant", "tool_calls": [tc.model_dump(exclude_none=True)]},
                            {"role": "tool", "tool_call_id": tc.id, "name": label, "content": res},
                        ]
                    )
                continue
            final_answer = c_choice.message.content.strip()
            break

        add_history("assistant", final_answer)
        flush_history_to_disk()
        if decision == "answer":
            return jsonify(
                {
                    "plans": [],
                    "coder": {"reply": final_answer, "tool_runs": coder_tool_runs},
                    "orchestrator": None,
                    "agents": [],
                }
            )
        else:
            return jsonify(
                {
                    "plans": [],
                    "coder": None,
                    "orchestrator": {"reply": final_answer, "tool_runs": coder_tool_runs},
                    "agents": [],
                }
            )
    # ----- ask orchestrator for a plan -----
    planner_sys = (
        # "You are an orchestrator. Coder agents are independent and share no "
        # "memory. Each agent only sees its own task list. You have up to %d "
        # "workers available and must never exceed this number. "
        # "When assigning "
        # "tasks do not rely on one agent continuing work of another unless you "
        # "explicitly provide the previous results. Respond ONLY with JSON like: "
        # "{\"agents\":N,\"tasks\":[{\"agent\":1,\"desc\":\"task\"}]}"
        " You are a code super agent and have the ability to orchestrate multiple smaller agents. "
        " Your overall job is to guide the process and assign super specific tasks to smaller agents. "
        " You can do this by assigning tasks to individual agents or you can execute commands on your own (the smaller agents can also execute the same commands like writing, reading and chaning files). "
        " Before creating smaller agents, create a detailed plan for everything that needs to be done. "
        " Right now you can have up to %d workers for 1 iteration. "
        " When you spawn a new agent it has no memory of previous tasks so you should give it a detailed prompt and list what it needs to do. "
        " Your agents work in parallel and can execute tasks independently but won't be able to work on the same file. "
        " You also have the ability to execute more iterations after one is compelte - if a process requires more steps than your available workers or needs something to be done in sequence like writing a file then reading it, you can do that by creating more agents after you got feedback from the previous ones.\n\n "
        "When assigning tasks do not rely on one agent continuing work of another unless you "
        "explicitly provide the previous results. Respond ONLY with JSON like: "
        "{\"agents\":N,\"tasks\":[{\"agent\":1,\"desc\":\"task\"}]}"
        " When one a iteration is over and you have the results from all agents and think that the process is complete, report to the user with summary of what has been done. "
        " This is the history of the conversation so far: \n"
    ) % workers
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
            "description": "Return a plan for the requested tasks.",
            "parameters": plan_schema,
        },
    }
    orc_messages = [{"role": "system", "content": planner_sys}] + HISTORY
    orc_tool_runs: list[dict] = []
    final_reply = ""
    all_plans: list[str] = []
    all_agents: list[dict] = []
    round_no = 0

    def run_agent(aid: int, tasks: list[str]):
        msgs = [{"role": "system", "content": "You are coder agent %d. Complete ONLY the following tasks in order:\n%s" % (aid, "\n".join(f"- {t}" for t in tasks))}]
        t_runs = []
        while True:
            r = coder_client.chat.completions.create(model=coder_model, messages=msgs, tools=TOOLS, tool_choice="auto")
            c = r.choices[0]
            if c.finish_reason == "tool_calls":
                for a in c.message.tool_calls:
                    a_args = json.loads(a.function.arguments or "{}")
                    log_tool_call(a.function.name, a_args)
                    res = TOOL_FUNCS[a.function.name](**a_args)
                    label = a_args.get("command") if a.function.name == "write_command" else a.function.name
                    t_runs.append({"cmd": label, "result": res})
                    msgs.append({"role": "assistant", "tool_calls": [a.model_dump(exclude_none=True)]})
                    msgs.append({"role": "tool", "tool_call_id": a.id, "name": label, "content": res})
                continue
            msgs.append({"role": "assistant", "content": c.message.content})
            return {"id": aid, "reply": c.message.content, "tool_runs": t_runs, "messages": msgs, "round": round_no}


    while True:
        print("\n\n\n")
        print(f"Round {round_no} messages: {orc_messages}")
        resp = orc_client.chat.completions.create(
            model=orc_model,
            messages=orc_messages,
            tools=TOOLS + [plan_tool],
            tool_choice="auto",
        )
        print("\n\n\n")
        print(f"Round {round_no} response: {resp}")
        print("\n\n\n")
        plan_text = "{}"
        plan = {"agents": 0, "tasks": []}
        choice = resp.choices[0]
        round_no += 1

        if choice.finish_reason == "tool_calls":
            orc_messages.append({"role": "assistant", "tool_calls": [c.model_dump(exclude_none=True) for c in choice.message.tool_calls]})
            for call in choice.message.tool_calls:
                args = json.loads(call.function.arguments or "{}")
                log_tool_call(call.function.name, args)
                if call.function.name == "make_plan":
                    plan_text = call.function.arguments or "{}"
                    orc_messages.append({"role": "tool", "tool_call_id": call.id, "name": "make_plan", "content": plan_text})
                    try:
                        plan = json.loads(plan_text)
                    except Exception:
                        plan = {"agents": 0, "tasks": []}
                    all_plans.append(plan_text)
                    socketio.emit('plan', {'plan': plan_text, 'round': round_no})
                    if plan.get("tasks") and plan.get("agents", 0) > 0:
                        num_agents = min(int(plan.get("agents", 1)), workers)
                        agent_tasks = {i: [] for i in range(1, num_agents + 1)}
                        for t in plan.get("tasks", []):
                            aid = int(t.get("agent", 1))
                            if aid not in agent_tasks:
                                aid = 1
                            agent_tasks[aid].append(t.get("desc", ""))
                        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                            futs = [ex.submit(run_agent, aid, tasks) for aid, tasks in agent_tasks.items() if tasks]
                            results = [f.result() for f in futs]
                        for r in results:
                            all_agents.append(r)
                            # HISTORY.extend(r["messages"])
                            add_history("assistant", r["reply"])
                            socketio.emit('agent_result', {
                                'id': r['id'], 'reply': r['reply'],
                                'tool_runs': r['tool_runs'], 'round': r['round']
                            })
                        summary = "\n".join(f"Agent {r['id']} result: {r['reply']}" for r in results)
                        orc_messages.append({"role": "user", "content": summary})
                    continue
                else:
                    res = TOOL_FUNCS[call.function.name](**args)
                    label = args.get("command") if call.function.name == "write_command" else call.function.name
                    orc_tool_runs.append({"cmd": label, "result": res})
                    orc_messages.append({"role": "tool", "tool_call_id": call.id, "name": label, "content": res})
            continue

        text = choice.message.content or ""
        try:
            plan = json.loads(text)
        except Exception:
            plan = None

        if isinstance(plan, dict) and "tasks" in plan and "agents" in plan:
            all_plans.append(text)
            socketio.emit('plan', {'plan': text, 'round': round_no})
            if plan.get("tasks") and plan.get("agents", 0) > 0:
                num_agents = min(int(plan.get("agents", 1)), workers)
                agent_tasks = {i: [] for i in range(1, num_agents + 1)}
                for t in plan.get("tasks", []):
                    aid = int(t.get("agent", 1))
                    if aid not in agent_tasks:
                        aid = 1
                    agent_tasks[aid].append(t.get("desc", ""))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = [ex.submit(run_agent, aid, tasks) for aid, tasks in agent_tasks.items() if tasks]
                    results = [f.result() for f in futs]
                for r in results:
                    all_agents.append(r)
                    # HISTORY.extend(r["messages"])
                    add_history("assistant", r["reply"])
                    socketio.emit('agent_result', {
                        'id': r['id'], 'reply': r['reply'],
                        'tool_runs': r['tool_runs'], 'round': r['round']
                    })
                summary = "\n".join(f"Agent {r['id']} result: {r['reply']}" for r in results)
                orc_messages.append({"role": "user", "content": summary})
                continue
            else:
                break

        final_reply = text
        orc_messages.append({"role": "assistant", "content": text})
        break

    # HISTORY.append({"role": "assistant", "content": "\n".join(all_plans)})
    add_history("assistant", "\n".join(all_plans))
    if final_reply:
        # HISTORY.append({"role": "assistant", "content": final_reply})
        add_history("assistant", final_reply)
    # with open(HISTORY_FILE, "w", encoding="utf-8") as f:
    #     json.dump(HISTORY, f, ensure_ascii=False, indent=2)
    flush_history_to_disk()

    return jsonify({
        "plans": all_plans,
        "orchestrator": {"reply": final_reply, "tool_runs": orc_tool_runs},
        "agents": [{"id": a["id"], "reply": a["reply"], "tool_runs": a["tool_runs"], "round": a["round"]} for a in all_agents]
    })

@app.route("/api/command", methods=["POST"])
def terminal():
    cmd   = request.json["command"]
    out   = run_cmd(cmd)
    log_tool_call("shell", {"command": cmd})
    log_tool_call("shell_result", {"result": out})
    flush_history_to_disk()
    return jsonify({"cmd": cmd, "result": out})

# ---------- main ---------- #
if __name__ == "__main__":
    webbrowser.open("http://127.0.0.1:5000")  # auto-open browser
    socketio.run(app, debug=True)

