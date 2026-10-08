// The notebook tab's side of the frame contract, enough for the walkthrough:
// one sandboxed frame per widget output, every message checked for source,
// origin "null" and nonce; comm.send only for comm ids this frame was given,
// under 1 MiB and 60 a second, never from a read-only frame.
(() => {
  const MAX_SEND = 1 << 20;
  const params = new URLSearchParams(location.search);
  const readonly = params.get("readonly") === "1";
  const w = (window.walk = { frames: [], errors: [], violations: [], dropped: 0, refused: [], sent: 0, modules: [] });
  let manager = null;
  const ws = new WebSocket(`ws://${location.host}/kernel`);
  const fromB64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0)).buffer;
  const toB64 = (b) => { const u = new Uint8Array(b instanceof ArrayBuffer ? b : b.buffer); let s = ""; for (const x of u) s += String.fromCharCode(x); return btoa(s); };
  fetch("/manager.js").then((r) => r.text()).then((t) => { manager = t; });

  const byId = (id) => w.frames.find((f) => f.id === id);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "hello") { w.connected = true; return; }
    if (m.type === "display") { mount(m.model_id); return; }
    const f = byId(m.frame_id);
    if (!f) { if (m.type === "refused") w.refused.push(m.message); return; }
    if (m.type === "opens") {
      for (const o of m.opens) f.allowed.add(o.comm_id);
      send(f, { type: "init", theme: "light", output_id: f.id, mime: "application/vnd.jupyter.widget-view+json", data: { model_id: f.model_id, version_major: 2 }, opens: m.opens.map((o) => ({ ...o, buffers: o.buffers.map(fromB64) })), readonly: f.readonly });
    } else if (m.type === "out") {
      const msg = m.message;
      if (msg.type === "comm.open") f.allowed.add(msg.comm_id);
      send(f, { ...msg, buffers: m.buffers.map(fromB64) });
    } else if (m.type === "module") {
      w.modules.push(m.name);
      send(f, { type: "module", name: m.name, version: m.version, code: m.code });
    } else if (m.type === "refused") {
      w.refused.push(m.message);
    }
  };
  function send(f, m) { f.iframe.contentWindow.postMessage({ alk: 1, frame: f.nonce, ...m }, "*"); }
  function mount(model_id) {
    const nonce = crypto.randomUUID();
    const iframe = document.createElement("iframe");
    iframe.setAttribute("sandbox", "allow-scripts");
    iframe.style.height = "60px";
    iframe.src = `${window.CONTENT_ORIGIN}/c/nb-output/frame.html#n=${nonce}`;
    // Browsers without ancestorOrigins learn the parent's origin from this.
    iframe.addEventListener("load", () => send(f, { type: "hello" }));
    const f = { id: `f${w.frames.length + 1}`, nonce, iframe, model_id, readonly, allowed: new Set(), times: [], ready: false };
    w.frames.push(f);
    document.getElementById("outputs").appendChild(iframe);
  }
  addEventListener("message", (e) => {
    const f = w.frames.find((x) => x.iframe.contentWindow === e.source);
    const m = e.data;
    if (!f || e.origin !== "null" || !m || m.alk !== 1 || m.frame !== f.nonce) { w.dropped++; return; }
    switch (m.type) {
      case "ready":
        f.ready = true;
        send(f, { type: "module", name: "@alkera/widgets", version: "", code: manager });
        ws.send(JSON.stringify({ type: "attach", frame_id: f.id, model_id: f.model_id, readonly: f.readonly }));
        break;
      case "comm.send": {
        const now = performance.now();
        f.times = f.times.filter((t) => now - t < 1000);
        const size = JSON.stringify(m.content).length + (m.buffers || []).reduce((n, b) => n + b.byteLength, 0);
        if (f.readonly || !f.allowed.has(m.comm_id) || size > MAX_SEND || f.times.length >= 60) { w.dropped++; break; }
        f.times.push(now);
        w.sent++;
        ws.send(JSON.stringify({ type: "comm.send", frame_id: f.id, comm_id: m.comm_id, msg_id: m.msg_id, content: m.content, buffers: (m.buffers || []).map(toB64) }));
        break;
      }
      case "need_module": ws.send(JSON.stringify({ type: "need_module", frame_id: f.id, name: m.name, version: m.version })); break;
      case "size": f.iframe.style.height = `${Math.min(20000, m.height + 4)}px`; break;
      case "violation": w.violations.push({ frame: f.id, ...m }); break;
      case "error": w.errors.push({ frame: f.id, message: m.message }); break;
      case "link": w.links = [...(w.links || []), m.href]; break;
      default: break;
    }
  });
})();
