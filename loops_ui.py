"""The loop crate's page, at /musicbot/dj/loops/ (linked from the DJ booth).

Real DJ borrows riffs and grooves from the library's stems (loops.py). They
are found by measuring, not by ear, so this page is where someone listens:
every loop plays as it is, and a thumbs down takes it out for good, a thumbs
up makes it a favourite. "Approved only" plays nothing nobody has approved.
"Played lately" lists what went out over the last mixes, so a riff that just
sounded wrong in the room is easy to find and turn down.

API (behind the web UI's usual session check, like the booth's):
  GET  /api/loops                  the crate, the votes, what was played lately
  GET  /api/loops/{id}/audio       one loop as WAV, twice round
  POST /api/loops/{id}/vote        {"vote": "up" | "down" | null}
  POST /api/loops/settings         {"approved_only": bool}
"""

import asyncio
import logging
from collections import OrderedDict

from aiohttp import web

import loops

logger = logging.getLogger("MusicBot.LoopsUI")

_previews = OrderedDict()           # id -> WAV bytes, the last few listened to


async def page(request):
    return web.Response(text=LOOPS_HTML, content_type="text/html", headers={"Cache-Control": "no-store"})


async def api_list(request):
    rated = loops.ratings()
    votes = rated["votes"]
    crate = loops.crate()
    by_id = {loop["id"]: loop for loop in crate}
    played = []
    for item in reversed(loops.played):
        loop = by_id.get(item["id"])
        if loop:
            played.append({"id": item["id"], "at": item["at"], "over": item.get("over"),
                           "part": loop["part"], "title": loop.get("title")})
    return web.json_response({
        "loops": [{"id": loop["id"], "part": loop["part"], "title": loop.get("title"), "bpm": round(loop.get("bpm") or 0, 1),
                   "camelot": loop.get("camelot"), "score": loop.get("score"), "start": loop.get("start"),
                   "vote": votes.get(loop["id"])}
                  for loop in crate],
        "approved_only": rated["approved_only"],
        "played": played,
    })


async def api_audio(request):
    loop_id = request.match_info["loop_id"]
    data = _previews.get(loop_id)
    if data is None:
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, loops.preview, loop_id)
        except LookupError:
            return web.json_response({"error": "No such loop."}, status=404)
        except Exception as error:
            logger.warning("Could not render loop %s", loop_id, exc_info=True)
            return web.json_response({"error": f"Could not play that loop: {error}"}, status=500)
        _previews[loop_id] = data
        while len(_previews) > 12:
            _previews.popitem(last=False)
    return web.Response(body=data, content_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


async def _body(request):
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="invalid json")


async def api_vote(request):
    body = await _body(request)
    try:
        loops.vote(request.match_info["loop_id"], body.get("vote"))
    except LookupError:
        return web.json_response({"error": "No such loop."}, status=404)
    except ValueError as error:
        return web.json_response({"error": str(error)}, status=400)
    return await api_list(request)


async def api_settings(request):
    body = await _body(request)
    if "approved_only" in body:
        loops.set_approved_only(bool(body["approved_only"]))
    return await api_list(request)


def setup(app):
    app.router.add_get("/dj/loops/", page)
    app.router.add_get("/api/loops", api_list)
    app.router.add_get("/api/loops/{loop_id}/audio", api_audio)
    app.router.add_post("/api/loops/settings", api_settings)
    app.router.add_post("/api/loops/{loop_id}/vote", api_vote)


LOOPS_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Loop Crate</title>
<style>
:root{color-scheme:dark;--bg:#07080a;--panel:#16181c;--edge:#2a2e35;--edge2:#3a3f48;--text:#eef1f5;--muted:#8b93a1;--dim:#5b6270;
--orange:#ff8a1c;--green:#2fe07a;--red:#ff3d5a;--cyan:#33d6ff;
--guitar:#ff8a1c;--piano:#33d6ff;--bass:#b04bff;--drums:#2fe07a}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 "Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
button{font:inherit;color:inherit;cursor:pointer;border:0;background:none;touch-action:manipulation}
button:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
.top{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid #000;background:linear-gradient(#1c1f24,#121418);position:sticky;top:0;z-index:5;flex-wrap:wrap}
.brand{font-weight:800;letter-spacing:.14em;font-size:14px}.brand b{color:var(--orange)}
.back{color:var(--muted);text-decoration:none;font-size:13px}.back:hover{color:var(--text)}
.spacer{flex:1}
.wrap{max-width:900px;margin:0 auto;padding:16px}
.intro{color:var(--muted);margin:0 0 14px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.chip{padding:6px 12px;border-radius:999px;border:1px solid var(--edge2);font-weight:700;font-size:12px;color:var(--muted)}
.chip.on{background:#23262d;color:var(--text);border-color:var(--text)}
.switch{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--edge);border-radius:12px;background:var(--panel);margin-bottom:16px}
.switch input{width:20px;height:20px;accent-color:var(--orange)}
.switch small{display:block;color:var(--muted)}
h2{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:18px 0 8px}
.list{display:flex;flex-direction:column;gap:6px}
.row{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:10px;align-items:center;padding:8px 10px;border-radius:12px;background:var(--panel);border:1px solid #000;box-shadow:0 0 0 1px #ffffff08 inset}
.row.down{opacity:.45}
.play{width:40px;height:40px;border-radius:50%;background:#23262d;display:grid;place-items:center;font-size:15px;border:1px solid var(--edge2)}
.play.on{background:var(--orange);color:#1a0d00;border-color:var(--orange)}
.play.wait{animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.4}}
.t{min-width:0}.t b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t span{color:var(--muted);font-size:12px}
.part{display:inline-block;font-size:10px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;padding:1px 6px;border-radius:6px;margin-right:6px;border:1px solid;vertical-align:1px}
.votes{display:flex;gap:6px}
.vote{width:40px;height:40px;border-radius:10px;border:1px solid var(--edge2);font-size:17px;filter:grayscale(1);opacity:.6}
.vote.on.up{background:#0f5a30;border-color:var(--green);filter:none;opacity:1}
.vote.on.down{background:#5a0f1e;border-color:var(--red);filter:none;opacity:1}
.empty{color:var(--dim);padding:18px;text-align:center}
.toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#23262d;border:1px solid var(--edge2);padding:10px 14px;border-radius:10px;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.show{opacity:1}
@media (max-width:520px){.row{grid-template-columns:40px minmax(0,1fr);}.votes{grid-column:2}}
</style></head><body>
<header class="top">
  <a class="back" id="back" href="#">&larr; Booth</a>
  <div class="brand">LOOP <b>CRATE</b></div>
  <div class="spacer"></div>
  <span id="count" style="color:var(--muted);font-size:12px"></span>
</header>
<main class="wrap">
  <p class="intro">Real DJ borrows these riffs and grooves from other songs' stems and plays them over its mixes. They were picked by measuring, not by ear: listen, and give the bad ones a thumbs down so they never play again.</p>
  <label class="switch"><input type="checkbox" id="approved"><span><b>Approved only</b><small>Only thumbed-up loops get played. The rest wait until someone listens to them.</small></span></label>
  <div id="playedBox"><h2>Played lately</h2><div class="list" id="played"></div></div>
  <h2>The crate</h2>
  <div class="bar" id="parts"></div>
  <div class="bar" id="verdicts"></div>
  <div class="list" id="list"></div>
</main>
<div class="toast" id="toast"></div>
<script>
"use strict";
const $=(s)=>document.querySelector(s);
const params=new URLSearchParams(location.search);
const guildId=params.get('guild_id')||'';
const base=location.pathname.replace(/dj\/loops\/?$/,'');
let token=new URLSearchParams(location.hash.slice(1)).get('token')||'';
try{ if(token) sessionStorage.setItem('dj_token:'+guildId,token); else token=sessionStorage.getItem('dj_token:'+guildId)||''; }catch(_){}
if(location.hash) history.replaceState(null,'',location.pathname+location.search);
$('#back').href=base+'dj/?guild_id='+encodeURIComponent(guildId);
const PARTS={guitar:'Guitar',piano:'Piano',bass:'Bass',drums:'Drums'};
let D={loops:[],played:[],approved_only:false}, part='all', verdict='all', playing=null, audio=null;
function headers(json){const h={};if(json)h['Content-Type']='application/json';if(token)h['X-MB-Session']=token;return h}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function toast(t){const el=$('#toast');el.textContent=t;el.classList.add('show');clearTimeout(el._h);el._h=setTimeout(()=>el.classList.remove('show'),2600)}
async function call(path,body){
  const r=await fetch(base+path,body===undefined?{headers:headers(),credentials:'same-origin'}:{method:'POST',headers:headers(true),credentials:'same-origin',body:JSON.stringify(body)});
  const data=await r.json().catch(()=>({}));
  if(r.status===401){toast('Open this from the DJ booth to sign in.');return null}
  if(!r.ok){toast(data.error||'That did not work.');return null}
  return data;
}
function ago(at){const s=Math.max(0,Date.now()/1000-at);return s<90?'just now':s<3600?Math.round(s/60)+' min ago':Math.round(s/3600)+' h ago'}
function row(l,extra){
  const v=l.vote;
  return `<div class="row ${v==='down'?'down':''}" data-id="${esc(l.id)}">
    <button class="play ${playing===l.id?'on':''}" data-play="${esc(l.id)}" aria-label="Play">${playing===l.id?'&#9632;':'&#9654;'}</button>
    <div class="t"><b><span class="part" style="color:var(--${l.part})">${PARTS[l.part]}</span>${esc(l.title||'Untitled')}</b><span>${extra}</span></div>
    <div class="votes"><button class="vote up ${v==='up'?'on':''}" data-vote="up" aria-label="Thumbs up">&#128077;</button><button class="vote down ${v==='down'?'on':''}" data-vote="down" aria-label="Thumbs down">&#128078;</button></div>
  </div>`;
}
function render(){
  const byId=Object.fromEntries(D.loops.map(l=>[l.id,l]));
  $('#approved').checked=!!D.approved_only;
  const up=D.loops.filter(l=>l.vote==='up').length, down=D.loops.filter(l=>l.vote==='down').length;
  $('#count').textContent=`${D.loops.length} loops · ${up} 👍 · ${down} 👎`;
  $('#parts').innerHTML=[['all','All'],...Object.entries(PARTS)].map(([k,n])=>`<button class="chip ${part===k?'on':''}" data-part="${k}">${n} ${k==='all'?D.loops.length:D.loops.filter(l=>l.part===k).length}</button>`).join('');
  $('#verdicts').innerHTML=[['all','Any'],['new','Not rated'],['up','👍'],['down','👎']].map(([k,n])=>`<button class="chip ${verdict===k?'on':''}" data-verdict="${k}">${n}</button>`).join('');
  $('#playedBox').style.display=D.played.length?'':'none';
  $('#played').innerHTML=D.played.map(p=>byId[p.id]?row(byId[p.id],`${ago(p.at)}${p.over?' over '+esc(p.over):''}`):'').join('');
  const shown=D.loops.filter(l=>(part==='all'||l.part===part)&&(verdict==='all'||(verdict==='new'?!l.vote:l.vote===verdict)));
  $('#list').innerHTML=shown.map(l=>row(l,`${l.bpm} bpm · ${esc(l.camelot||'?')} · from ${Math.floor(l.start/60)}:${String(Math.floor(l.start%60)).padStart(2,'0')}`)).join('')||'<div class="empty">Nothing here.</div>';
}
async function load(){const d=await call('api/loops');if(d){D=d;render()}}
async function play(id,button){
  if(audio){audio.pause();audio=null}
  if(playing===id){playing=null;render();return}
  playing=id;render();
  button=document.querySelector(`[data-play="${CSS.escape(id)}"]`);button&&button.classList.add('wait');
  try{
    const r=await fetch(base+'api/loops/'+encodeURIComponent(id)+'/audio',{headers:headers(),credentials:'same-origin'});
    if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.error||'Could not play it.')}
    const url=URL.createObjectURL(await r.blob());
    if(playing!==id){URL.revokeObjectURL(url);return}
    audio=new Audio(url);audio.onended=()=>{if(playing===id){playing=null;render()}URL.revokeObjectURL(url)};
    await audio.play();
  }catch(e){toast(e.message);playing=null}
  render();
}
document.addEventListener('click',async e=>{
  const p=e.target.closest('[data-play]');if(p){play(p.dataset.play);return}
  const v=e.target.closest('[data-vote]');
  if(v){const id=v.closest('[data-id]').dataset.id;const l=D.loops.find(x=>x.id===id);const next=l&&l.vote===v.dataset.vote?null:v.dataset.vote;
    const d=await call('api/loops/'+encodeURIComponent(id)+'/vote',{vote:next});if(d){D=d;render();if(next==='down')toast('That one won’t be played again.')}return}
  const c=e.target.closest('[data-part]');if(c){part=c.dataset.part;render();return}
  const f=e.target.closest('[data-verdict]');if(f){verdict=f.dataset.verdict;render()}
});
$('#approved').addEventListener('change',async e=>{const d=await call('api/loops/settings',{approved_only:e.target.checked});if(d){D=d;render();toast(d.approved_only?'Only approved loops play now.':'Every loop not turned down can play.')}});
load();setInterval(load,15000);
</script>
</body></html>
"""
