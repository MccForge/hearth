"""MCP App views for Hearth (MCP Apps extension, spec 2026-01-26).

A host that supports MCP Apps (Alexa+ devices with screens, Claude, ChatGPT, VS Code, Goose, ...) renders these HTML views in a
sandboxed iframe next to the conversation. Tools advertise the view they belong to through `_meta.ui.resourceUri`; the view
receives the tool call's arguments and result over the JSON-RPC postMessage bridge (`ui/notifications/tool-input`,
`ui/notifications/tool-result`) and can talk back (`ui/message`, `tools/call`). No SDK, no build step: each view is one
self-contained HTML document with a ~40-line bridge, so it works in any compliant host and in Hearth's own simulator."""
from __future__ import annotations
from typing import Any

MIME = "text/html;profile=mcp-app"
PROTOCOL = "2026-01-26"
CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; media-src data: blob:; connect-src 'none'"
RESOURCE_META = {"ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}}}


def ui_meta(view: str) -> dict:
    """`_meta` for a tool whose result is rendered by the given view."""
    return {"ui": {"resourceUri": f"ui://hearth/{view}"}}


# ---------------------------------------------------------------- shared app-side bridge (vanilla JS, no SDK)
BRIDGE_JS = r"""
const H = (() => {
  let seq = 0, ctx = null; const pending = new Map(), handlers = {};
  const post = m => window.parent.postMessage(m, '*');
  function request(method, params) { return new Promise((res, rej) => { const id = ++seq; pending.set(id, {res, rej}); post({jsonrpc: '2.0', id, method, params: params || {}}); }); }
  function notify(method, params) { post({jsonrpc: '2.0', method, params: params || {}}); }
  window.addEventListener('message', ev => {
    const m = ev.data; if (!m || m.jsonrpc !== '2.0') return;
    if (m.method === undefined) { const p = pending.get(m.id); if (!p) return; pending.delete(m.id); m.error ? p.rej(m.error) : p.res(m.result); return; }
    const h = handlers[m.method];
    if (m.id !== undefined) Promise.resolve(h ? h(m.params || {}) : {}).then(r => post({jsonrpc: '2.0', id: m.id, result: r || {}})).catch(e => post({jsonrpc: '2.0', id: m.id, error: {code: -32000, message: String(e)}}));
    else if (h) h(m.params || {});
  });
  function connect(name) { return request('ui/initialize', {protocolVersion: '2026-01-26', capabilities: {}, clientInfo: {name, version: '1.0.0'}}).then(r => { ctx = (r && r.hostContext) || {}; applyContext(ctx); notify('ui/notifications/initialized'); return ctx; }); }
  function applyContext(c) { const root = document.documentElement; if (c.theme) root.dataset.theme = c.theme;
    const i = c.safeAreaInsets; if (i) ['top', 'right', 'bottom', 'left'].forEach(k => root.style.setProperty('--safe-' + k, (i[k] || 0) + 'px'));
    const d = c.containerDimensions; if (d) { const w = d.width || d.maxWidth; if (w) { root.style.setProperty('--cw', w + 'px'); root.style.fontSize = Math.max(FONT[1], Math.min(FONT[2], w * FONT[0])) + 'px'; }
      if (d.height) { root.style.setProperty('--ch', d.height + 'px'); root.dataset.fixed = '1'; } else { root.style.removeProperty('--ch'); delete root.dataset.fixed; if (d.maxHeight) root.style.setProperty('--chmax', d.maxHeight + 'px'); } } }
  function sizeChanged() { notify('ui/notifications/size-changed', {width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight}); }
  handlers['ui/notifications/host-context-changed'] = p => applyContext(p);
  handlers['ui/resource-teardown'] = () => ({});
  handlers['ping'] = () => ({});
  return {request, notify, on: (m, f) => { handlers[m] = f; }, connect, sizeChanged, get ctx() { return ctx; }};
})();
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const short = (s, n) => { s = String(s || '').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const parseText = res => { const t = (res.content || []).find(b => b.type === 'text'); if (!t) return {}; try { return JSON.parse(t.text); } catch (e) { return {text: t.text}; } };
const toolOf = r => (r._meta && r._meta['hearth/tool']) || (H.ctx && H.ctx.toolInfo && H.ctx.toolInfo.tool && H.ctx.toolInfo.tool.name) || '';
const fmtDate = iso => { try { return new Date(iso + 'T12:00:00').toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric'}); } catch (e) { return iso || ''; } };
"""

BASE_CSS = r"""
:root { --bg:#0b1220; --bg2:#15223b; --ink:#f4f6fb; --dim:#a9b4c8; --line:rgba(255,255,255,.12); --tile:rgba(255,255,255,.06); --accent:#5ec8ff; --amber:#f2c14e; --red:#ff6b6b; --green:#6fd8a5; }
:root[data-theme="light"] { --bg:#ffffff; --bg2:#f6f3ee; --ink:#23201d; --dim:#6f665f; --line:#e6ddd3; --tile:#faf6f1; --accent:#b5542d; --amber:#b8860b; --red:#c8322b; --green:#2e8b57; }
html { width:var(--cw, 100%); font-size:14px; overflow:hidden; }
html[data-fixed] { height:var(--ch); }
body { margin:0; color:var(--ink); background:transparent; font:400 1rem/1.35 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.card { box-sizing:border-box; width:100%; min-height:var(--ch, auto); max-height:var(--chmax, none); padding:calc(1.5em + var(--safe-top, 0px)) calc(2.1em + var(--safe-right, 0px)) calc(1.5em + var(--safe-bottom, 0px)) calc(2.1em + var(--safe-left, 0px)); background:linear-gradient(160deg, var(--bg), var(--bg2)); display:flex; flex-direction:column; gap:.85em; overflow:hidden; }
html[data-fixed] .card { height:var(--ch); }
.top { display:flex; align-items:flex-start; justify-content:space-between; gap:1em; }
.brand { font-size:.72em; letter-spacing:.16em; text-transform:uppercase; color:var(--accent); font-weight:600; }
.date { color:var(--dim); font-size:.85em; text-align:right; white-space:nowrap; }
h1 { margin:.15em 0 0; font-size:2em; font-weight:600; letter-spacing:-.01em; line-height:1.15; }
.sub { color:var(--dim); font-size:.95em; margin-top:.2em; }
.tile { background:var(--tile); border:1px solid var(--line); border-radius:.8em; padding:.75em 1em; }
.tile h3 { margin:0 0 .4em; font-size:.72em; text-transform:uppercase; letter-spacing:.12em; color:var(--dim); font-weight:600; }
.chips { display:flex; flex-wrap:wrap; gap:.4em; } .chip { padding:.22em .7em; border-radius:1em; background:rgba(127,127,127,.18); font-size:.85em; }
.chip.on { background:rgba(111,216,165,.22); color:var(--green); } .chip.off { background:rgba(242,193,78,.22); color:var(--amber); } .chip.bad { background:rgba(255,107,107,.2); color:var(--red); }
.foot { display:flex; align-items:center; gap:.8em; min-height:1.5em; color:var(--dim); font-size:.95em; }
.ok { color:var(--green); } .warn { color:var(--amber); } .bad { color:var(--red); } .big { font-size:1.35em; font-weight:600; color:var(--ink); }
.hidden { display:none !important; }
"""


def _page(title: str, css: str, body: str, js: str, font=(0.0235, 12, 21)) -> str:
    """font = (scale per px of container width, min px, max px): the view sizes its type from the host's containerDimensions."""
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>\n<style>{BASE_CSS}{css}</style></head>\n'
            f'<body>{body}\n<script>const FONT = {list(font)};{BRIDGE_JS}{js}</script></body></html>')


# ---------------------------------------------------------------- 1. the check-in card (device screen)
CHECKIN_CSS = r"""
h1 { font-size:1.5em; } .sub { font-size:.88em; margin-top:.1em; } .card { gap:.6em; }
.steps { display:flex; flex-wrap:wrap; gap:.45em; }
.t { flex:1 1 6.5em; min-width:6.5em; max-width:12em; padding:.3em .6em; border-radius:.7em; background:var(--tile); border:1px solid var(--line); transition:background .25s, border-color .25s; display:flex; flex-direction:column; gap:.1em; }
.t .lbl { display:flex; align-items:center; gap:.45em; font-size:.72em; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.t .dot { width:.75em; height:.75em; border-radius:50%; border:2px solid var(--dim); flex:none; box-sizing:border-box; }
.t .val { font-size:.95em; min-height:1.3em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:var(--dim); }
.t.cur { background:rgba(94,200,255,.14); border-color:var(--accent); } .t.cur .dot { border-color:var(--accent); box-shadow:0 0 .6em var(--accent); } .t.cur .lbl { color:var(--accent); }
.t.done .dot { background:var(--green); border-color:var(--green); } .t.done .val { color:var(--ink); }
.t.flag .dot { background:var(--amber); border-color:var(--amber); } .t.flag .val { color:var(--amber); }
.tiles { display:flex; flex-wrap:wrap; gap:.6em; align-items:stretch; align-content:flex-start; flex:1 1 auto; min-height:0; overflow:hidden; } .tiles .tile { flex:1 1 9.5em; min-width:9.5em; padding:.5em .8em; } .tile h3 { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ev { display:flex; gap:.6em; align-items:baseline; margin:.1em 0; min-width:0; } .ev > div { min-width:0; } .ev b { color:var(--accent); font-weight:600; min-width:4.4em; white-space:nowrap; } .ev small { display:block; color:var(--dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.msg { display:flex; align-items:center; gap:.7em; } .play { width:2.2em; height:2.2em; border-radius:50%; border:0; background:var(--accent); color:#06203a; font-size:1em; cursor:pointer; flex:none; }
.msg .from { font-weight:600; } .msg .tx { color:var(--dim); font-size:.86em; font-style:italic; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.q { font-size:.9em; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; } .q span { color:var(--dim); }
.card.alert { background:linear-gradient(160deg, #3a0f14, #6a1f1f); } .card.alert .brand { color:#ffb3b3; } .card.alert .t.cur { border-color:#ffb3b3; background:rgba(255,255,255,.08); }
.card.done h1 { color:var(--green); }
.foot { margin-top:auto; flex:none; } .foot:empty { display:none; }
"""

CHECKIN_BODY = r"""
<div class="card" id="card">
  <div class="top"><div><div class="brand">Hearth</div><h1 id="title">Good morning</h1><div class="sub" id="sub">Your daily check-in</div></div><div class="date" id="date"></div></div>
  <div class="steps" id="steps"></div>
  <div class="tiles">
    <div class="tile" id="medsTile"><h3>Medication</h3><div class="chips" id="meds"></div></div>
    <div class="tile hidden" id="evTile"><h3>Today</h3><div id="events"></div></div>
    <div class="tile hidden" id="msgTile"><h3>Message from family</h3><div class="msg"><button class="play" id="playBtn" title="Play">▶</button><div><div class="from" id="msgFrom"></div><div class="tx" id="msgTx"></div></div></div></div>
    <div class="tile hidden" id="qTile"><h3>Family asked</h3><div id="qs"></div></div>
  </div>
  <div class="foot" id="foot"></div>
</div>
"""

CHECKIN_JS = r"""
const S = {ctx: null, answered: {}, flags: [], current: null, audio: null, topics: [], lastArgs: {}};
const WORDS = {mood: {1: 'Rough', 2: 'Not great', 3: 'Okay', 4: 'Good', 5: 'Great'}, sleep: {1: 'Barely slept', 2: 'Poorly', 3: 'Okay', 4: 'Well', 5: 'Very well'}};
const LABEL = {mood: 'Feeling', sleep: 'Sleep', meds_taken: 'Medication', ate: 'Food', concern: 'Concerns', plans: 'Plans today'};
const medName = m => m.replace(/\s+\d+\s*(mg|mcg|ml|units?)\b.*$/i, '');
function setContext(c) {
  S.ctx = c; $('title').textContent = c.greeting || 'Hello'; $('date').textContent = fmtDate(c.status && c.status.date);
  const away = (c.away || [])[0];
  $('sub').textContent = away && away.who ? `${away.who} is away${away.cover ? ' · ' + away.cover + ' is your go-to' : ''}` : 'Your daily check-in';
  S.topics = [['mood'], ['sleep'], ['meds_taken'], ['ate'], ['concern']];
  (c.events_today || []).forEach(e => S.topics.push(['event:' + e.id, short(e.title, 22)]));
  (c.questions_from_family || []).forEach(q => S.topics.push(['question:' + q.id, q.from + ' asks']));
  S.topics.push(['plans']);
  $('meds').innerHTML = (c.medications_due || []).map(m => `<span class="chip">${esc(medName(m))}</span>`).join('') || '<span class="chip">None due</span>';
  const ev = c.events_today || []; $('evTile').classList.toggle('hidden', !ev.length);
  $('events').innerHTML = ev.map(e => `<div class="ev"><b>${esc(e.time || 'today')}</b><div>${esc(e.title)}${e.notes ? `<small>${esc(short(e.notes, 60))}</small>` : ''}</div></div>`).join('');
  const qs = c.questions_from_family || []; $('qTile').classList.toggle('hidden', !qs.length);
  $('qs').innerHTML = qs.map(q => `<div class="q">“${esc(short(q.text, 70))}” <span>— ${esc(q.from)}</span></div>`).join('');
  const m = (c.family_messages || [])[0];
  if (m) { $('msgTile').classList.remove('hidden'); $('msgFrom').textContent = 'From ' + m.from; $('msgTx').textContent = m.transcript ? '“' + short(m.transcript, 70) + '”' : 'Voice message'; }
  advance(); renderSteps();
}
function renderSteps() {
  $('steps').innerHTML = S.topics.map(([k, lbl]) => { const a = S.answered[k]; const cls = ['t', k === S.current && !a ? 'cur' : '', a ? 'done' : '', a && a.flag ? 'flag' : ''].join(' ');
    return `<div class="${cls}" title="${esc(lbl || LABEL[k] || k)}"><span class="lbl"><span class="dot"></span>${esc(lbl || LABEL[k] || k)}</span><span class="val">${a ? esc(a.text) : (k === S.current ? '…' : '')}</span></div>`; }).join('');
  H.sizeChanged();
}
function advance() { const i = S.topics.findIndex(([k]) => !S.answered[k]); S.current = i >= 0 ? S.topics[i][0] : null; }
function foot(html) { $('foot').innerHTML = html; }
const PRETTY = {fall: 'a fall', chest_pain: 'chest pain', breathing: 'breathing trouble', dizzy: 'dizziness', confusion: 'confusion', pain: 'pain', no_sleep: 'poor sleep', skipped_meds: 'medication not taken', no_food: 'not eating', lonely: 'feeling lonely', emergency: 'an emergency'};
function answered(field, args, res) {
  const v = (res.recorded || {})[field]; let text;
  if (field === 'mood' || field === 'sleep') text = WORDS[field][v] || short(args.quote || args.value, 24);
  else if (field === 'meds_taken') text = v === 1 ? 'Taken' : v === 0 ? 'Not yet' : 'Not sure';
  else if (field === 'ate') text = v === 1 ? 'Yes' : v === 0 ? 'Not yet' : short(args.quote || args.value, 24);
  else text = short(args.quote || args.value, 30);
  const added = res.flags_added || []; const flag = added.length > 0; if (flag) S.flags.push(...added);
  const pretty = added.map(f => PRETTY[f] || f.replace(/_/g, ' ')).join(', ');
  if (field === 'note') { if (flag) foot(`<span class="warn">Noted ${esc(pretty)} · your family will see this</span>`); return; }
  S.answered[field] = {text, flag};
  if (field === 'meds_taken') document.querySelectorAll('#meds .chip').forEach(ch => ch.classList.add(v === 1 ? 'on' : v === 0 ? 'off' : 'x'));
  advance(); renderSteps();
  if (flag) foot(`<span class="warn">Noted ${esc(pretty)} · your family will see this</span>`);
}
function onResult(tool, args, res) {
  const sc = res.structuredContent || parseText(res);
  switch (tool) {
    case 'get_checkin_context': setContext(sc); break;
    case 'start_checkin': (sc.answered || []).forEach(f => { if (LABEL[f] && !S.answered[f]) S.answered[f] = {text: 'Answered earlier', flag: false}; }); advance(); renderSteps(); break;
    case 'record_answer': answered(args.field, args, sc); break;
    case 'complete_checkin': { S.current = null; renderSteps(); const lvl = sc.risk_level || 'ok'; const to = (sc.summary_sent_to || []).join(' and ') || 'your family'; const calm = lvl === 'ok' || lvl === 'watch';
      $('card').classList.add('done'); $('title').textContent = calm ? 'All done. Have a lovely day.' : lvl === 'concern' ? 'All done. Take it gently today.' : 'Thank you for telling me.';
      const told = (sc.escalation && (sc.escalation.notified || []).map(n => n.contact.split(' ')[0]).join(', ')) || to;
      foot(calm ? `<span class="ok">✓ ${esc(to)} knows you're doing okay</span>` : lvl === 'concern' ? `<span class="warn">✓ ${esc(told)} will know about today · expect a call</span>` : `<span class="bad">⚠ Your family is being told right now</span>`); break; }
    case 'get_family_message': { const audio = (res.content || []).find(b => b.type === 'audio'); const info = parseText(res);
      $('msgTile').classList.remove('hidden'); $('msgFrom').textContent = 'From ' + (info.from || 'family'); if (info.transcript) $('msgTx').textContent = '“' + short(info.transcript, 70) + '”';
      if (audio && audio.data) S.audio = new Audio('data:' + (audio.mimeType || 'audio/webm') + ';base64,' + audio.data); break; }
    case 'request_help': { $('card').classList.add('alert'); $('title').textContent = 'Help is on the way'; $('sub').textContent = 'If this is an emergency, call 911 now.';
      const names = (sc.notified || []).map(n => n.contact).join(', ') || 'your family'; foot(`<span class="big">Alerting ${esc(names)}</span>`); break; }
    case 'snooze_checkin': $('title').textContent = 'No problem'; $('sub').textContent = "I'll check back in a little while."; foot('<span>Check-in paused</span>'); break;
    case 'record_reply': foot(`<span class="ok">✓ Message saved for ${esc(sc.to || 'your family')}</span>`); break;
    case 'log_medication': if (sc.taken) { S.answered.meds_taken = {text: 'Taken', flag: false}; document.querySelectorAll('#meds .chip').forEach(ch => ch.classList.add('on')); advance(); renderSteps(); } break;
  }
  H.sizeChanged();
}
$('playBtn').onclick = () => { if (S.audio) { S.audio.currentTime = 0; S.audio.play(); } else H.request('ui/message', {role: 'user', content: [{type: 'text', text: 'Can you play the message from my family again?'}]}); };
H.on('ui/notifications/tool-input', p => { S.lastArgs = p.arguments || {}; });
H.on('ui/notifications/tool-result', r => onResult(toolOf(r), S.lastArgs, r));
H.connect('Hearth check-in card').then(() => H.sizeChanged());
"""

# ---------------------------------------------------------------- 2. the calendar card (device screen or phone)
CALENDAR_CSS = r"""
.week { display:grid; grid-template-columns:repeat(7, 1fr); gap:.5em; flex:1; min-height:0; }
.agenda { display:flex; flex-direction:column; gap:.4em; } .agenda .row { display:flex; gap:.7em; align-items:baseline; padding:.35em .6em; border-radius:.5em; background:var(--tile); border:1px solid var(--line); } .agenda .row.today { border-color:var(--accent); }
.agenda .d { min-width:4.2em; color:var(--dim); font-size:.8em; text-transform:uppercase; letter-spacing:.06em; white-space:nowrap; } .agenda .row.today .d { color:var(--accent); } .agenda .e { display:block; background:none; border:0; padding:0; font-size:.95em; } .agenda .none { color:var(--dim); font-size:.9em; }
.day { background:var(--tile); border:1px solid var(--line); border-radius:.7em; padding:.5em .55em; min-height:6em; display:flex; flex-direction:column; gap:.3em; }
.day.today { border-color:var(--accent); } .day h4 { margin:0; font-size:.72em; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); font-weight:600; }
.day h4 b { display:block; font-size:1.5em; color:var(--ink); letter-spacing:0; }
.e { font-size:.82em; line-height:1.25; padding:.3em .45em; border-radius:.45em; background:rgba(94,200,255,.14); border-left:.25em solid var(--accent); }
.e.new { background:rgba(111,216,165,.2); border-left-color:var(--green); } .e.done { opacity:.6; } .e small { display:block; color:var(--dim); }
.e.reminder { background:rgba(242,193,78,.16); border-left-color:var(--amber); }
"""

CALENDAR_BODY = r"""
<div class="card"><div class="top"><div><div class="brand">Hearth</div><h1 id="title">Coming up</h1><div class="sub" id="sub">The next seven days</div></div><div class="date" id="date"></div></div>
<div class="week" id="week"></div><div class="foot" id="foot"></div></div>
"""

CALENDAR_JS = r"""
const S = {events: [], lastArgs: {}, highlight: null};
const iso = d => d.toISOString().slice(0, 10);
function render() {
  const today = new Date(); today.setHours(12, 0, 0, 0); const days = [];
  for (let i = 0; i < 7; i++) { const d = new Date(today); d.setDate(today.getDate() + i); days.push(d); }
  $('date').textContent = today.toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric'});
  const narrow = (H.ctx && H.ctx.containerDimensions && (H.ctx.containerDimensions.width || H.ctx.containerDimensions.maxWidth) || 800) < 480;
  $('week').className = narrow ? 'agenda' : 'week';
  if (narrow) { $('week').innerHTML = days.map((d, i) => { const k = iso(d); const evs = S.events.filter(e => e.date === k); if (!evs.length && i > 0) return ''; return `<div class="row${i === 0 ? ' today' : ''}"><span class="d">${i === 0 ? 'Today' : d.toLocaleDateString('en-US', {weekday: 'short', day: 'numeric'})}</span><div>${evs.length ? evs.map(e => `<span class="e${e.id === S.highlight ? ' new' : ''}">${esc(e.title)}${e.time ? ' · ' + esc(e.time) : ''}</span>`).join('') : '<span class="none">Nothing today</span>'}</div></div>`; }).join(''); }
  else $('week').innerHTML = days.map((d, i) => { const k = iso(d); const evs = S.events.filter(e => e.date === k);
    return `<div class="day${i === 0 ? ' today' : ''}"><h4>${d.toLocaleDateString('en-US', {weekday: 'short'})}<b>${d.getDate()}</b></h4>` +
      evs.map(e => `<div class="e${e.id === S.highlight ? ' new' : ''}${e.status === 'done' ? ' done' : ''}${e.kind === 'reminder' ? ' reminder' : ''}">${esc(e.title)}${e.time ? `<small>${esc(e.time)}</small>` : ''}</div>`).join('') + '</div>'; }).join('');
  const later = S.events.filter(e => e.date > iso(days[6]));
  foot(later.length ? `Later: ${later.map(e => esc(e.title) + ' on ' + fmtDate(e.date)).join(' · ')}` : (S.events.length ? '' : 'Nothing scheduled this week'));
  H.sizeChanged();
}
function foot(html) { $('foot').innerHTML = html; }
function onResult(tool, args, res) { const sc = res.structuredContent || parseText(res);
  if (tool === 'list_events') { S.events = sc.events || []; S.highlight = null; render(); }
  else if (tool === 'add_event' && sc.event_id) { S.events.push({id: sc.event_id, date: args.date, time: args.time || '', title: args.title, kind: args.kind || 'appointment', status: 'scheduled'}); S.highlight = sc.event_id; render(); foot(`<span class="ok">✓ Added ${esc(args.title)} · ${esc(fmtDate(args.date))}${args.time ? ' at ' + esc(args.time) : ''}</span>`); } }
H.on('ui/notifications/tool-input', p => { S.lastArgs = p.arguments || {}; });
H.on('ui/notifications/tool-result', r => onResult(toolOf(r), S.lastArgs, r));
H.connect('Hearth calendar').then(render);
"""

# ---------------------------------------------------------------- 3. the family status card (caregiver's phone or any host)
STATUS_CSS = r"""
.card { padding:1.1em 1.1em 1.2em; gap:.8em; }
.pill { display:inline-block; padding:.2em .7em; border-radius:1em; font-size:.8em; font-weight:600; letter-spacing:.02em; }
.pill.ok { background:rgba(46,139,87,.15); color:var(--green); } .pill.watch { background:rgba(184,134,11,.15); color:var(--amber); } .pill.concern, .pill.urgent { background:rgba(200,50,43,.14); color:var(--red); }
.pill.neutral { background:rgba(127,127,127,.15); color:var(--dim); }
.summary { font-size:.98em; line-height:1.45; }
.row { display:flex; gap:.5em; flex-wrap:wrap; align-items:center; }
ul { margin:.2em 0 0 1.1em; padding:0; } li { margin:.15em 0; }
.al { border-left:.25em solid var(--red); padding:.3em .6em; background:rgba(200,50,43,.08); border-radius:.3em; margin:.2em 0; font-size:.92em; }
"""

STATUS_BODY = r"""
<div class="card"><div class="top"><div><div class="brand">Hearth</div><h1 id="title" style="font-size:1.3em">Mom today</h1><div class="sub" id="sub"></div></div><div class="date" id="date"></div></div>
<div class="row" id="pills"></div>
<div class="summary" id="summary"></div>
<div class="tile hidden" id="alerts"><h3>Open alerts</h3><div id="alertList"></div></div>
<div class="tile hidden" id="flagsTile"><h3>Flagged today</h3><div class="chips" id="flags"></div></div>
<div class="tile hidden" id="trends"><h3>This week</h3><ul id="trendList"></ul></div>
<div class="foot" id="foot"></div></div>
"""

STATUS_JS = r"""
const shortDate = iso => { try { return new Date(iso + 'T12:00:00').toLocaleDateString('en-US', {month: 'short', day: 'numeric'}); } catch (e) { return iso || ''; } };
const STATE = {checked_in: ['Checked in', 'ok'], in_progress: ['Checking in now', 'neutral'], waiting: ['Not yet · window open', 'watch'], before_window: ['Day not started', 'neutral'], overdue: ['Missed check-in', 'urgent']};
const FLAG = {fall: 'Fall', chest_pain: 'Chest pain', breathing: 'Breathing', dizzy: 'Dizziness', confusion: 'Confusion', pain: 'Pain', no_sleep: 'Poor sleep', skipped_meds: 'Missed medication', no_food: 'Not eating', lonely: 'Lonely', emergency: 'Emergency'};
function onResult(tool, args, res) { const s = res.structuredContent || parseText(res); if (tool !== 'get_status' || !s.name) return;
  const first = (s.name || '').split(' ')[0]; $('title').textContent = `${first} today`; $('date').textContent = fmtDate(s.date) + (s.local_time ? ' · ' + s.local_time : '');
  const st = STATE[s.state] || [s.state, 'neutral']; const risk = s.risk_level; const pills = [`<span class="pill ${st[1]}">${esc(st[0])}</span>`];
  if (risk) pills.push(`<span class="pill ${risk}">Concern: ${esc(risk)}</span>`); if (s.state === 'overdue' && s.overdue_minutes) pills.push(`<span class="pill urgent">${s.overdue_minutes >= 120 ? Math.round(s.overdue_minutes / 60) + ' h' : s.overdue_minutes + ' min'} late</span>`);
  if (s.snoozed_until) pills.push('<span class="pill neutral">Asked to talk later</span>'); $('pills').innerHTML = pills.join('');
  $('summary').textContent = s.summary || (s.state === 'checked_in' ? 'Checked in, no summary yet.' : s.state === 'overdue' ? `No check-in during the ${s.window} window.` : `Check-in window ${s.window}.`);
  const away = (s.away || [])[0]; const fn = n => String(n || '').split(' ')[0]; $('sub').textContent = away && away.who ? `${fn(away.who)} away until ${shortDate(away.until)}${away.cover ? ' · ' + fn(away.cover) + ' covering' : ''}` : '';
  const al = s.open_alert_details || []; $('alerts').classList.toggle('hidden', !al.length); $('alertList').innerHTML = al.map(a => `<div class="al"><b>${esc(a.level)}</b> · ${esc(a.reason)}${a.detail ? ': ' + esc(short(a.detail, 120)) : ''}</div>`).join('');
  const fl = s.flags || []; $('flagsTile').classList.toggle('hidden', !fl.length); $('flags').innerHTML = fl.map(f => `<span class="chip ${['fall','chest_pain','breathing','emergency'].includes(f) ? 'bad' : 'off'}">${esc(FLAG[f] || f)}</span>`).join('');
  const tr = s.trend_insights || []; $('trends').classList.toggle('hidden', !tr.length); $('trendList').innerHTML = tr.map(t => `<li>${esc(t)}</li>`).join('');
  const bits = []; if (s.pending_messages) bits.push(`${s.pending_messages} message${s.pending_messages > 1 ? 's' : ''} waiting to play`); if (s.pending_questions) bits.push(`${s.pending_questions} question${s.pending_questions > 1 ? 's' : ''} queued`);
  $('foot').textContent = bits.join(' · '); H.sizeChanged(); }
let lastArgs = {};
H.on('ui/notifications/tool-input', p => { lastArgs = p.arguments || {}; });
H.on('ui/notifications/tool-result', r => onResult(toolOf(r), lastArgs, r));
H.connect('Hearth family status').then(() => H.sizeChanged());
"""

VIEWS: dict[str, dict[str, Any]] = {
    "ui://hearth/checkin": {"name": "checkin_card", "title": "Check-in card", "description": "What the person sees on a screen during the daily check-in: greeting, topics ticked off as they answer, medication, today's appointments, the family message, and the outcome.",
                            "html": _page("Hearth check-in", CHECKIN_CSS, CHECKIN_BODY, CHECKIN_JS)},
    "ui://hearth/calendar": {"name": "calendar_card", "title": "Calendar card", "description": "The next seven days of appointments and reminders; highlights an event just added by voice.",
                             "html": _page("Hearth calendar", CALENDAR_CSS, CALENDAR_BODY, CALENDAR_JS)},
    "ui://hearth/status": {"name": "family_status_card", "title": "Family status card", "description": "The caregiver's view: today's state, concern level, summary, open alerts, flags, and the week's trends.",
                           "html": _page("Hearth family status", STATUS_CSS, STATUS_BODY, STATUS_JS, font=(0.045, 13, 17))},
}


def register(server) -> None:
    """Expose each view as an MCP resource with the MCP Apps mime type and `_meta.ui` rendering hints."""
    def _handler(html: str):
        def _view() -> str:
            return html
        return _view
    for uri, v in VIEWS.items():
        server.resource(uri, name=v["name"], title=v["title"], description=v["description"], mime_type=MIME, meta=RESOURCE_META)(_handler(v["html"]))


def manifest(server) -> dict:
    """What a host learns from tools/list and resources/list, condensed for the simulator: tool -> view, view -> mime/meta."""
    tools = {}
    for t in server._tool_manager.list_tools():
        uri = ((t.meta or {}).get("ui") or {}).get("resourceUri")
        if uri: tools[t.name] = uri
    return {"protocolVersion": PROTOCOL, "tools": tools,
            "resources": {uri: {"name": v["name"], "title": v["title"], "mimeType": MIME, "_meta": RESOURCE_META} for uri, v in VIEWS.items()}}
