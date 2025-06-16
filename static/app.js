const chatPane = document.getElementById("chatPane");
const termPane = document.getElementById("termPane");
const socket = window.io ? io() : { on: ()=>{}, emit: ()=>{} };
const shownPlans = new Set();
const shownAgents = new Set();
let orcEnabled = true;
const orcToggle = document.getElementById("orcToggle");
orcToggle.onclick = () => {
  orcEnabled = !orcEnabled;
  orcToggle.textContent = `Orchestrator ${orcEnabled ? 'ON' : 'OFF'}`;
};

socket.on('plan', d => {
  if(!shownPlans.has(d.round)){
    shownPlans.add(d.round);
    showPlan(d.plan, d.round);
  }
});

socket.on('agent_result', a => {
  const key = `${a.round}-${a.id}`;
  if(shownAgents.has(key)) return;
  shownAgents.add(key);
  a.tool_runs.forEach(t => {
    bubble(`[A${a.id}] $ ${t.cmd}\n${t.result}`, 'code', termPane);
  });
  bubble(`[Agent ${a.id}] ${a.reply}`, 'ai', chatPane);
});
socket.on('chat_done', d => {
  // coder section (if decision == "answer")
  if (d.coder) {
    d.coder.tool_runs?.forEach(t =>
      bubble(`[Coder] $ ${t.cmd}\n${t.result}`, 'code', termPane));
    if (d.coder.reply) bubble(`[Coder] ${d.coder.reply}`, 'ai', chatPane);
  }
  // orchestrator summary
  d.orchestrator?.tool_runs?.forEach(t =>
      bubble(`[Orc] $ ${t.cmd}\n${t.result}`, 'code', termPane));
  if (d.orchestrator?.reply)
      bubble(`[Orchestrator] ${d.orchestrator.reply}`, 'orc', chatPane);
  // agent results (repeat filter just in case)
  d.agents?.forEach(a => {
    const key = `${a.round}-${a.id}`; if (shownAgents.has(key)) return;
    shownAgents.add(key);
    a.tool_runs.forEach(t =>
      bubble(`[A${a.id}] $ ${t.cmd}\n${t.result}`, 'code', termPane));
    bubble(`[Agent ${a.id}] ${a.reply}`, 'ai', chatPane);
  });
});

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

function bubble(text, cls, pane, html=false){
  const d = document.createElement("div");
  d.className = `bubble ${cls}`;
  if(html) d.innerHTML = text; else d.textContent = text;
  pane.append(d);
  requestAnimationFrame(()=>{ pane.scrollTop = pane.scrollHeight; });
  return d;
}

function showPlan(planStr, round){
  try{
    const plan = JSON.parse(planStr);
    let html = `<strong>Plan ${round}:</strong><br>Agents: ${plan.agents}<ul>`;

    plan.tasks.forEach(t=>{ html += `<li>[Agent ${t.agent}] ${t.desc}</li>`; });
    html += '</ul>';
    bubble(html,'orc',chatPane,true);
  }catch(e){
    bubble(planStr,'ai',chatPane);
  }
}

/* ---------- CHAT ---------- */
const chatInput  = document.getElementById("chatInput");
async function sendChat(){
  const msg = chatInput.value.trim(); if(!msg) return;
  bubble(msg,"user",chatPane); chatInput.value="";

  // const data = await post("/api/chat",{
  //   prompt:  msg,
  //   orc_provider:   document.getElementById("orcProvider").value,
  //   coder_provider: document.getElementById("coderProvider").value,
  //   orchestrator_model: document.getElementById("orcModel").value,
  //   coder_model:        document.getElementById("coderModel").value,
  //   workers: parseInt(document.getElementById("workers").value,10),
  //   orc_enabled: orcEnabled
  // });
  await post("/api/chat",{
    prompt:  msg,
    orc_provider:   document.getElementById("orcProvider").value,
    coder_provider: document.getElementById("coderProvider").value,
    orchestrator_model: document.getElementById("orcModel").value,
    coder_model:        document.getElementById("coderModel").value,
    workers: parseInt(document.getElementById("workers").value,10),
    orc_enabled: orcEnabled
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

