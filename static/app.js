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
document.getElementById("sendChat").onclick = async ()=>{
  const msg = chatInput.value.trim(); if(!msg) return;
  bubble(msg,"user",chatPane); chatInput.value="";

  const data = await post("/api/chat",{
    prompt: msg,
    model:  document.getElementById("model").value,
    provider: document.getElementById("provider").value
  });

  data.tool_runs.forEach(t=>{
    bubble(`$ ${t.cmd}\n${t.result}`,"code",termPane);
  });
  bubble(data.reply,"ai",chatPane);
};

/* ---------- TERMINAL ---------- */
const cmdInput   = document.getElementById("cmdInput");
document.getElementById("sendCmd").onclick = async ()=>{
  const cmd = cmdInput.value.trim(); if(!cmd) return;
  bubble(`$ ${cmd}`,"code",termPane); cmdInput.value="";
  const res = await post("/api/command",{ command: cmd });
  bubble(res.result,"code",termPane);
};

