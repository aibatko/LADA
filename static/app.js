const chatPane = document.getElementById("chatPane");
const termPane = document.getElementById("termPane");

async function loadHistory(){
  const r = await fetch("/api/history");
  const hist = await r.json();
  hist.forEach(m=>{
    if(m.role === "user")
      bubble(m.content,"user",chatPane);
    else if(m.role === "assistant" && m.content)
      bubble(m.content,"ai",chatPane);
    else if(m.role === "tool")
      bubble(`$ ${m.name}\n${m.content}`,"code",termPane);
  });
}

loadHistory();

async function post(url, body){
  const r = await fetch(url,{
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify(body)
  });
  return r.json();
}

function bubble(text, cls, pane){
  const d = document.createElement("div");
  d.className = `bubble ${cls}`;
  d.textContent = text;
  pane.append(d);
  /* wait till the element is rendered, then jump to bottom */
  requestAnimationFrame(()=>{ pane.scrollTop = pane.scrollHeight; });
}

/* ---------- CHAT ---------- */
const chatInput  = document.getElementById("chatInput");
async function sendChat(){
  const msg = chatInput.value.trim(); if(!msg) return;
  bubble(msg,"user",chatPane); chatInput.value="";

  const data = await post("/api/chat",{
    prompt:  msg,
    provider: document.getElementById("provider").value,
    orchestrator_model: document.getElementById("orcModel").value,
    coder_model:        document.getElementById("coderModel").value,
    workers: parseInt(document.getElementById("workers").value,10)
  });

  if(data.plan) bubble(data.plan,"ai",chatPane);
  (data.agents||[]).forEach(a=>{
    a.tool_runs.forEach(t=>{
      bubble(`$ ${t.cmd}\n${t.result}`,"code",termPane);
    });
    bubble(`[Agent ${a.id}] ${a.reply}`,"ai",chatPane);
  });
}
document.getElementById("sendChat").onclick = sendChat;
chatInput.addEventListener("keydown", e => {
  if(e.key === "Enter" && !e.shiftKey){
    e.preventDefault();
    sendChat();
  }
});

/* ---------- TERMINAL ---------- */
const cmdInput   = document.getElementById("cmdInput");
async function sendCmd(){
  const cmd = cmdInput.value.trim(); if(!cmd) return;
  bubble(`$ ${cmd}`,"code",termPane); cmdInput.value="";
  const res = await post("/api/command",{ command: cmd });
  bubble(res.result,"code",termPane);
}
document.getElementById("sendCmd").onclick = sendCmd;
cmdInput.addEventListener("keydown", e => {
  if(e.key === "Enter" && !e.shiftKey){
    e.preventDefault();
    sendCmd();
  }
});

