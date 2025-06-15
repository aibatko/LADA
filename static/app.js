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
    bubble(html,'ai',chatPane,true);
    for(let i=1;i<=plan.agents;i++){
      const wrap = bubble(`Agent ${i}: `,'ai',chatPane,true);
      const prog = document.createElement('progress');
      prog.id = `round${round}-agent${i}-prog`;
      prog.max = 100;
      prog.value = 0;
      wrap.appendChild(prog);
    }
  }catch(e){
    bubble(planStr,'ai',chatPane);
  }
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

  (data.plans||[]).forEach((p,i)=> showPlan(p,i+1));
  if(data.orchestrator){
    (data.orchestrator.tool_runs||[]).forEach(t=>{
      bubble(`[Orc] $ ${t.cmd}\n${t.result}`,"code",termPane);
    });
  }
  (data.agents||[]).forEach(a=>{
    a.tool_runs.forEach(t=>{
      bubble(`[A${a.id}] $ ${t.cmd}\n${t.result}`,"code",termPane);
    });
    bubble(`[Agent ${a.id}] ${a.reply}`,"ai",chatPane);
    const prog = document.getElementById(`round${a.round}-agent${a.id}-prog`);
    if(prog) prog.value = 100;
  });
  if(data.orchestrator && data.orchestrator.reply){
    bubble(`[Orchestrator] ${data.orchestrator.reply}`,"ai",chatPane);
  }
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

