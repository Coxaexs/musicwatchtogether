"""The DJ booth page, served at /musicbot/dj/?guild_id=... (see webui.py).

One page for Discord guilds and Huddle rooms alike: it only talks to
/api/guilds/<id>/dj. Laid out like an all-in-one DJ system - a screen with
scrolling waveforms on top, two decks with jog wheels and pads, a mixer in the
middle - with Simple, Medium and Advanced views that hide what you don't need.
"""

DJ_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>DJ Booth</title>
<style>
:root{color-scheme:dark;
--bg:#07080a;--panel:#16181c;--panel2:#1d2025;--edge:#2a2e35;--edge2:#3a3f48;--text:#eef1f5;--muted:#8b93a1;--dim:#5b6270;
--orange:#ff8a1c;--orange-d:#7a3d05;--green:#2fe07a;--green-d:#0f5a30;--blue:#2d8cff;--red:#ff3d5a;--amber:#ffb020;--cyan:#33d6ff;
--low:#1f6bff;--mid:#ff9a2e;--high:#f4f6ff;--screen:#050608;--r:12px}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);font:13px/1.35 "Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{min-height:100vh;background:
 radial-gradient(1200px 500px at 50% -200px,#23262d 0,transparent 70%),
 repeating-linear-gradient(90deg,#0b0c0f 0 2px,#0a0b0e 2px 4px)}
button{font:inherit;color:inherit;cursor:pointer;border:0;background:none;touch-action:manipulation}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
select,input[type=text],input[type=password]{background:#0d0f12;color:var(--text);border:1px solid var(--edge2);border-radius:8px;padding:8px 10px;font:inherit}
.hide{display:none!important}
body[data-mode=simple] .med,body[data-mode=simple] .adv,body[data-mode=medium] .adv{display:none!important}
body:not([data-mode=simple]) .simple-only{display:none!important}

/* ---------- top bar ---------- */
.top{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid #000;background:linear-gradient(#1c1f24,#121418);box-shadow:0 1px 0 #ffffff10 inset;position:sticky;top:0;z-index:20;flex-wrap:wrap}
.brand{font-weight:800;letter-spacing:.14em;font-size:14px;white-space:nowrap}
.brand b{color:var(--orange)}.brand small{color:var(--dim);font-weight:600;letter-spacing:.2em;margin-left:8px;font-size:10px}
.room{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:260px}
.spacer{flex:1}
.modes{display:flex;background:#0a0b0d;border:1px solid var(--edge);border-radius:999px;padding:3px}
.modes button{padding:6px 12px;border-radius:999px;font-weight:700;font-size:11px;letter-spacing:.08em;color:var(--muted)}
.modes button.on{background:var(--panel2);color:var(--text);box-shadow:0 0 0 1px var(--edge2) inset}
.onair{display:flex;align-items:center;gap:6px;font-weight:800;font-size:11px;letter-spacing:.12em;color:var(--dim)}
.onair i{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.onair.live{color:var(--red)}.onair.live i{background:var(--red);box-shadow:0 0 10px var(--red);animation:blink 1.4s infinite}
.pill{padding:7px 12px;border-radius:999px;border:1px solid var(--edge2);font-weight:700;font-size:11px;letter-spacing:.08em}
.pill.auto.on{background:var(--orange);border-color:var(--orange);color:#1a0d00;box-shadow:0 0 16px #ff8a1c66}
.pill.stemsw.on{background:#ff4fa3;border-color:#ff4fa3;color:#2a0016;box-shadow:0 0 16px #ff4fa366}
.pill.stemsw:disabled{opacity:.35;cursor:not-allowed}
.pill.real.on{background:linear-gradient(90deg,#ff3d5a,#ff8a1c);border-color:#ff5a3d;color:#fff;box-shadow:0 0 16px #ff3d5a77}
.pill.end:hover{border-color:var(--red);color:var(--red)}
@keyframes blink{50%{opacity:.35}}

/* ---------- console grid ---------- */
.console{display:grid;gap:12px;padding:12px;max-width:1500px;margin:0 auto;
 grid-template-columns:minmax(0,1fr) minmax(300px,360px) minmax(0,1fr);
 grid-template-areas:"screen screen screen" "deckA mixer deckB" "auto sampler browser"}
.screen{grid-area:screen}.deck-A{grid-area:deckA}.deck-B{grid-area:deckB}.mixer{grid-area:mixer}
.autodj{grid-area:auto}.sampler{grid-area:sampler}.browser{grid-area:browser}
.panel{background:linear-gradient(180deg,#1b1e23,#141619);border:1px solid #000;border-radius:var(--r);box-shadow:0 0 0 1px #ffffff0a inset,0 10px 30px #0008;padding:12px;min-width:0}
.label{font-size:10px;font-weight:700;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}

/* ---------- screen ---------- */
.screen{background:#0b0c0e;border:1px solid #000;border-radius:14px;padding:8px;box-shadow:0 0 0 1px #ffffff12 inset,0 20px 50px #000a}
.screen-in{background:var(--screen);border-radius:8px;border:1px solid #1e2228;overflow:hidden}
.sd{display:grid;grid-template-columns:auto 44px minmax(0,1fr) auto auto;gap:10px;align-items:center;padding:7px 10px;background:linear-gradient(#0e1114,#090b0d)}
.sd .letter{width:26px;height:26px;border-radius:6px;display:grid;place-items:center;font-weight:900;background:#1b2027;color:var(--muted)}
.sd.master .letter{background:var(--orange);color:#1a0d00}
.sd img{width:44px;height:44px;border-radius:5px;object-fit:cover;background:#15181c}
.sd .t{min-width:0}.sd .title{font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sd .artist{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}
.sd .badges{display:flex;gap:5px;margin-top:3px;flex-wrap:wrap}
.badge{font-size:10px;font-weight:800;letter-spacing:.06em;padding:1px 6px;border-radius:4px;background:#1b2027;color:var(--muted)}
.badge.key{color:#101;background:var(--amber)}.badge.sync{background:var(--cyan);color:#002}.badge.loop{background:var(--orange);color:#200}.badge.warn{background:var(--red);color:#fff}
.sd .clock{text-align:right;font-variant-numeric:tabular-nums}
.sd .clock b{display:block;font-size:20px;font-weight:800;letter-spacing:.02em}
.sd .clock span{color:var(--muted);font-size:11px}
.sd .clock b.low{color:var(--red);animation:blink .7s infinite}
.sd .bpm{text-align:right;font-variant-numeric:tabular-nums;min-width:86px}
.sd .bpm b{display:block;font-size:24px;font-weight:900;line-height:1}
.sd .bpm span{font-size:11px;color:var(--muted)}
.bars{font-size:11px;color:var(--amber);font-weight:800;font-variant-numeric:tabular-nums}
canvas.wave{display:block;width:100%;height:84px;background:#040506}
canvas.ov{display:block;width:100%;height:26px;background:#07080a;cursor:pointer;border-top:1px solid #111}
.tr{display:flex;align-items:center;gap:10px;padding:5px 10px;border-top:1px solid #15191e;border-bottom:1px solid #15191e;background:#07090b;min-height:30px;font-size:11px;color:var(--muted)}
.tr .bar{flex:1;height:5px;border-radius:3px;background:#161a20;overflow:hidden}
.tr .bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--blue),var(--orange))}
.tr b{color:var(--text)}

/* ---------- controls ---------- */
.btn{border-radius:8px;padding:8px 10px;font-weight:800;font-size:11px;letter-spacing:.08em;background:linear-gradient(#2a2e35,#1d2025);border:1px solid #000;box-shadow:0 1px 0 #ffffff14 inset,0 2px 4px #0006;color:#c8ced8;text-transform:uppercase;white-space:nowrap}
.btn:active{transform:translateY(1px)}
.btn.on{color:#fff;background:linear-gradient(#3a3f48,#2a2e35);box-shadow:0 0 0 1px var(--cyan) inset,0 0 12px #33d6ff55}
.btn.orange{color:var(--orange)}.btn.orange.on{background:var(--orange);color:#1a0d00;box-shadow:0 0 14px #ff8a1c88}
.btn.red.on{background:var(--red);color:#fff;box-shadow:0 0 14px #ff3d5a88}
.btn.small{padding:5px 7px;font-size:10px}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.row.between{justify-content:space-between}

.knob{display:flex;flex-direction:column;align-items:center;gap:2px;user-select:none;touch-action:none;cursor:ns-resize;width:50px}
.knob svg{width:44px;height:44px;overflow:visible}
.knob .kb{fill:url(#kg);stroke:#000;stroke-width:1.5}
.knob .kt{fill:none;stroke:#23272e;stroke-width:3;stroke-linecap:round}
.knob .ka{fill:none;stroke:var(--cyan);stroke-width:3;stroke-linecap:round}
.knob .kp{stroke:#fff;stroke-width:2.2;stroke-linecap:round}
.knob.kill .ka{stroke:var(--red)}
.knob label{font-size:9px;font-weight:800;letter-spacing:.12em;color:var(--muted);pointer-events:none}
.knob output{font-size:9px;color:var(--dim);font-variant-numeric:tabular-nums;height:11px}

input[type=range]{-webkit-appearance:none;appearance:none;background:transparent;margin:0}
input[type=range]::-webkit-slider-runnable-track{background:#050607;border-radius:4px;box-shadow:0 0 0 1px #000,0 1px 0 #ffffff12}
input[type=range]::-moz-range-track{background:#050607;border-radius:4px;box-shadow:0 0 0 1px #000}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;background:linear-gradient(90deg,#8d939c,#e9edf2 45%,#6d737c);border:1px solid #000;border-radius:3px;box-shadow:0 2px 6px #000c}
input[type=range]::-moz-range-thumb{background:linear-gradient(90deg,#8d939c,#e9edf2 45%,#6d737c);border:1px solid #000;border-radius:3px}
.vfader{writing-mode:vertical-lr;direction:rtl;width:34px;height:150px}
.vfader.pitch{direction:ltr;height:190px}
.vfader::-webkit-slider-runnable-track{width:6px}
.vfader::-webkit-slider-thumb{width:34px;height:18px;margin-left:-14px}
.vfader::-moz-range-thumb{width:32px;height:16px}
.hfader{width:100%;height:36px}
.hfader::-webkit-slider-runnable-track{height:6px}
.hfader::-webkit-slider-thumb{width:22px;height:34px;margin-top:-14px}
.hfader::-moz-range-thumb{width:20px;height:32px}
.fadercap{position:relative;display:flex;flex-direction:column;align-items:center;gap:6px}

/* ---------- decks ---------- */
.deck{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-content:start}
.deck-B{grid-template-columns:minmax(0,1fr) auto}
.deck .head{grid-column:1/-1;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.deck .head .big{font-size:22px;font-weight:900;color:var(--muted);margin-right:4px}
.jogwrap{display:grid;place-items:center;padding:4px 0}
.jog{position:relative;--js:min(250px,30vw);width:var(--js);height:var(--js);flex:none;border-radius:50%;touch-action:none;user-select:none;cursor:grab;
 background:radial-gradient(circle at 50% 50%,#2b2f36 0 56%,#15171b 57% 60%,#0b0c0e 61%),#0b0c0e;
 box-shadow:0 0 0 2px #000,0 0 0 5px #23272d,0 12px 30px #000c,inset 0 2px 1px #ffffff18}
.jog:active{cursor:grabbing}
.jog .ring{position:absolute;inset:-7px;border-radius:50%;background:conic-gradient(var(--c,var(--blue)) calc(var(--p,0)*1turn),#11141a 0);-webkit-mask:radial-gradient(circle,transparent 69%,#000 70%);mask:radial-gradient(circle,transparent 69%,#000 70%);opacity:.9}
.jog .plate{position:absolute;inset:10%;border-radius:50%;background:repeating-conic-gradient(from 0deg,#24282e 0 2deg,#1e2126 2deg 4deg);box-shadow:inset 0 0 0 1px #000,inset 0 10px 20px #ffffff08}
.jog .plate::after{content:"";position:absolute;left:50%;top:3%;width:4px;height:12%;margin-left:-2px;border-radius:2px;background:#e9edf2;box-shadow:0 0 6px #fff8}
.jog .hub{position:absolute;left:31%;top:31%;width:38%;height:38%;border-radius:50%;overflow:hidden;background:#0b0d10;box-shadow:0 0 0 3px #000,0 0 0 5px var(--c,var(--blue)),0 0 18px var(--c,var(--blue))}
.jog .hub img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.jog .hub img:not([src]){visibility:hidden}
.jog .hub b{position:absolute;inset:0;display:grid;place-items:center;font-size:26px;font-weight:900;color:#fff;text-shadow:0 2px 8px #000}
.jog .hub img[src]+b{opacity:0}
.deck.touch .jog{box-shadow:0 0 0 2px #000,0 0 0 5px var(--cyan),0 12px 30px #000c}
.tempo{display:flex;flex-direction:column;align-items:center;gap:6px;min-width:70px}
.tempo .readout{font-variant-numeric:tabular-nums;font-weight:800;font-size:12px}
.transport{display:flex;gap:14px;align-items:center}
.round{width:64px;height:64px;border-radius:50%;font-weight:900;font-size:12px;letter-spacing:.06em;background:radial-gradient(circle at 50% 35%,#343941,#191c20);border:2px solid #000;box-shadow:0 0 0 3px #2a2e35,0 6px 14px #000a}
.round.cue{color:var(--orange);box-shadow:0 0 0 3px var(--orange-d),0 6px 14px #000a}
.round.cue.on{box-shadow:0 0 0 3px var(--orange),0 0 18px #ff8a1c99}
.round.play{color:var(--green);font-size:20px;box-shadow:0 0 0 3px var(--green-d),0 6px 14px #000a}
.round.play.on{box-shadow:0 0 0 3px var(--green),0 0 20px #2fe07aaa}
.round.play.ready{animation:ready 1s infinite}
@keyframes ready{50%{box-shadow:0 0 0 3px var(--green),0 0 14px #2fe07a88}}
.pads{display:flex;flex-direction:column;gap:6px;flex:1;min-width:0}
.padmodes{display:flex;gap:4px;flex-wrap:wrap}
.padmodes button{font-size:9px;font-weight:800;letter-spacing:.08em;padding:4px 6px;border-radius:4px;border:1px solid var(--edge2);color:var(--muted)}
.padmodes button.on{border-color:var(--c);color:var(--c)}
.padgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.pad{aspect-ratio:1.35;border-radius:8px;background:#121418;border:2px solid var(--pc,#333);box-shadow:inset 0 0 0 1px #000,0 3px 6px #0008;font-size:10px;font-weight:800;color:#9aa1ad;display:grid;place-items:center;line-height:1.1;text-align:center;padding:2px;overflow:hidden}
.pad.lit{background:color-mix(in srgb,var(--pc) 70%,#000);color:#fff;box-shadow:0 0 14px color-mix(in srgb,var(--pc) 60%,transparent),inset 0 0 0 1px #0006}
.pad:active{transform:scale(.97)}
.deck-foot{grid-column:1/-1;display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap}
.loadbar{grid-column:1/-1;display:flex;gap:6px;flex-wrap:wrap}
.loadbar .btn{flex:1}
.deck .status{grid-column:1/-1;color:var(--muted);font-size:12px;min-height:16px}
.deck .status.err{color:var(--red)}
.modeseg{display:flex;margin:4px 0 10px}.modeseg button{flex:1;padding:8px 6px;font-weight:800;letter-spacing:.1em;font-size:11px}
.modeseg button[data-v=real].on{background:linear-gradient(90deg,#ff3d5a,#ff8a1c);color:#fff;box-shadow:0 0 14px #ff3d5a66}
.realinfo{font-size:12px;color:var(--muted);line-height:1.5;margin-bottom:8px}.realinfo b{color:var(--text)}
.realinfo .meter{height:5px;border-radius:3px;background:#161a20;overflow:hidden;margin:4px 0 2px}.realinfo .meter i{display:block;height:100%;background:linear-gradient(90deg,#2fe07a,#ffb020,#ff3d5a)}
body.realmode .smoothonly{display:none!important}
body:not(.realmode) .realonly{display:none!important}
.vibeseg button.on[data-v=chill]{background:#1f6bff;color:#fff}.vibeseg button.on[data-v=club]{background:#ff8a1c;color:#1a0d00}
.vibeseg button.on[data-v=hype]{background:linear-gradient(90deg,#ff3d5a,#ff4fa3);color:#fff;box-shadow:0 0 14px #ff3d5a66}
.stems{grid-column:1/-1;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}
.stem{position:relative;overflow:hidden;border-radius:8px;padding:9px 4px 11px;font-weight:800;font-size:10px;letter-spacing:.1em;background:#0d0f12;border:1px solid #000;box-shadow:0 1px 0 #ffffff10 inset;color:var(--dim)}
.stem i{position:absolute;left:0;bottom:0;height:3px;width:calc(var(--g,1)*100%);background:var(--pc);transition:width .2s}
.stem.lit{color:#fff;box-shadow:0 0 0 1px var(--pc) inset,0 0 12px color-mix(in srgb,var(--pc) 35%,transparent)}
.stems.wait .stem,.stems.off .stem{opacity:.35;pointer-events:none}
.stems.wait .stem i{animation:blink 1s infinite}

/* ---------- mixer ---------- */
.mixer{display:flex;flex-direction:column;gap:10px}
.mixgrid{display:grid;grid-template-columns:1fr auto 1fr;gap:8px}
.ch{display:flex;flex-direction:column;align-items:center;gap:4px}
.ch .name{font-weight:900;color:var(--muted)}
.chfoot{display:flex;gap:6px;align-items:flex-end}
.vu{display:flex;flex-direction:column-reverse;gap:2px;width:8px;height:150px;padding:1px 0}
.vu i{flex:1;border-radius:1px;background:#15181c}
.vu i.g{background:var(--green)}.vu i.y{background:var(--amber)}.vu i.r{background:var(--red)}
.center{display:flex;flex-direction:column;align-items:center;gap:8px;min-width:100px}
.mvu{display:flex;gap:3px}
.fx{background:#0e1013;border:1px solid #000;border-radius:10px;padding:8px;display:flex;flex-direction:column;gap:6px;width:100%}
.fxtypes{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
.fxtypes button{font-size:9px;font-weight:800;padding:5px 2px;border-radius:5px;background:#1a1d22;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.fxtypes button.on{background:#223;color:var(--cyan);box-shadow:0 0 0 1px var(--cyan) inset}
.fxbeat{display:flex;align-items:center;justify-content:space-between;gap:4px}
.fxbeat b{font-size:16px;font-variant-numeric:tabular-nums;min-width:40px;text-align:center}
.fxon{width:100%;height:40px;border-radius:50px;font-weight:900;letter-spacing:.14em;background:radial-gradient(circle at 50% 30%,#2d4b72,#172538);color:#9cc9ff;box-shadow:0 0 0 2px #000,0 0 0 4px #1e3350}
.fxon.on{background:radial-gradient(circle at 50% 30%,#7fc0ff,#2d8cff);color:#001;box-shadow:0 0 0 2px #000,0 0 22px #2d8cffcc}
.seg{display:flex;border:1px solid var(--edge2);border-radius:6px;overflow:hidden}
.seg button{flex:1;padding:4px 7px;font-size:10px;font-weight:800;color:var(--muted)}
.seg button.on{background:var(--edge2);color:#fff}
.xf{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center}
.xf b{font-weight:900;color:var(--muted)}
.mixbox{display:flex;flex-direction:column;gap:6px;background:#0e1013;border:1px solid #000;border-radius:10px;padding:8px}
.mixbtn{height:46px;border-radius:10px;font-weight:900;letter-spacing:.12em;font-size:13px;color:#1a0d00;background:linear-gradient(#ffa24a,#ff7a00);box-shadow:0 0 0 1px #000,0 6px 16px #ff7a0044}
.mixbtn:disabled{filter:grayscale(1) brightness(.5);cursor:default}
.hold{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.hold button{height:48px;border-radius:10px;font-weight:900;font-size:11px;letter-spacing:.08em;background:#101318;border:2px solid var(--pc);color:var(--pc)}
.hold button.lit{background:var(--pc);color:#000;box-shadow:0 0 16px var(--pc)}

/* ---------- auto dj / sampler / browser ---------- */
.autodj .headline{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}
.switch{position:relative;width:52px;height:28px;border-radius:14px;background:#2a2e35;box-shadow:inset 0 0 0 1px #000}
.switch::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#aeb4bd;transition:.2s}
.switch.on{background:var(--orange)}.switch.on::after{left:27px;background:#fff}
.chips{display:flex;gap:5px;flex-wrap:wrap;margin:6px 0}
.chips button{padding:5px 9px;border-radius:999px;border:1px solid var(--edge2);font-size:11px;font-weight:700;color:var(--muted)}
.chips button.on{background:#2a1a08;border-color:var(--orange);color:var(--orange)}
.next{margin-top:8px;padding:8px;border-radius:8px;background:#0e1013;color:var(--muted);font-size:12px}
.next b{color:var(--text)}
.sgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}
.sgrid .pad{aspect-ratio:1.5}
.tabs{display:flex;gap:4px;margin-bottom:8px}
.tabs button{padding:6px 10px;border-radius:7px;font-weight:800;font-size:11px;letter-spacing:.06em;color:var(--muted)}
.tabs button.on{background:var(--panel2);color:var(--text);box-shadow:0 0 0 1px var(--edge2) inset}
.list{display:flex;flex-direction:column;gap:4px;max-height:260px;overflow:auto}
.item{display:grid;grid-template-columns:36px minmax(0,1fr) auto;gap:8px;align-items:center;padding:5px;border-radius:8px;background:#101216}
.item img{width:36px;height:36px;border-radius:4px;object-fit:cover;background:#1b1e23}
.item .t{min-width:0}.item .t b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}
.item .t span{color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.item .row .btn{padding:5px 8px}
.searchrow{display:flex;gap:6px;flex-wrap:wrap}.searchrow input{flex:1;min-width:160px}
.empty{color:var(--dim);padding:14px;text-align:center}

/* ---------- overlays ---------- */
.overlay{position:fixed;inset:0;z-index:50;display:grid;place-items:center;background:radial-gradient(900px 600px at 50% 30%,#1d2128ee,#050608f5);padding:16px}
.card{width:min(520px,100%);background:linear-gradient(#1b1e23,#121418);border:1px solid #000;border-radius:18px;padding:24px;box-shadow:0 0 0 1px #ffffff12 inset,0 30px 80px #000}
.card h1{margin:0 0 6px;font-size:26px;letter-spacing:.02em}.card h1 b{color:var(--orange)}
.card p{color:var(--muted);margin:6px 0 16px}
.card .go{display:grid;gap:10px}
.card .go button{padding:14px;border-radius:12px;font-weight:800;font-size:15px;text-align:left;border:1px solid var(--edge2);background:#101216}
.card .go button small{display:block;color:var(--muted);font-weight:500;font-size:12px;margin-top:3px}
.card .go button.primary{background:linear-gradient(#ffa24a,#ff7a00);color:#1a0d00;border-color:#000}
.card .go button.primary small{color:#4a2400}
.card input{width:100%;margin-bottom:10px}
.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%) translateY(20px);opacity:0;transition:.25s;background:#1d2025;border:1px solid var(--edge2);padding:10px 14px;border-radius:10px;z-index:60;max-width:calc(100% - 32px);box-shadow:0 10px 30px #000a}
.toast.show{opacity:1;transform:translateX(-50%)}

@media (max-width:1100px){
 .console{grid-template-columns:minmax(0,1fr) minmax(0,1fr);grid-template-areas:"screen screen" "deckA deckB" "mixer mixer" "auto sampler" "browser browser"}
 .mixer{display:grid;grid-template-columns:1fr 1fr;align-items:start}.mixer>*{min-width:0}
 .mixer .mixgrid{grid-column:1/-1}
}
@media (max-width:760px){
 .console{grid-template-columns:minmax(0,1fr);grid-template-areas:"screen" "deckA" "mixer" "deckB" "auto" "sampler" "browser";padding:8px;gap:8px}
 .mixer{display:flex}
 .sd{grid-template-columns:auto 36px minmax(0,1fr) auto}.sd .clock{display:none}.sd img{width:36px;height:36px}
 .room{display:none}
 .top{padding:8px 10px}
 .jog{--js:min(230px,62vw)}
}
</style></head>
<body data-mode="medium">
<svg width="0" height="0" style="position:absolute"><defs>
<radialGradient id="kg" cx="50%" cy="35%" r="65%"><stop offset="0" stop-color="#5a6069"/><stop offset=".6" stop-color="#2b2f36"/><stop offset="1" stop-color="#15171b"/></radialGradient>
</defs></svg>

<header class="top">
  <div class="brand">HUDDLE <b>DJ</b><small>2-DECK BOOTH</small></div>
  <div class="room" id="room"></div>
  <div class="spacer"></div>
  <div class="modes" role="tablist" aria-label="Booth layout">
    <button data-mode="simple">SIMPLE</button><button data-mode="medium">MEDIUM</button><button data-mode="advanced">ADVANCED</button>
  </div>
  <div class="onair" id="onair"><i></i>OFF AIR</div>
  <button class="pill stemsw" id="stemsTop" title="Split songs into drums, bass, vocals and melody for stem controls and stem mixes">STEMS</button>
  <button class="pill auto" id="autoTop" title="Auto DJ mixes the queue for you, in order">AUTO DJ</button>
  <button class="pill real" id="realTop" title="Real DJ performs a set: picks songs from the queue, plays their best part, mashups, loop rolls and drops">REAL DJ</button>
  <button class="pill end" id="endBtn">END SET</button>
</header>

<main class="console" id="console">
  <section class="screen" aria-label="Screen">
    <div class="screen-in">
      <div class="sd" id="sdA"></div>
      <canvas class="wave" id="waveA"></canvas>
      <div class="tr" id="tr"><span id="trText">Two decks. Load a song on each and mix.</span><div class="bar"><i id="trBar"></i></div><span id="trRight"></span></div>
      <canvas class="wave" id="waveB"></canvas>
      <div class="sd" id="sdB"></div>
      <canvas class="ov" id="ovA" title="Deck A overview. Click to jump."></canvas>
      <canvas class="ov" id="ovB" title="Deck B overview. Click to jump."></canvas>
    </div>
  </section>
  <section class="panel deck deck-A" data-deck="A"></section>
  <section class="panel mixer" id="mixer"></section>
  <section class="panel deck deck-B" data-deck="B"></section>
  <section class="panel autodj" id="autodj"></section>
  <section class="panel sampler" id="sampler"></section>
  <section class="panel browser" id="browser"></section>
</main>

<div class="overlay hide" id="start"><div class="card">
  <h1>HUDDLE <b>DJ</b></h1>
  <p id="startRoom">Two live decks, a mixer, beat FX and a sampler. Everyone in the voice room hears the mix.</p>
  <div class="go">
    <button class="primary" data-go="auto">▶ Go live with Auto DJ<small>It mixes the queue for you, phrase-matched and beat-synced. Take over whenever you like.</small></button>
    <button data-go="manual">🎛️ Go live, manual<small>Empty decks. You load, cue and mix.</small></button>
  </div>
  <p style="margin:14px 0 0;font-size:12px" id="startNote">Whatever is playing now carries over onto deck A. Songs added with /play land in the DJ's queue.</p>
</div></div>
<div class="overlay hide" id="login"><div class="card">
  <h1>Booth <b>locked</b></h1><p>Open the booth from <b>/dj</b> in Discord or Huddle, or sign in with the dashboard password.</p>
  <input type="password" id="pw" placeholder="Dashboard password" autocomplete="current-password">
  <div class="go"><button class="primary" id="pwBtn">Unlock</button></div>
</div></div>
<div class="overlay hide" id="picker"><div class="card"><h1>Pick a <b>room</b></h1><div class="list" id="pickList"></div></div></div>
<div class="toast" id="toast"></div>

<script>
"use strict";
const $=(s,el=document)=>el.querySelector(s), $$=(s,el=document)=>[...el.querySelectorAll(s)];
const params=new URLSearchParams(location.search);
const guildId=params.get('guild_id')||'';
const base=location.pathname.replace(/dj\/?$/,'');
const apiUrl=base+'api/guilds/'+encodeURIComponent(guildId)+'/dj';
let token=new URLSearchParams(location.hash.slice(1)).get('token')||'';
try{ if(token) sessionStorage.setItem('dj_token:'+guildId,token); else token=sessionStorage.getItem('dj_token:'+guildId)||''; }catch(_){}
if(location.hash) history.replaceState(null,'',location.pathname+location.search);

const DECKS=['A','B'];
const HOT=['#ff3b6b','#ff8a1c','#ffd21c','#35d05f','#1cc8ff','#3b6bff','#b04bff','#ff4bd8'];
const DECKCOL={A:'#2d8cff',B:'#ff8a1c'};
const SAMPLE_NAMES={horn:'AIR HORN',siren:'SIREN',rewind:'REWIND',laser:'LASER',boom:'808 BOOM',clap:'CLAP',riser:'RISER',scratch:'SCRATCH'};
const FXNAME={echo:'ECHO',delay:'DELAY',reverb:'REVERB',flanger:'FLANGER',roll:'ROLL',trans:'TRANS'};
const COLORNAME={filter:'FILTER',space:'SPACE',dub_echo:'DUB ECHO',crush:'CRUSH',noise:'NOISE'};
let S=null, stateAt=0, CAT=null;
const waves={A:null,B:null}, ovCache={A:null,B:null};
const padMode={A:'hot',B:'hot'};
const heldUntil={};
const hold=(k,ms=900)=>{heldUntil[k]=performance.now()+ms};
const held=k=>(heldUntil[k]||0)>performance.now();

function headers(){const h={'Content-Type':'application/json'};if(token)h['X-MB-Session']=token;return h}
function toast(text){const t=$('#toast');t.textContent=text;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),3200)}
function fmt(sec,tenths){sec=Math.max(0,sec||0);const m=Math.floor(sec/60),s=sec-m*60;return m+':'+(tenths?s.toFixed(1).padStart(4,'0'):String(Math.floor(s)).padStart(2,'0'))}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

async function api(body){
  try{
    const r=await fetch(apiUrl,{method:'POST',headers:headers(),credentials:'same-origin',body:JSON.stringify(body)});
    const data=await r.json().catch(()=>({}));
    if(r.status===401){show('login');return null}
    if(!r.ok){toast(data.error||'That did not work.');return null}
    apply(data);return data;
  }catch(e){toast('Lost the connection to the booth.');return null}
}
const lanes={};
function live(key,body){
  hold(key);
  const lane=lanes[key]||(lanes[key]={t:null,body:null});
  lane.body=body;
  if(lane.t)return;
  const flush=()=>{if(!lane.body){lane.t=null;return}const b=lane.body;lane.body=null;api(b);lane.t=setTimeout(flush,50)};
  flush();
}

// ---------------------------------------------------------------- polling
let polling=false;
async function poll(){
  if(polling)return;polling=true;
  try{
    const need=!CAT||(S&&S.active&&DECKS.some(k=>S.decks[k].loaded&&(!waves[k]||waves[k].id!==S.decks[k].wave_id)));
    const r=await fetch(apiUrl+(need?'?full=1':''),{headers:headers(),credentials:'same-origin'});
    if(r.status===401){show('login');return}
    if(r.status===404||r.status===400){const d=await r.json().catch(()=>({}));toast(d.error||'Room not found.');return}
    if(r.ok)apply(await r.json());
  }catch(_){}finally{polling=false}
}
function show(which){for(const id of ['start','login','picker'])$('#'+id).classList.toggle('hide',id!==which)}

// ---------------------------------------------------------------- building
function knob(p){
  return `<div class="knob${p.cls?' '+p.cls:''}" data-deck="${p.deck||''}" data-param="${p.param}" data-min="${p.min}" data-max="${p.max}" data-def="${p.def}" data-kind="${p.kind||'bi'}" title="${p.title||p.label}. Drag up/down, double-click to reset.">
  <svg viewBox="0 0 44 44"><path class="kt"/><path class="ka"/><circle class="kb" cx="22" cy="22" r="14"/><line class="kp" x1="22" y1="22" x2="22" y2="11"/></svg>
  <label>${p.label}</label><output></output></div>`;
}
const STEMS=[['drums','DRUMS','#ffb020'],['bass','BASS','#1f6bff'],['vocals','VOCALS','#ff4fa3'],['other','MELODY','#2fe07a']];
function deckHTML(k){
  const loops=[1/4,1/2,1,2,4,8,16,32];
  return `
  <div class="head">
    <span class="big">${k}</span>
    <button class="btn small orange adv" data-op="loop_in">IN</button>
    <button class="btn small orange adv" data-op="loop_out">OUT</button>
    <button class="btn small orange adv" data-op="reloop">RELOOP/EXIT</button>
    <button class="btn small adv" data-op="loop_half">½×</button>
    <button class="btn small adv" data-op="loop_double">2×</button>
    <button class="btn small med" data-op="bjump" data-beats="-4" title="Beat jump back 4">◀◀</button>
    <button class="btn small med" data-op="bjump" data-beats="4" title="Beat jump forward 4">▶▶</button>
    <span style="flex:1"></span>
    <button class="btn small adv" data-flag="slip">SLIP</button>
    <button class="btn small adv" data-flag="quantize">QUANTIZE</button>
    <button class="btn small adv" data-flag="reverse">REV</button>
    <button class="btn small adv" data-flag="vinyl">VINYL</button>
  </div>
  <div class="jogwrap"><div class="jog" style="--c:${DECKCOL[k]}"><div class="ring"></div><div class="plate"></div><div class="hub"><img alt=""><b>${k}</b></div></div></div>
  <div class="tempo">
    <button class="btn med" data-op="sync">BEAT SYNC</button>
    <button class="btn small adv" data-op="master">MASTER</button>
    <div class="readout med" data-r="pitch">0.00%</div>
    <input type="range" class="vfader pitch med" data-pitch min="-8" max="8" step="0.01" value="0" aria-label="Tempo deck ${k}">
    <div class="seg adv" data-range><button data-v="6">6</button><button data-v="8">8</button><button data-v="16">16</button><button data-v="50">W</button></div>
    <div class="row adv"><button class="btn small" data-scale="0.5">÷2</button><button class="btn small" data-scale="2">×2</button></div>
    <button class="btn small adv" data-op="brake" title="Stop like a turntable losing power">BRAKE</button>
    <button class="btn small adv" data-op="spinback" title="Throw the record back">SPIN</button>
  </div>
  <div class="deck-foot">
    <div class="transport"><button class="round cue" data-op="cue" aria-label="Cue deck ${k}">CUE</button><button class="round play" data-op="play" aria-label="Play or pause deck ${k}">▶︎‖</button></div>
    <div class="pads med">
      <div class="padmodes" style="--c:${DECKCOL[k]}"><button data-pm="hot">HOT CUE</button><button class="adv" data-pm="loop">BEAT LOOP</button><button class="adv" data-pm="jump">BEAT JUMP</button><button class="adv" data-pm="sampler">SAMPLER</button></div>
      <div class="padgrid">${Array.from({length:8},(_,i)=>`<button class="pad" data-pad="${i}"></button>`).join('')}</div>
    </div>
    <button class="btn simple-only" data-op="sync" style="height:44px">SYNC</button>
  </div>
  <div class="stems med" data-stems title="Stems: click to mute or bring back a part, shift-click to solo it.">${STEMS.map(([n,l,c])=>`<button class="stem" data-stem="${n}" style="--pc:${c}">${l}<i></i></button>`).join('')}</div>
  <div class="loadbar"><button class="btn" data-op="load_next">⤓ LOAD NEXT</button><button class="btn med" data-op="eject">EJECT</button></div>
  <div class="status" data-r="status"></div>`;
}
function mixerHTML(){
  const ch=k=>`<div class="ch" data-deck="${k}">
    <span class="name">${k}</span>
    ${knob({deck:k,param:'trim',label:'TRIM',min:-12,max:12,def:0,cls:'adv'})}
    ${knob({deck:k,param:'eq_hi',label:'HI',min:-26,max:6,def:0,cls:'med'})}
    ${knob({deck:k,param:'eq_mid',label:'MID',min:-26,max:6,def:0,cls:'med'})}
    ${knob({deck:k,param:'eq_low',label:'LOW',min:-26,max:6,def:0,cls:'med'})}
    ${knob({deck:k,param:'color',label:'COLOR',min:-1,max:1,def:0,cls:'med',title:'Sound color FX: left = low-pass, right = high-pass'})}
    <select class="adv" data-colorfx aria-label="Color FX deck ${k}" style="padding:3px;font-size:10px;width:74px"></select>
    <div class="chfoot"><div class="vu" data-vu="${k}">${'<i></i>'.repeat(14)}</div><input type="range" class="vfader" data-fader min="0" max="1" step="0.005" value="1" aria-label="Channel fader ${k}"></div>
    <div class="seg adv" data-xf style="font-size:9px"><button data-v="A">A</button><button data-v="THRU">T</button><button data-v="B">B</button></div>
  </div>`;
  return `
  <div class="mixgrid">
    ${ch('A')}
    <div class="center">
      ${knob({param:'master',label:'MASTER',min:0,max:1.2,def:0.85,kind:'uni',cls:'med'})}
      <div class="mvu med"><div class="vu" data-vu="L">${'<i></i>'.repeat(14)}</div><div class="vu" data-vu="R">${'<i></i>'.repeat(14)}</div></div>
      <div class="fx med">
        <div class="label" style="text-align:center">BEAT FX</div>
        <div class="fxtypes">${['echo','delay','reverb','flanger','roll','trans'].map(t=>`<button data-fxtype="${t}">${FXNAME[t]}</button>`).join('')}</div>
        <div class="fxbeat"><button class="btn small" data-fxbeat="-1">◀</button><b id="fxBeat">1/2</b><button class="btn small" data-fxbeat="1">▶</button></div>
        <div class="row between">${knob({param:'fx_level',label:'LEVEL',min:0,max:1,def:0.5,kind:'uni'})}
        <div class="seg adv" id="fxTarget" style="flex-direction:column"><button data-v="A">CH A</button><button data-v="master">MST</button><button data-v="B">CH B</button></div></div>
        <button class="fxon" id="fxOn">ON</button>
      </div>
    </div>
    ${ch('B')}
  </div>
  <div class="mixbox">
    <div class="row between"><span class="label">MIX</span><select id="mixStyle" aria-label="Transition style" style="padding:4px 6px;font-size:11px"></select></div>
    <button class="mixbtn" id="mixBtn">MIX ▶</button>
  </div>
  <div class="hold simple-only" id="holdFx"></div>
  <div class="xf"><b>A</b><input type="range" class="hfader" id="xfader" min="-1" max="1" step="0.005" value="0" aria-label="Crossfader"><b>B</b></div>
  <div class="row between adv"><span class="label">X-FADER CURVE</span><div class="seg" id="xfCurve"><button data-v="smooth">SMOOTH</button><button data-v="sharp">CUT</button></div></div>`;
}
function build(){
  for(const k of DECKS)$('.deck-'+k).innerHTML=deckHTML(k);
  $('#mixer').innerHTML=mixerHTML();
  $('#autodj').innerHTML=`<div class="headline"><div><div class="label">AUTO DJ</div><div style="font-weight:800;font-size:15px">Let the booth mix</div></div><button class="switch" id="autoSwitch" aria-label="Auto DJ"></button></div>
    <div class="seg modeseg" id="autoMode" title="Auto DJ: plays the queue in order, smooth mixes. Real DJ: performs a set — picks songs, plays their best part, mashups and drops."><button data-v="auto">AUTO DJ</button><button data-v="real">REAL DJ</button></div>
    <div class="seg modeseg vibeseg realonly" id="vibeSeg" title="How much Real DJ plays the decks: loops, effects, sampler, drops"><button data-v="chill">CHILL</button><button data-v="club">CLUB</button><button data-v="hype">HYPE</button></div>
    <div class="realinfo" id="realInfo"></div>
    <div class="smoothonly"><div class="label">Transition</div><div class="chips" id="styleChips"></div></div>
    <div class="row between med smoothonly"><span class="label">Length</span><select id="autoBeats" style="padding:4px 6px"><option value="">Style default</option><option value="4">1 bar</option><option value="8">2 bars</option><option value="16">4 bars</option><option value="32">8 bars</option><option value="64">16 bars</option></select></div>
    <div class="next" id="nextUp">…</div>`;
  $('#sampler').innerHTML=`<div class="row between"><div class="label">SAMPLER</div>${knob({param:'sampler',label:'VOL',min:0,max:1,def:0.7,kind:'uni',cls:'adv'})}</div><div class="sgrid" id="sgrid"></div>`;
  $('#browser').innerHTML=`<div class="tabs"><button data-tab="queue" class="on">QUEUE</button><button data-tab="search">SEARCH</button><button data-tab="history">HISTORY</button></div>
    <div data-panel="queue"><div class="list" id="queueList"></div></div>
    <div data-panel="search" class="hide"><div class="searchrow"><input type="text" id="q" placeholder="Song, artist or link" aria-label="Search"><button class="btn" data-load="A">LOAD A</button><button class="btn" data-load="B">LOAD B</button><button class="btn" data-queue>+ QUEUE</button></div><p style="color:var(--muted);font-size:12px">Loading decodes the whole song so cues, loops and scratching are instant. It takes a few seconds.</p></div>
    <div data-panel="history" class="hide"><div class="list" id="histList"></div></div>`;
  wire();
}

// ---------------------------------------------------------------- knobs
function setKnob(el,v){
  const min=+el.dataset.min,max=+el.dataset.max,def=+el.dataset.def;
  el._v=v;
  const a=a=>(-135+270*(a-min)/(max-min))*Math.PI/180;
  const pt=(ang,r)=>[22+r*Math.sin(ang),22-r*Math.cos(ang)];
  const arc=(from,to,r)=>{const[x1,y1]=pt(from,r),[x2,y2]=pt(to,r);const large=Math.abs(to-from)>Math.PI?1:0;const sweep=to>from?1:0;return `M${x1} ${y1}A${r} ${r} 0 ${large} ${sweep} ${x2} ${y2}`};
  $('.kt',el).setAttribute('d',arc(a(min),a(max),19));
  const from=el.dataset.kind==='uni'?a(min):a(def);
  const to=a(v);
  $('.ka',el).setAttribute('d',Math.abs(to-from)<0.001?'':arc(Math.min(from,to),Math.max(from,to),19));
  const [px,py]=pt(to,11);const p=$('.kp',el);p.setAttribute('x2',px);p.setAttribute('y2',py);
  const param=el.dataset.param;
  el.classList.toggle('kill',param.startsWith('eq_')&&v<=-25.5);
  let text='';
  if(param.startsWith('eq_'))text=v<=-25.5?'KILL':(v>0?'+':'')+v.toFixed(0)+'dB';
  else if(param==='trim')text=(v>0?'+':'')+v.toFixed(1);
  else if(param==='color')text=Math.abs(v)<0.03?'':(v<0?'LPF ':'HPF ')+Math.round(Math.abs(v)*100);
  else text=Math.round(v*100)+'%';
  $('output',el).textContent=text;
}
function knobSend(el,v){
  const d=el.dataset.deck,param=el.dataset.param;
  const key='k:'+d+param;
  if(d)live(key,{op:'set',deck:d,param,value:v});
  else if(param==='fx_level')live(key,{op:'fx',level:v});
  else live(key,{op:'set_mixer',param,value:v});
}
function wireKnob(el){
  let y0=0,v0=0,id=null;
  const range=()=>(+el.dataset.max)-(+el.dataset.min);
  el.addEventListener('pointerdown',e=>{id=e.pointerId;el.setPointerCapture(id);y0=e.clientY;v0=el._v??+el.dataset.def;e.preventDefault()});
  el.addEventListener('pointermove',e=>{if(e.pointerId!==id)return;
    let v=v0+(y0-e.clientY)/(e.shiftKey?600:160)*range();
    v=Math.min(+el.dataset.max,Math.max(+el.dataset.min,v));
    const def=+el.dataset.def;if(el.dataset.kind!=='uni'&&Math.abs(v-def)<range()*0.02)v=def;
    setKnob(el,v);knobSend(el,v)});
  const end=e=>{if(e.pointerId===id)id=null};
  el.addEventListener('pointerup',end);el.addEventListener('pointercancel',end);
  el.addEventListener('dblclick',()=>{const v=+el.dataset.def;setKnob(el,v);knobSend(el,v)});
  el.addEventListener('wheel',e=>{e.preventDefault();let v=(el._v??+el.dataset.def)-Math.sign(e.deltaY)*range()/40;v=Math.min(+el.dataset.max,Math.max(+el.dataset.min,v));setKnob(el,v);knobSend(el,v)},{passive:false});
}

// ---------------------------------------------------------------- wiring
function deckOf(el){return el.closest('[data-deck]')?.dataset.deck}
function wire(){
  $$('.knob').forEach(k=>{wireKnob(k);setKnob(k,+k.dataset.def)});
  $$('.modes button').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
  for(const k of DECKS){
    const el=$('.deck-'+k);
    el.addEventListener('click',e=>{
      const b=e.target.closest('button');if(!b)return;
      const op=b.dataset.op;
      if(op==='cue')return;
      if(op==='bjump')return api({op:'beat_jump',deck:k,beats:+b.dataset.beats});
      if(op==='spinback')return api({op:'brake',deck:k,spinback:true});
      if(op==='load_next')return api({op:'load_next',deck:k});
      if(b.dataset.stem){const g=S?.decks?.[k]?.stems?.gain||{};const n=b.dataset.stem;
        if(e.shiftKey){for(const [m] of STEMS)api({op:'set',deck:k,param:'stem_'+m,value:m===n?1:0});return}
        return api({op:'set',deck:k,param:'stem_'+n,value:(g[n]??1)>0.5?0:1})}
      if(op)return api({op,deck:k});
      if(b.dataset.flag)return api({op:'flag',deck:k,name:b.dataset.flag});
      if(b.dataset.scale)return api({op:'bpm_scale',deck:k,value:+b.dataset.scale});
      if(b.dataset.pm){padMode[k]=b.dataset.pm;renderDeck(k);return}
      if(b.closest('[data-range]'))return api({op:'set',deck:k,param:'range',value:+b.dataset.v});
    });
    const cue=$('[data-op=cue]',el);
    cue.addEventListener('pointerdown',e=>{e.preventDefault();cue.setPointerCapture(e.pointerId);api({op:'cue',deck:k,down:true})});
    cue.addEventListener('pointerup',()=>api({op:'cue',deck:k,down:false}));
    $$('.pad',el).forEach(p=>wirePad(p,k));
    const pitch=$('[data-pitch]',el);
    pitch.addEventListener('input',()=>live('pitch'+k,{op:'set',deck:k,param:'pitch',value:+pitch.value}));
    pitch.addEventListener('dblclick',()=>{pitch.value=0;live('pitch'+k,{op:'set',deck:k,param:'pitch',value:0})});
    wireJog(k);
  }
  $$('.ch').forEach(ch=>{
    const k=ch.dataset.deck;
    const f=$('[data-fader]',ch);f.addEventListener('input',()=>live('fader'+k,{op:'set',deck:k,param:'fader',value:+f.value}));
    const sel=$('[data-colorfx]',ch);sel.addEventListener('change',()=>api({op:'set',deck:k,param:'color_fx',value:sel.value}));
    $$('[data-xf] button',ch).forEach(b=>b.onclick=()=>api({op:'set',deck:k,param:'xf',value:b.dataset.v}));
  });
  const xf=$('#xfader');xf.addEventListener('input',()=>live('xfader',{op:'set_mixer',param:'xfader',value:+xf.value}));
  xf.addEventListener('dblclick',()=>{xf.value=0;live('xfader',{op:'set_mixer',param:'xfader',value:0})});
  $$('#xfCurve button').forEach(b=>b.onclick=()=>api({op:'set_mixer',param:'xf_curve',value:b.dataset.v}));
  $$('[data-fxtype]').forEach(b=>b.onclick=()=>api({op:'fx',type:b.dataset.fxtype}));
  $$('[data-fxbeat]').forEach(b=>b.onclick=()=>{const cur=S?.fx?.beat??2;api({op:'fx',beat:Math.max(0,Math.min(7,cur+ +b.dataset.fxbeat))})});
  $$('#fxTarget button').forEach(b=>b.onclick=()=>api({op:'fx',target:b.dataset.v}));
  $('#fxOn').onclick=()=>api({op:'fx',on:!S?.fx?.on});
  $('#mixBtn').onclick=()=>api({op:'mix',style:$('#mixStyle').value});
  $('#mixStyle').onchange=()=>api({op:'auto',style:$('#mixStyle').value});
  $('#stemsTop').onclick=()=>api({op:'stems',on:!S?.stems?.on}).then(r=>r&&toast(S?.stems?.on?'Stems on: songs get split into parts.':'Stems off.'));
  $('#autoSwitch').onclick=()=>api({op:'auto',on:!S?.auto?.on});
  // AUTO DJ and REAL DJ: press one to run it, press the lit one again to stop.
  const modeBtn=(mode,msg)=>()=>{const a=S?.auto||{};const lit=a.on&&(a.mode||'auto')===mode;
    if(lit)return api({op:'auto',on:false});
    api({op:'auto',mode,on:true}).then(r=>r&&toast(msg))};
  const VIBE_MSG={chill:'Chill: long smooth blends, few tricks.',club:'Club: loops, effects and drops most phrases.',hype:'Hype: tricks all the time, drops, sampler.'};
  $$('#vibeSeg button').forEach(b=>b.onclick=()=>api({op:'auto',vibe:b.dataset.v}).then(r=>r&&toast(VIBE_MSG[b.dataset.v])));
  $('#autoTop').onclick=modeBtn('auto','Auto DJ: the queue in order, smooth mixes.');
  $('#realTop').onclick=modeBtn('real','Real DJ: picking songs and playing their best parts.');
  $$('#autoMode button').forEach(b=>b.onclick=()=>api({op:'auto',mode:b.dataset.v}).then(r=>r&&toast(b.dataset.v==='real'?'Real DJ: picking songs and playing the best parts.':'Auto DJ: the queue in order.')));
  $('#autoBeats').onchange=()=>api({op:'auto',beats:$('#autoBeats').value?+$('#autoBeats').value:null});
  $('#endBtn').onclick=()=>{if(confirm('End the DJ set? The room goes back to its normal queue.'))api({op:'stop'})};
  $$('#browser .tabs button').forEach(b=>b.onclick=()=>{$$('#browser .tabs button').forEach(x=>x.classList.toggle('on',x===b));$$('#browser [data-panel]').forEach(p=>p.classList.toggle('hide',p.dataset.panel!==b.dataset.tab))});
  $$('#browser [data-load]').forEach(b=>b.onclick=()=>{const q=$('#q').value.trim();if(!q)return toast('Type a song first.');api({op:'load',deck:b.dataset.load,query:q}).then(r=>r&&toast('Loading on deck '+b.dataset.load+'…'))});
  $('#browser [data-queue]').onclick=()=>{const q=$('#q').value.trim();if(!q)return toast('Type a song first.');api({op:'queue',query:q}).then(r=>{if(r)$('#q').value=''})};
  $('#q').addEventListener('keydown',e=>{if(e.key==='Enter')$('#browser [data-queue]').click()});
  $('#queueList').addEventListener('click',e=>{const b=e.target.closest('[data-qload]');if(b)api({op:'load',deck:b.dataset.qload,index:+b.dataset.i})});
  for(const k of DECKS)$('#ov'+k).addEventListener('click',e=>{const d=S?.decks?.[k];if(!d?.loaded)return;const r=e.currentTarget.getBoundingClientRect();api({op:'seek',deck:k,value:(e.clientX-r.left)/r.width*d.duration})});
  $$('[data-go]').forEach(b=>b.onclick=()=>{b.disabled=true;api({op:'start',auto:b.dataset.go==='auto',full:true}).finally(()=>b.disabled=false)});
  $('#pwBtn').onclick=unlock;$('#pw').addEventListener('keydown',e=>{if(e.key==='Enter')unlock()});
  document.addEventListener('keydown',keys);
}
function wirePad(p,k){
  let timer=null,long=false;
  p.addEventListener('pointerdown',e=>{
    e.preventDefault();long=false;const i=+p.dataset.pad;const mode=padMode[k];
    if(mode==='hot'){timer=setTimeout(()=>{long=true;api({op:'hotcue',deck:k,index:i,clear:true});toast('Hot cue '+'ABCDEFGH'[i]+' cleared')},600)}
  });
  p.addEventListener('pointerup',e=>{
    clearTimeout(timer);if(long)return;
    const i=+p.dataset.pad,mode=padMode[k];
    if(mode==='hot')api({op:'hotcue',deck:k,index:i,clear:e.shiftKey});
    else if(mode==='loop')api({op:'loop',deck:k,beats:[1/4,1/2,1,2,4,8,16,32][i]});
    else if(mode==='jump')api({op:'beat_jump',deck:k,beats:[-1,-4,-8,-16,1,4,8,16][i]});
    else if(mode==='sampler')api({op:'sample',name:(CAT?.samples||[])[i]});
  });
  p.addEventListener('pointerleave',()=>clearTimeout(timer));
}
async function unlock(){
  const r=await fetch(base+'api/session',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({password:$('#pw').value})});
  if(!r.ok)return toast('Wrong password.');
  token='';$('#pw').value='';show(null);poll();
}
function keys(e){
  if(e.target.matches('input[type=text],input[type=password],select'))return;
  const k=e.key.toLowerCase();
  const map={z:['play','A'],m:['play','B'],a:['sync','A'],k:['sync','B']};
  if(map[k]){e.preventDefault();api({op:map[k][0],deck:map[k][1]})}
  else if(k==='arrowleft'||k==='arrowright'){e.preventDefault();const x=$('#xfader');x.value=Math.max(-1,Math.min(1,+x.value+(k==='arrowleft'?-0.1:0.1)));live('xfader',{op:'set_mixer',param:'xfader',value:+x.value})}
  else if(k==='f'&&S?.active){e.preventDefault();api({op:'fx',on:!S.fx.on})}
}

// ---------------------------------------------------------------- jog wheels
function wireJog(k){
  const jog=$('.deck-'+k+' .jog');const deck=$('.deck-'+k);
  let id=null,last=0,lastT=0,mode=null,acc=0;
  const angle=e=>{const r=jog.getBoundingClientRect();return Math.atan2(e.clientY-(r.top+r.height/2),e.clientX-(r.left+r.width/2))};
  const radius=e=>{const r=jog.getBoundingClientRect();return Math.hypot(e.clientX-(r.left+r.width/2),e.clientY-(r.top+r.height/2))/(r.width/2)};
  jog.addEventListener('pointerdown',e=>{
    const d=S?.decks?.[k];if(!d?.loaded)return;
    id=e.pointerId;jog.setPointerCapture(id);last=angle(e);lastT=performance.now();acc=0;
    // Vinyl mode: the top of the platter grabs the record. The side ring
    // (or vinyl off) nudges the tempo instead, like a CDJ.
    mode=d.vinyl&&radius(e)<0.78?'scratch':(d.playing?'bend':'search');
    deck.classList.add('touch');
    if(mode==='scratch')live('jog'+k,{op:'scratch',deck:k,value:0});
    e.preventDefault();
  });
  jog.addEventListener('pointermove',e=>{
    if(e.pointerId!==id)return;
    const a=angle(e),now=performance.now();
    let da=a-last;if(da>Math.PI)da-=2*Math.PI;if(da<-Math.PI)da+=2*Math.PI;
    const dt=Math.max(8,now-lastT)/1000;last=a;lastT=now;
    const turns=da/(2*Math.PI);
    if(mode==='scratch')live('jog'+k,{op:'scratch',deck:k,value:(turns*1.8)/dt});
    else if(mode==='bend')live('jog'+k,{op:'bend',deck:k,value:Math.max(-1,Math.min(1,turns/dt*1.2))});
    else{acc+=turns*1.8;if(Math.abs(acc)>0.02){const d=S.decks[k];live('jog'+k,{op:'seek',deck:k,value:livePos(d)+acc});acc=0}}
  });
  const end=e=>{if(e.pointerId!==id)return;id=null;deck.classList.remove('touch');
    const lane=lanes['jog'+k];if(lane)lane.body=null;
    if(mode==='scratch')api({op:'scratch',deck:k,value:null});
    if(mode==='bend')api({op:'bend',deck:k,value:0});
    mode=null};
  jog.addEventListener('pointerup',end);jog.addEventListener('pointercancel',end);
}

// ---------------------------------------------------------------- render
function setMode(m){
  document.body.dataset.mode=m;
  $$('.modes button').forEach(b=>b.classList.toggle('on',b.dataset.mode===m));
  try{localStorage.setItem('dj_mode',m)}catch(_){}
  resize();
}
function beatText(d,pos){
  if(!d.bpm)return '';
  const beat=Math.floor((pos-d.first_beat)*d.bpm/60);
  if(beat<0)return '';
  return `${Math.floor(beat/4)+1}.${(beat%4)+1}`;
}
function apply(st){
  if(!st)return;
  if(st.catalog)CAT=st.catalog;
  if(!st.active){S=st;stateAt=performance.now();renderInactive();return}
  show(null);
  for(const k of DECKS){
    const d=st.decks[k];
    if(!d.loaded){waves[k]=null;ovCache[k]=null}
    else if(d.wave&&(!waves[k]||waves[k].id!==d.wave_id)){
      const dec=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
      waves[k]={id:d.wave_id,rate:d.wave.rate,low:dec(d.wave.low),mid:dec(d.wave.mid),high:dec(d.wave.high)};
      ovCache[k]=null;
    }
  }
  S=st;stateAt=performance.now();
  for(const k of DECKS)renderDeck(k);
  renderMixer();renderAuto();renderBrowser();
  if(st.notice)toast(st.notice);
}
function renderInactive(){
  $('#onair').className='onair';$('#onair').lastChild.textContent='OFF AIR';
  show('start');
  if(!guildId)pickRoom();
}
function renderDeck(k){
  const d=S.decks[k],el=$('.deck-'+k),sd=$('#sd'+k);
  const isMaster=S.mixer.master_deck===k;
  const liveBpm=d.bpm?d.bpm*(1+d.pitch/100):null;
  sd.classList.toggle('master',isMaster);
  const title=d.loading?'Loading '+d.loading+'…':(d.title||(d.error?'Load failed':'Empty deck'));
  const sig=[title,d.artist,d.error,d.thumbnail,d.camelot,d.sync,isMaster,d.loop&&d.loop.on,d.slip,liveBpm&&liveBpm.toFixed(1),d.pitch,d.loaded].join('|');
  if(sd._sig!==sig){sd._sig=sig;sd.innerHTML=`<span class="letter">${k}</span>${d.thumbnail?`<img src="${esc(d.thumbnail)}" alt="">`:'<img alt="">'}
    <div class="t"><div class="title">${esc(title)}</div><div class="artist">${esc(d.artist||(d.error?d.error:(d.loaded?'':'Press LOAD NEXT or search')))}</div>
    <div class="badges">${d.camelot?`<span class="badge key" title="${esc(d.key||'')}">${esc(d.camelot)}</span>`:''}${d.sync?'<span class="badge sync">SYNC</span>':''}${isMaster&&d.loaded?'<span class="badge">MASTER</span>':''}${d.loop&&d.loop.on?'<span class="badge loop">LOOP</span>':''}${d.slip?'<span class="badge">SLIP</span>':''}<span class="bars" data-bars></span></div></div>
    <div class="clock"><b data-remain>-0:00.0</b><span data-elapsed>0:00</span></div>
    <div class="bpm"><b>${liveBpm?liveBpm.toFixed(1):'--.-'}</b><span>${d.loaded?(d.pitch>=0?'+':'')+d.pitch.toFixed(2)+'%':'BPM'}</span></div>`;}
  const img=$('.hub img',el);
  if(d.thumbnail){if(img.getAttribute('src')!==d.thumbnail)img.src=d.thumbnail}else img.removeAttribute('src');
  const on=(sel,v)=>$$(sel,el).forEach(b=>b.classList.toggle('on',!!v));
  const play=$('[data-op=play]',el);play.classList.toggle('on',d.playing);play.classList.toggle('ready',d.loaded&&!d.playing);
  $('[data-op=cue]',el).classList.toggle('on',d.loaded&&!d.playing&&Math.abs(d.pos-d.cue)<0.05);
  on('[data-op=sync]',d.sync);on('[data-op=master]',isMaster);
  for(const f of ['slip','quantize','reverse','vinyl'])on(`[data-flag=${f}]`,d[f]);
  const lp=d.loop;on('[data-op=reloop]',lp&&lp.on);on('[data-op=loop_in]',lp&&lp.in!=null&&!lp.out);on('[data-op=loop_out]',lp&&lp.on);
  $$('[data-range] button',el).forEach(b=>b.classList.toggle('on',+b.dataset.v===d.range));
  const pitch=$('[data-pitch]',el);
  pitch.min=-d.range;pitch.max=d.range;
  if(!held('pitch'+k))pitch.value=d.pitch;
  $('[data-r=pitch]',el).textContent=(d.pitch>=0?'+':'')+d.pitch.toFixed(2)+'%';
  $$('.padmodes button',el).forEach(b=>b.classList.toggle('on',b.dataset.pm===padMode[k]));
  const pads=$$('.pad',el),mode=padMode[k];
  pads.forEach((p,i)=>{
    let label='',color='#333',lit=false;
    if(mode==='hot'){const t=d.hotcues[i];color=HOT[i];lit=t!=null;label=`${'ABCDEFGH'[i]}${t!=null?'<br><small>'+fmt(t)+'</small>':''}`}
    else if(mode==='loop'){const b=[1/4,1/2,1,2,4,8,16,32][i];color='#ff8a1c';label=b<1?'1/'+(1/b):String(b);lit=!!(lp&&lp.on&&d.bpm&&Math.abs((lp.out-lp.in)-b*60/d.bpm)<0.01)}
    else if(mode==='jump'){const b=[-1,-4,-8,-16,1,4,8,16][i];color='#b04bff';label=(b<0?'◀ ':'')+Math.abs(b)+(b>0?' ▶':'')}
    else{const n=(CAT?.samples||[])[i];color=HOT[(i+4)%8];label=SAMPLE_NAMES[n]||n||''}
    p.style.setProperty('--pc',color);p.classList.toggle('lit',lit);if(p.innerHTML!==label)p.innerHTML=label;
  });
  const st=d.stems||{},sg=st.gain||{},box=$('[data-stems]',el);
  box.classList.toggle('hide',!S.stems?.on);
  box.classList.toggle('wait',d.loaded&&st.status==='separating');
  box.classList.toggle('off',!d.loaded||(st.status!=='ready'&&st.status!=='separating'));
  $$('.stem',box).forEach(b=>{const kar=d.karaoke&&b.dataset.stem==='vocals';const g=kar?0:(sg[b.dataset.stem]??1);b.style.setProperty('--g',g);b.classList.toggle('lit',st.status==='ready'&&g>0.5);
    b.firstChild.textContent=kar?'🎤 KARAOKE':(STEMS.find(x=>x[0]===b.dataset.stem)||[])[1];b.title=kar?'Karaoke mode is on for this room: vocals stay out':''});
  const status=$('[data-r=status]',el);
  status.textContent=d.error?('⚠ '+d.error):(d.loading?'Decoding and analysing…':
    (d.loaded&&st.status==='separating'?'Splitting stems… (plays normally meanwhile)':(d.loaded&&st.status==='failed'?'Stems unavailable for this song.':'')));
  status.classList.toggle('err',!!d.error);
  const ch=$(`.ch[data-deck=${k}]`);
  if(ch){
    for(const kn of $$('.knob',ch)){const p=kn.dataset.param,key='k:'+k+p;if(held(key))continue;
      const v=p==='trim'?d.trim:p==='color'?d.color:d.eq[p.slice(3)];if(v!=null&&v!==kn._v)setKnob(kn,v)}
    const f=$('[data-fader]',ch);if(!held('fader'+k))f.value=d.fader;
    const sel=$('[data-colorfx]',ch);
    if(CAT&&!sel.options.length)sel.innerHTML=CAT.color_fx.map(c=>`<option value="${c}">${COLORNAME[c]||c}</option>`).join('');
    sel.value=d.color_fx;
    $$('[data-xf] button',ch).forEach(b=>b.classList.toggle('on',b.dataset.v===d.xf));
  }
}
function renderMixer(){
  const m=S.mixer,fx=S.fx;
  const sw=$('#stemsTop'),stm=S.stems||{};
  sw.classList.toggle('on',!!stm.on);sw.disabled=!stm.available;
  sw.title=stm.available?(stm.on?'Stems on: click to turn off for this room':'Stems off: click to split songs into drums, bass, vocals and melody'):'Stem separation is not installed on this bot';
  $('#onair').className='onair'+(S.live?' live':'');$('#onair').lastChild.textContent=S.live?'ON AIR':'STARTING…';
  if(!held('xfader'))$('#xfader').value=m.xfader;
  $$('#xfCurve button').forEach(b=>b.classList.toggle('on',b.dataset.v===m.xf_curve));
  for(const kn of $$('.knob:not([data-deck=A]):not([data-deck=B])')){
    const p=kn.dataset.param;if(held('k:'+p))continue;
    const v=p==='master'?m.master:p==='sampler'?m.sampler:p==='fx_level'?fx.level:null;
    if(v!=null&&v!==kn._v)setKnob(kn,v);
  }
  $$('[data-fxtype]').forEach(b=>b.classList.toggle('on',b.dataset.fxtype===fx.type));
  $('#fxBeat').textContent=(CAT?.fx_beats||[])[fx.beat]||'';
  $$('#fxTarget button').forEach(b=>b.classList.toggle('on',b.dataset.v===fx.target));
  $('#fxOn').classList.toggle('on',fx.on);
  const sel=$('#mixStyle');
  if(CAT&&!sel.options.length)sel.innerHTML=CAT.styles.map(s=>`<option value="${s.id}">${esc(s.label)}</option>`).join('');
  if(document.activeElement!==sel)sel.value=S.auto.style;
  const tr=S.transition;
  const playing=DECKS.filter(k=>S.decks[k].playing);
  const other=playing.length===1?(playing[0]==='A'?'B':'A'):null;
  const btn=$('#mixBtn');
  btn.disabled=!(other&&S.decks[other].loaded)||!!tr;
  btn.textContent=tr?(tr.started?'MIXING…':'ARMED'):(other?`MIX ${playing[0]} ▶ ${other}`:'MIX ▶');
  $('#trText').innerHTML=tr?`<b>${esc(tr.label)}</b> ${tr.from} → ${tr.to} · ${tr.started?Math.round(tr.progress*100)+'%':'starts in '+Math.max(0,Math.ceil(tr.starts_in_beats??0))+' beats'}`:
    (S.auto.on?(S.auto.mode==='real'?'<b>Real DJ</b> is on. It plays each song\'s best part and mixes into the next one\'s big moment.':'<b>Auto DJ</b> is on. It mixes the next song in on a phrase.'):'Load a song on each deck, then hit <b>MIX</b> or ride the crossfader.');
  $('#trBar').style.width=tr&&tr.started?(tr.progress*100)+'%':'0';
  $('#trRight').textContent=tr?'':'';
  // Simple mode: hold-to-use FX pads on the master.
  const hold=$('#holdFx');
  if(!hold.children.length){
    const colors={echo:'#2d8cff',reverb:'#b04bff',flanger:'#1cc8ff',roll:'#ff8a1c',trans:'#ff3b6b',delay:'#35d05f'};
    hold.innerHTML=['echo','reverb','flanger','roll','trans','delay'].map(t=>`<button data-hold="${t}" style="--pc:${colors[t]}">${FXNAME[t]}</button>`).join('');
    $$('[data-hold]',hold).forEach(b=>{
      b.addEventListener('pointerdown',e=>{e.preventDefault();b.setPointerCapture(e.pointerId);b.classList.add('lit');api({op:'fx',type:b.dataset.hold,target:'master',level:0.65,beat:b.dataset.hold==='roll'?2:(b.dataset.hold==='trans'?1:3),on:true})});
      const up=()=>{b.classList.remove('lit');api({op:'fx',on:false})};
      b.addEventListener('pointerup',up);b.addEventListener('pointercancel',up);
    });
  }
}
function renderAuto(){
  const a=S.auto;
  const real=a.mode==='real';
  document.body.classList.toggle('realmode',real);
  $$('#autoMode button').forEach(b=>b.classList.toggle('on',b.dataset.v===(a.mode||'auto')));
  $$('#vibeSeg button').forEach(b=>b.classList.toggle('on',b.dataset.v===(a.vibe||'club')));

  const ri=$('#realInfo');
  if(real&&a.real){const pl=a.real.plan;const lbl=pl?(pl.label||(CAT?.styles||[]).find(x=>x.id===pl.style)?.label||pl.style):null;
    const html=`<div>Set energy now <b>${Math.round(a.real.energy_target*100)}%</b></div><div class="meter"><i style="width:${Math.round(a.real.energy_target*100)}%"></i></div>`
      +(pl?`<div>Next mix: <b>${esc(lbl)}</b>${pl.ride?` · tempo ride ${pl.ride>0?'+':''}${pl.ride.toFixed(1)}%`:''}</div>${pl.why?`<div style="opacity:.7;font-size:12px">${esc(pl.why.replace(/ \(keys .*$/,''))}</div>`:''}`:(a.real.next?`<div>Loading <b>${esc(a.real.next)}</b>…</div>`:'<div>Picks the next song from the queue by tempo, key and mood, and plays its best part.</div>'));
    if(ri._h!==html){ri._h=html;ri.innerHTML=html}}
  else if(ri._h!==''){ri._h='';ri.innerHTML=''}
  $('#autoSwitch').classList.toggle('on',a.on);$('#autoTop').classList.toggle('on',a.on&&!real);$('#realTop').classList.toggle('on',a.on&&real);
  const chips=$('#styleChips');
  if(CAT&&!chips.children.length){chips.innerHTML=CAT.styles.map(s=>`<button data-style="${s.id}">${esc(s.label)}</button>`).join('');
    $$('button',chips).forEach(b=>b.onclick=()=>api({op:'auto',style:b.dataset.style}))}
  $$('button',chips).forEach(b=>b.classList.toggle('on',b.dataset.style===a.style));
  const sel=$('#autoBeats');if(document.activeElement!==sel)sel.value=a.beats?String(a.beats):'';
  const next=S.crate[0];
  const tr=S.transition;
  let html=tr?`Mixing into <b>${esc(S.decks[tr.to].title||'deck '+tr.to)}</b>`:
    real?(S.crate.length?`${S.crate.length} songs in the crate. Real DJ picks from all of them.`:'The crate is empty. Queue a playlist and Real DJ picks from it.'):
    (next?`Up next: <b>${esc(next.title||next.query)}</b>`:'The queue is empty. Add songs with <b>/play</b> or the search tab'+(S.kind==='discord'&&a.on?', or Smart Autoplay picks one.':'.'));
  $('#nextUp').innerHTML=html;
  const sg=$('#sgrid');
  if(CAT&&!sg.children.length){sg.innerHTML=CAT.samples.map((n,i)=>`<button class="pad" data-sample="${n}" style="--pc:${HOT[(i+4)%8]}">${SAMPLE_NAMES[n]||n}</button>`).join('');
    $$('[data-sample]',sg).forEach(b=>b.addEventListener('pointerdown',e=>{e.preventDefault();b.classList.add('lit');setTimeout(()=>b.classList.remove('lit'),180);api({op:'sample',name:b.dataset.sample})}))}
}
function renderBrowser(){
  const q=S.crate||[];
  const ql=$('#queueList');
  const key=JSON.stringify(q.map(i=>i.title));
  if(ql._key!==key){ql._key=key;
    ql.innerHTML=q.length?q.map((it,i)=>`<div class="item"><img ${it.thumbnail?`src="${esc(it.thumbnail)}"`:''} alt=""><div class="t"><b>${esc(it.title||it.query)}</b><span>${esc(it.artist||'')}</span></div><div class="row"><button class="btn" data-qload="A" data-i="${i}">A</button><button class="btn" data-qload="B" data-i="${i}">B</button></div></div>`).join('')
      :'<div class="empty">Nothing queued. /play adds songs here.</div>'}
  const hl=$('#histList');const hk=JSON.stringify((S.history||[]).map(h=>h.at));
  if(hl._key!==hk){hl._key=hk;hl.innerHTML=(S.history||[]).map(it=>`<div class="item"><img ${it.thumbnail?`src="${esc(it.thumbnail)}"`:''} alt=""><div class="t"><b>${esc(it.title)}</b><span>${esc(it.artist||'')}</span></div><span class="label">${new Date(it.at*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</span></div>`).join('')||'<div class="empty">Loaded songs show up here.</div>'}
  $('#browser [data-tab=queue]').textContent='QUEUE'+(q.length?' ('+q.length+')':'');
}

// ---------------------------------------------------------------- animation
function livePos(d){
  if(!d||!d.loaded)return 0;
  let p=d.pos+(d.rate||0)*(performance.now()-stateAt)/1000;
  const lp=d.loop;
  if(lp&&lp.on&&lp.out>lp.in&&p>=lp.out&&(d.rate||0)>0)p=lp.in+((p-lp.in)%(lp.out-lp.in));
  return Math.max(0,Math.min(d.duration,p));
}
const cv={};
function resize(){
  for(const id of ['waveA','waveB','ovA','ovB']){
    const c=$('#'+id);const r=c.getBoundingClientRect();const dpr=Math.min(2,devicePixelRatio||1);
    const w=Math.max(10,Math.round(r.width*dpr)),h=Math.max(10,Math.round(r.height*dpr));
    if(c.width!==w||c.height!==h){c.width=w;c.height=h;if(id.startsWith('ov'))ovCache[id.slice(2)]=null}
    cv[id]=c;
  }
}
function drawWave(k,d,pos){
  const c=cv['wave'+k];if(!c)return;const g=c.getContext('2d');const W=c.width,H=c.height;
  g.fillStyle='#040506';g.fillRect(0,0,W,H);
  const w=waves[k];
  if(!d.loaded||!w){g.fillStyle='#1b1f25';g.font=`${Math.round(H/5)}px system-ui`;g.textAlign='center';g.fillText(d.loading?'LOADING…':'NO TRACK',W/2,H/2+H/14);return}
  const span=10; // seconds across the view
  const pxPerSec=W/span, t0=pos-span/2, mid=H/2;
  // loop region
  const lp=d.loop;
  if(lp&&lp.in!=null&&lp.out!=null){g.fillStyle=lp.on?'#ff8a1c30':'#ff8a1c14';const x1=(lp.in-t0)*pxPerSec,x2=(lp.out-t0)*pxPerSec;g.fillRect(x1,0,x2-x1,H)}
  const n=w.low.length;
  const step=Math.max(1,Math.round(W/600));
  for(let x=0;x<W;x+=step){
    const i=Math.floor((t0+x/pxPerSec)*w.rate);if(i<0||i>=n)continue;
    const lo=w.low[i]/255*mid*0.95,mi=w.mid[i]/255*mid*0.8,hi=w.high[i]/255*mid*0.6;
    g.fillStyle='#1f6bff';g.fillRect(x,mid-lo,step,lo*2);
    g.fillStyle='#ff9a2e';g.fillRect(x,mid-mi,step,mi*2);
    g.fillStyle='#f4f6ff';g.fillRect(x,mid-hi,step,hi*2);
  }
  if(pos>0){g.fillStyle='#00000055';g.fillRect(0,0,W/2,H)}
  // beat grid
  if(d.bpm){const bs=60/d.bpm;let b=Math.ceil((t0-d.first_beat)/bs);
    for(let t=d.first_beat+b*bs;t<t0+span;t+=bs,b++){const x=Math.round((t-t0)*pxPerSec);const down=((b%4)+4)%4===0;g.fillStyle=down?'#ff3d5acc':'#ffffff40';g.fillRect(x,0,down?2:1,down?H:H*0.14);if(!down)g.fillRect(x,H-H*0.14,1,H*0.14)}}
  // cues
  const mark=(t,color,label)=>{const x=(t-t0)*pxPerSec;if(x<-20||x>W+20)return;g.fillStyle=color;g.fillRect(x-1,0,2,H);g.beginPath();g.moveTo(x-7,0);g.lineTo(x+7,0);g.lineTo(x,9);g.fill();if(label){g.font=`bold ${Math.round(H/7)}px system-ui`;g.fillStyle=color;g.fillText(label,x+4,H-4)}};
  mark(d.cue,'#ff8a1c');
  d.hotcues.forEach((t,i)=>{if(t!=null)mark(t,HOT[i],'ABCDEFGH'[i])});
  g.fillStyle='#fff';g.fillRect(W/2-1,0,2,H);g.fillStyle='#ff3d5a';g.fillRect(W/2-1,0,2,6);g.fillRect(W/2-1,H-6,2,6);
}
function drawOverview(k,d,pos){
  const c=cv['ov'+k];if(!c)return;const g=c.getContext('2d');const W=c.width,H=c.height;const w=waves[k];
  if(!d.loaded||!w){g.fillStyle='#07080a';g.fillRect(0,0,W,H);return}
  if(!ovCache[k]){
    const o=document.createElement('canvas');o.width=W;o.height=H;const og=o.getContext('2d');og.fillStyle='#07080a';og.fillRect(0,0,W,H);
    const n=w.low.length;
    for(let x=0;x<W;x++){const a=Math.floor(x/W*n),b=Math.max(a+1,Math.floor((x+1)/W*n));let lo=0,mi=0,hi=0;for(let i=a;i<b&&i<n;i++){lo=Math.max(lo,w.low[i]);mi=Math.max(mi,w.mid[i]);hi=Math.max(hi,w.high[i])}
      og.fillStyle='#1f6bff';og.fillRect(x,H-lo/255*H,1,lo/255*H);og.fillStyle='#ff9a2e';og.fillRect(x,H-mi/255*H*0.8,1,mi/255*H*0.8);og.fillStyle='#e8ecff';og.fillRect(x,H-hi/255*H*0.55,1,hi/255*H*0.55)}
    ovCache[k]=o;
  }
  g.drawImage(ovCache[k],0,0);
  const x=pos/d.duration*W;g.fillStyle='#000000a0';g.fillRect(0,0,x,H);
  d.hotcues.forEach((t,i)=>{if(t!=null){g.fillStyle=HOT[i];g.fillRect(t/d.duration*W-1,0,2,H*0.4)}});
  if(d.music_end&&S.auto.on){g.fillStyle='#ff8a1c';g.fillRect(d.music_end/d.duration*W-1,0,2,H)}
  g.fillStyle='#fff';g.fillRect(x-1,0,2,H);
}
const vuLevel={};
function setVu(id,level){
  const el=$(`[data-vu="${id}"]`);if(!el)return;
  const prev=vuLevel[id]||0;const v=Math.max(level,prev*0.9);vuLevel[id]=v;
  const db=20*Math.log10(Math.max(1e-5,v));const n=Math.round(Math.max(0,Math.min(14,(db+36)/39*14)));
  if(el._n===n)return;el._n=n;
  [...el.children].forEach((seg,i)=>{seg.className=i<n?(i>=12?'r':i>=9?'y':'g'):''});
}
function frame(){
  if(S&&S.active){
    for(const k of DECKS){
      const d=S.decks[k];const pos=livePos(d);
      drawWave(k,d,pos);drawOverview(k,d,pos);
      const el=$('.deck-'+k);const jog=$('.jog',el);
      const ang=pos*360/1.8;
      $('.plate',jog).style.transform=`rotate(${ang}deg)`;
      const img=$('.hub img',jog);img.style.transform=`rotate(${ang}deg)`;
      jog.style.setProperty('--p',d.loaded&&d.duration?(pos/d.duration).toFixed(4):0);
      const sd=$('#sd'+k);
      const remain=d.loaded?d.duration-pos:0;
      const r=$('[data-remain]',sd),e=$('[data-elapsed]',sd),bars=$('[data-bars]',sd);
      if(r){r.textContent='-'+fmt(remain,true);r.classList.toggle('low',d.playing&&remain<30);}
      if(e)e.textContent=fmt(pos)+' / '+fmt(d.duration);
      if(bars)bars.textContent=d.loaded?beatText(d,pos):'';
      setVu(k,d.level);
    }
    setVu('L',S.mixer.level[0]);setVu('R',S.mixer.level[1]);
  }
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- start
async function pickRoom(){
  show('picker');
  try{
    const r=await fetch(base+'api/guilds',{headers:headers(),credentials:'same-origin'});
    if(r.status===401)return show('login');
    const data=await r.json();
    $('#pickList').innerHTML=(data.guilds||[]).map(g=>`<a class="item" style="color:inherit;text-decoration:none" href="?guild_id=${encodeURIComponent(g.id)}"><img ${g.icon?`src="${esc(g.icon)}"`:''} alt=""><div class="t"><b>${esc(g.name)}</b><span>${g.connected?'Bot connected':''}</span></div><span></span></a>`).join('')||'<div class="empty">No rooms.</div>';
  }catch(_){toast('Could not list rooms.')}
}
async function roomName(){
  try{const r=await fetch(base+'api/guilds',{headers:headers(),credentials:'same-origin'});if(!r.ok)return;const data=await r.json();
    const g=(data.guilds||[]).find(g=>g.id===guildId);if(g){$('#room').textContent=g.name;$('#startRoom').innerHTML=`Going live in <b>${esc(g.name)}</b>. Everyone in the voice room hears the mix.`;document.title='DJ Booth · '+g.name}}catch(_){}
}
async function init(){
  build();
  let mode='medium';try{mode=localStorage.getItem('dj_mode')||(innerWidth<760?'simple':'medium')}catch(_){}
  setMode(mode);
  addEventListener('resize',resize);
  if(!guildId){pickRoom();return}
  if(token){try{await fetch(base+'api/session',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({token,guild_id:guildId})})}catch(_){}}
  roomName();
  await poll();
  const loop=async()=>{await poll();setTimeout(loop,document.hidden?1500:220)};loop();
  requestAnimationFrame(frame);
}
init();
</script>
</body></html>
"""
