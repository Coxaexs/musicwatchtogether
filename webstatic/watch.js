/* Watch Together: one synced player for a Hoffle voice room. */
(function () {
  'use strict';
  const T = window.Together, h = T.h, $ = T.$;
  const v = $('#v'), stage = $('#stage');
  let st = null;            // last room state
  let clock = null;         // {index, playing, position, now}: where the room is
  let mediaBase = '';
  let reactions = [];
  let loadedSrc = '', hls = null, blocked = false, seekingUntil = 0, lastHardSeek = 0;
  let dragging = -1, searchState = { q: '', items: null, busy: false, error: '' };
  let bPlayer = null;

  const PLAY_ICON = T.icon('play', 'fill'), PAUSE_ICON = T.icon('pause', 'fill');
  $('#skipBtn').append(T.icon('skip', 'fill'));
  $('#muteBtn').append(T.icon('volume'));
  $('#reactBtn').append(T.icon('smile'));
  $('#fsBtn').append(T.icon('expand'));
  $('#welcomeIcon').append(T.icon('film'));

  // ------------------------------------------------------------ helpers
  const current = () => (st && st.index >= 0 ? st.queue[st.index] : null);
  const settings = () => (st && st.settings) || {};
  const canControl = () => T.can('control_policy', settings());
  const isMine = (item) => item && item.added_by && T.me && item.added_by.id === T.me.id;
  function expected() {
    if (!clock) return 0;
    return clock.playing ? clock.position + (T.serverNow() - clock.now) / 1000 : clock.position;
  }
  function setClock(d) {
    clock = { index: d.index, playing: d.playing, position: d.position, now: d.now };
  }
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) return resolve();
      const s = h('script', { src });
      s.onload = resolve; s.onerror = reject;
      document.head.append(s);
    });
  }

  // ------------------------------------------------------------ messages
  T.boot({
    onMessage(d) {
      if (d.t === 'welcome') {
        mediaBase = d.media;
        reactions = d.reactions || [];
        $('#reactPop').replaceChildren(T.reactionPicker(reactions, (emoji) => {
          T.send({ t: 'react', emoji });
          $('#reactPop').classList.add('hidden');
        }));
      } else if (d.t === 'state') {
        const before = current();
        st = d;
        setClock(d);
        if (!before || !current() || before.uid !== current().uid) blocked = false;
        render();
      } else if (d.t === 'sync') {
        if (!st) return;
        if (d.index !== st.index) { st.index = d.index; render(); }
        setClock(d);
        if (d.by && d.action && d.by.id !== (T.me && T.me.id)) {
          const verb = d.action === 'play' ? 'pressed play' : d.action === 'pause' ? 'paused' : 'jumped to ' + T.fmt(d.position);
          T.toast(`${d.by.name} ${verb}`, { by: d.by, ms: 1600 });
        }
        reconcile(true);
      } else if (d.t === 'progress') {
        const item = st && st.queue.find((it) => it.uid === d.uid);
        if (item) { item.progress = d.pct; renderProgress(item); }
      } else if (d.t === 'react') {
        T.float($('#reactions'), d.emoji, d.by);
      } else if (d.t === 'results') {
        if (d.q !== searchState.q) return;
        searchState = { q: d.q, items: d.items, busy: false, error: d.error || '' };
        renderSearch();
      } else if (d.t === 'bmedia') {
        showMedia(d);
      } else if (d.t === 'bcursor') {
        showCursor(d);
      }
    },
  });

  // ------------------------------------------------------------ the video
  function srcFor(item) {
    return mediaBase + item.file.split('/').map(encodeURIComponent).join('/');
  }
  function setSource(item) {
    const src = srcFor(item);
    if (src === loadedSrc) return;
    loadedSrc = src;
    if (hls) { try { hls.destroy(); } catch (_e) { /* gone */ } hls = null; }
    const startAt = expected();
    const seekOnce = () => {
      if (Math.abs(v.currentTime - expected()) > 0.5) v.currentTime = Math.max(0, expected());
      v.removeEventListener('loadedmetadata', seekOnce);
    };
    v.addEventListener('loadedmetadata', seekOnce);
    if (item.file.endsWith('.m3u8')) {
      // hls.js wherever it runs (some Chromes claim native HLS and then fail);
      // the browser's own HLS only where hls.js can't (iPhone Safari).
      const native = () => { v.src = src; v.load(); };
      loadScript('/watch/static/hls.min.js').then(() => {
        if (loadedSrc !== src) return;
        if (!window.Hls || !window.Hls.isSupported()) { native(); return; }
        hls = new window.Hls({ startPosition: startAt });
        hls.on(window.Hls.Events.ERROR, (_event, data) => {
          if (!data.fatal || !hls) return;
          if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
          else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        });
        hls.loadSource(src);
        hls.attachMedia(v);
      }).catch(native);
    } else {
      v.src = src;
      v.load();
    }
  }
  function clearSource() {
    if (!loadedSrc) return;
    loadedSrc = '';
    if (hls) { try { hls.destroy(); } catch (_e) { /* gone */ } hls = null; }
    v.pause();
    v.removeAttribute('src');
    v.load();
  }
  function tryPlay() {
    const p = v.play();
    if (p && p.catch) {
      p.then(() => { if (blocked) { blocked = false; renderStage(); } })
        .catch((err) => { if (err && err.name === 'NotAllowedError' && !blocked) { blocked = true; renderStage(); } });
    }
  }

  /** Bring the local player to where the room is. Small drift is eased out
   *  with the playback rate; big drift is a seek. */
  function reconcile(fromSync) {
    const item = current();
    if (!st || !item || item.status !== 'ready' || !item.file || (st.browser && st.browser.active)) return;
    if (!loadedSrc) return;
    if (clock.playing && v.paused && !blocked && !v.ended) tryPlay();
    if (!clock.playing && !v.paused) v.pause();
    if (v.readyState < 1 || Date.now() < seekingUntil) return;
    const want = expected();
    // A stream still downloading reports only what has arrived as its length.
    const duration = item.duration || (isFinite(v.duration) ? v.duration : 0);
    if (duration && want >= duration - 0.25) return;   // the end: 'ended' moves the room on
    const drift = v.currentTime - want;
    const now = Date.now();
    let sync = 'on';
    if (Math.abs(drift) > (clock.playing ? 1.5 : 0.35)) {
      if (now - lastHardSeek > 1500 || fromSync) {
        v.currentTime = Math.max(0, want);
        lastHardSeek = now;
      }
      v.playbackRate = 1;
      sync = 'catch';
    } else if (clock.playing && Math.abs(drift) > 0.2) {
      v.playbackRate = drift > 0 ? 0.94 : 1.06;
      sync = 'catch';
    } else {
      v.playbackRate = 1;
    }
    const el = $('#sync');
    el.className = 'sync' + (sync === 'catch' ? ' catch' : '') + (T.connected ? '' : ' off');
    el.querySelector('b').textContent = !T.connected ? 'Reconnecting' : sync === 'catch' ? 'Catching up' : 'In sync';
  }
  setInterval(() => { reconcile(false); paintControls(); }, 400);

  v.addEventListener('ended', () => {
    if (st) T.send({ t: 'ended', index: st.index });
  });
  v.addEventListener('progress', paintControls);
  v.addEventListener('playing', () => { if (blocked) { blocked = false; renderStage(); } });

  // ------------------------------------------------------------ controls
  function act(type, extra) {
    if (!canControl()) { T.toast('The host has locked playback', { kind: 'error' }); return false; }
    T.send(Object.assign({ t: type }, extra || {}));
    return true;
  }
  function togglePlay() {
    if (blocked) { v.muted = false; blocked = false; tryPlay(); renderStage(); return; }
    if (!clock || !current()) return;
    const pos = loadedSrc ? v.currentTime : expected();
    if (clock.playing) {
      if (act('pause', { pos })) { clock = { index: clock.index, playing: false, position: pos, now: T.serverNow() }; v.pause(); }
    } else if (act('play', { pos })) {
      clock = { index: clock.index, playing: true, position: pos, now: T.serverNow() };
      tryPlay();
    }
    paintControls();
  }
  function seekTo(pos) {
    if (!act('seek', { pos })) return;
    clock = { index: clock.index, playing: clock.playing, position: pos, now: T.serverNow() };
    v.currentTime = pos;
    seekingUntil = Date.now() + 800;
  }
  $('#playBtn').onclick = togglePlay;
  $('#skipBtn').onclick = () => T.send({ t: 'skip' });
  $('#fsBtn').onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (stage.requestFullscreen) stage.requestFullscreen().catch(() => {});
    else if (v.webkitEnterFullscreen) v.webkitEnterFullscreen();
  };
  $('#reactBtn').onclick = (e) => { e.stopPropagation(); $('#reactPop').classList.toggle('hidden'); };
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#reactPop')) $('#reactPop').classList.add('hidden');
  });

  const vol = $('#volume');
  let savedVol = 1;
  try { savedVol = parseFloat(localStorage.getItem('together:volume') || '1'); } catch (_e) { /* fine */ }
  v.volume = isFinite(savedVol) ? Math.min(1, Math.max(0, savedVol)) : 1;
  vol.value = v.volume;
  vol.oninput = () => {
    v.volume = parseFloat(vol.value); v.muted = v.volume === 0;
    try { localStorage.setItem('together:volume', String(v.volume)); } catch (_e) { /* fine */ }
    paintVolume();
  };
  $('#muteBtn').onclick = () => { v.muted = !v.muted; paintVolume(); };
  function paintVolume() {
    $('#muteBtn').replaceChildren(T.icon(v.muted || v.volume === 0 ? 'mute' : 'volume'));
  }

  // seek bar
  const seek = $('#seek');
  function seekFraction(e) {
    const r = seek.getBoundingClientRect();
    return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  }
  function durationNow() {
    const item = current();
    return (item && item.duration) || (isFinite(v.duration) && v.duration ? v.duration : 0);
  }
  let seekDrag = false;
  seek.addEventListener('pointerdown', (e) => {
    if (!canControl() || !durationNow()) return;
    seekDrag = true; seek.setPointerCapture(e.pointerId);
    $('#seekFill').style.width = (seekFraction(e) * 100) + '%';
  });
  seek.addEventListener('pointermove', (e) => {
    const d = durationNow();
    if (!d) return;
    const tip = $('#seekTip');
    tip.classList.remove('hidden');
    tip.textContent = T.fmt(seekFraction(e) * d);
    tip.style.left = (seekFraction(e) * 100) + '%';
    if (seekDrag) $('#seekFill').style.width = (seekFraction(e) * 100) + '%';
  });
  seek.addEventListener('pointerleave', () => $('#seekTip').classList.add('hidden'));
  seek.addEventListener('pointerup', (e) => {
    if (!seekDrag) return;
    seekDrag = false;
    seekTo(seekFraction(e) * durationNow());
  });
  seek.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      seekTo(Math.max(0, expected() + (e.key === 'ArrowLeft' ? -10 : 10)));
    }
  });

  function paintControls() {
    const item = current();
    const playing = clock && clock.playing && !blocked;
    const btn = $('#playBtn');
    const want = playing ? PAUSE_ICON : PLAY_ICON;
    if (btn.firstChild !== want) btn.replaceChildren(want);
    btn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    btn.disabled = !item;
    const d = durationNow();
    const t = loadedSrc && v.readyState >= 1 ? v.currentTime : expected();
    $('#time').textContent = T.fmt(Math.min(t, d || t)) + ' / ' + T.fmt(d);
    if (!seekDrag) $('#seekFill').style.width = d ? Math.min(100, (t / d) * 100) + '%' : '0';
    let buffered = 0;
    try { if (v.buffered.length) buffered = v.buffered.end(v.buffered.length - 1); } catch (_e) { /* none */ }
    $('#seekBuf').style.width = d ? Math.min(100, (buffered / d) * 100) + '%' : '0';
    seek.classList.toggle('locked', !canControl());
    const votes = st && settings().skip_policy === 'vote' && !T.isManager();
    const skip = $('#skipBtn');
    skip.disabled = !item;
    const label = votes && st.skip_votes ? `${st.skip_votes}/${st.skip_needed}` : '';
    if ((skip.dataset.label || '') !== label) {
      skip.dataset.label = label;
      skip.replaceChildren(T.icon('skip', 'fill'), label ? h('span.count', null, label) : null);
    }
    skip.title = votes ? 'Vote to skip' : 'Skip';
  }

  // hide controls while watching
  let idleTimer = null;
  function wake() {
    stage.classList.remove('idle');
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      if (clock && clock.playing && !blocked && !$('#controls').matches(':hover')) stage.classList.add('idle');
    }, 2800);
  }
  stage.addEventListener('pointermove', wake);
  stage.addEventListener('pointerdown', wake);
  stage.addEventListener('dblclick', (e) => { if (e.target === v) $('#fsBtn').click(); });
  v.addEventListener('click', togglePlay);

  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, select, textarea, canvas')) return;
    if (e.key === ' ' || e.key === 'k') { e.preventDefault(); togglePlay(); }
    else if (e.key === 'f') $('#fsBtn').click();
    else if (e.key === 'm') $('#muteBtn').click();
    else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && current()) {
      e.preventDefault(); seekTo(Math.max(0, expected() + (e.key === 'ArrowLeft' ? -10 : 10)));
    }
    wake();
  });

  // ------------------------------------------------------------ rendering
  function render() {
    renderStage();
    renderNow();
    renderQueue();
    renderRoom();
    renderBrowser();
    paintControls();
    $('#queueCount').textContent = st.queue.length > Math.max(0, st.index + 1)
      ? String(st.queue.length - Math.max(0, st.index + 1)) : '';
    $('#peopleCount').textContent = st.members.length ? String(st.members.length) : '';
  }

  let embedUid = null;
  function renderStage() {
    const item = current();
    const msg = $('#stageMsg');
    const browser = st && st.browser && st.browser.active;
    $('#welcome').classList.toggle('hidden', Boolean(item) || Boolean(browser) || !st);
    $('#controls').classList.toggle('hidden', !item || Boolean(browser) || (item && item.status === 'embed'));
    // embed (yt-dlp could not download): the site's own player, not synced
    if (item && item.status === 'embed' && !browser) {
      if (embedUid !== item.uid) {
        embedUid = item.uid;
        clearSource();
        const src = item.embed_kind === 'youtube'
          ? `https://www.youtube-nocookie.com/embed/${encodeURIComponent(item.embed)}?autoplay=1&playsinline=1`
          : item.embed;
        $('#embedBox').replaceChildren(
          h('iframe', { src, allow: 'autoplay; fullscreen; encrypted-media; picture-in-picture', allowfullscreen: true,
            referrerpolicy: 'no-referrer', sandbox: 'allow-scripts allow-same-origin allow-presentation allow-popups' }),
          h('div.embed-note', null, 'Playing from the site itself, so it is not synced. Skip when you are done.'));
      }
      $('#embedBox').classList.remove('hidden');
    } else if (embedUid) {
      embedUid = null;
      $('#embedBox').replaceChildren();
      $('#embedBox').classList.add('hidden');
    }
    if (item && item.status === 'ready' && item.file && !browser) {
      setSource(item);
      v.classList.remove('hidden');
    } else if (!item || browser || item.status !== 'ready') {
      clearSource();
    }
    msg.replaceChildren();
    msg.className = 'stage-msg hidden';
    if (!item || browser) return;
    const show = (...children) => {
      msg.className = 'stage-msg';
      if (item.thumbnail) {
        const back = h('div.backdrop');
        back.style.backgroundImage = `url("${String(item.thumbnail).replace(/"/g, '%22')}")`;
        msg.append(back);
      }
      msg.append(...children);
    };
    if (item.status === 'pending' || item.status === 'downloading' || item.status === 'sniffing') {
      const pct = item.progress || 0;
      const bar = h('div.bar' + (item.status === 'downloading' && pct ? '' : '.indeterminate'), null, h('i', { style: { width: pct + '%' } }));
      show(h('h3', null, item.title || 'Getting it ready'),
        h('p', null, item.status === 'sniffing' ? 'Looking for the video on that page…'
          : item.status === 'downloading' ? (item.live_dl ? 'Starting the stream…' : `Getting it ready for everyone · ${pct}%`)
            : 'Finding it…'), bar);
      msg.dataset.uid = item.uid;
    } else if (item.status === 'choice') {
      const mayPick = T.isManager() || isMine(item);
      show(h('h3', null, 'Which video on that page?'),
        h('p', null, mayPick ? 'The page had more than one. Some could be ads.' : `Waiting for ${(item.added_by && item.added_by.name) || 'a moderator'} to pick`),
        mayPick ? h('div.choices', null, (item.choices || []).map((c) =>
          h('button', { type: 'button', onclick: () => T.send({ t: 'pick_choice', uid: item.uid, url: c.url }) },
            c.name || c.url, ' ', h('small', null, c.mime || c.kind || '')))) : null);
    } else if (item.status === 'error') {
      show(T.icon('alert'), h('h3', null, 'This one can’t play'), h('p', null, item.error || 'The site refused.'),
        h('button.btn.primary', { type: 'button', onclick: () => T.send({ t: 'skip' }) }, T.icon('skip', 'fill'), 'Skip it'));
    } else if (blocked) {
      show(h('button.big-play', { type: 'button', 'aria-label': 'Join playback', onclick: togglePlay }, T.icon('play')),
        h('h3', null, 'Click to join in'), h('p', null, 'Your browser wants a click before it plays sound.'));
      msg.classList.add('clickable');
      msg.onclick = togglePlay;
    } else if (clock && !clock.playing && st.index === st.queue.length - 1 && expected() >= (item.duration || Infinity) - 1) {
      show(h('h3', null, 'That was the last one'), h('p', null, 'Search for something else to keep going.'),
        h('button.btn.primary', { type: 'button', onclick: () => openTab('search', true) }, T.icon('search'), 'Search'));
    }
  }
  function renderProgress(item) {
    const msg = $('#stageMsg');
    if (msg.dataset.uid === item.uid && current() && current().uid === item.uid) {
      const bar = msg.querySelector('.bar');
      const p = msg.querySelector('p');
      if (bar) { bar.classList.remove('indeterminate'); bar.firstChild.style.width = item.progress + '%'; }
      if (p) p.textContent = item.live_dl ? 'Starting the stream…' : `Getting it ready for everyone · ${item.progress}%`;
    }
    const row = document.querySelector(`.vrow[data-uid="${item.uid}"] .vstatus span`);
    if (row && item.status === 'downloading') row.textContent = `Preparing · ${item.progress}%`;
  }

  function personBit(person) {
    return person ? [T.avatar(person, 'small'), h('span.ellipsis', null, person.name)] : [];
  }
  function renderNow() {
    const item = current();
    $('#nowTitle').textContent = item ? item.title || 'Untitled' : st.queue.length ? 'Nothing playing' : 'Nothing queued yet';
    const meta = $('#nowMeta');
    meta.replaceChildren();
    if (item) {
      if (item.uploader) meta.append(h('span.ellipsis', null, item.uploader));
      if (item.added_by) meta.append(h('span', null, '·'), h('span', null, 'added by'), ...personBit(item.added_by));
    } else {
      meta.append(h('span', null, 'Search or paste a link to start'));
    }
    T.tellHoffle('now', { title: item ? item.title : null });
    $('#nowActions').replaceChildren(T.stack(st.members, 5));
  }

  function thumb(item) {
    const el = h('div.thumb');
    if (item.thumbnail) el.style.backgroundImage = `url("${String(item.thumbnail).replace(/"/g, '%22')}")`;
    if (item.duration) el.append(h('span.dur', null, T.fmt(item.duration)));
    return el;
  }
  function statusLine(item) {
    if (item.status === 'ready') return null;
    if (item.status === 'embed') return h('div.vstatus', null, T.icon('globe'), h('span', null, 'Plays from the site (not synced)'));
    if (item.status === 'error') return h('div.vstatus.error', null, T.icon('alert'), h('span.ellipsis', null, item.error || 'Can’t play this'));
    if (item.status === 'choice') return h('div.vstatus', null, T.icon('pointer'), h('span', null, 'Pick which video'));
    return h('div.vstatus', null, T.icon('loader', 'spin'),
      h('span', null, item.status === 'downloading' ? `Preparing · ${item.progress || 0}%` : item.status === 'sniffing' ? 'Looking on the page…' : 'Finding it…'));
  }
  function videoRow(item, index, kind) {
    const mayRemove = T.isManager() || isMine(item);
    const draggable = kind === 'next' && canControl();
    const row = h('div.vrow' + (kind === 'now' ? '.now' : kind === 'past' ? '.past' : ''), {
      dataset: { uid: item.uid }, draggable: draggable ? 'true' : null,
      ondragstart: (e) => { dragging = index; row.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; },
      ondragend: () => { dragging = -1; document.querySelectorAll('.vrow').forEach((r) => r.classList.remove('dragging', 'over')); },
      ondragover: (e) => { if (dragging >= 0 && kind === 'next') { e.preventDefault(); row.classList.add('over'); } },
      ondragleave: () => row.classList.remove('over'),
      ondrop: (e) => { e.preventDefault(); if (dragging >= 0 && dragging !== index) T.send({ t: 'reorder', from: dragging, to: index }); },
    },
    draggable ? h('span.grip', { title: 'Drag to reorder' }, T.icon('grip')) : null,
    thumb(item),
    h('div.vtext', null,
      h('div.vtitle', { title: item.title || '' }, item.title || 'Untitled'),
      h('div.vmeta', null, item.uploader ? h('span.ellipsis', null, item.uploader) : null,
        item.added_by ? [item.uploader ? h('span', null, '·') : null, ...personBit(item.added_by)] : null),
      statusLine(item)),
    h('div.vactions', null,
      kind !== 'now' && canControl() ? h('button.btn.ghost.small.icon', { type: 'button', title: 'Play now', 'aria-label': 'Play now',
        onclick: () => T.send({ t: 'jump', index }) }, T.icon('play', 'fill')) : null,
      mayRemove ? h('button.btn.ghost.small.icon', { type: 'button', title: 'Remove', 'aria-label': 'Remove',
        onclick: () => T.send({ t: 'remove', index }) }, T.icon('x')) : null));
    return row;
  }
  let showPast = false;
  function renderQueue() {
    const box = $('#tab-queue');
    const scroll = box.scrollTop;
    box.replaceChildren();
    const idx = st.index;
    const cur = current();
    if (cur) box.append(h('div.section-title', null, 'Now playing'), videoRow(cur, idx, 'now'));
    const next = st.queue.map((item, i) => [item, i]).filter(([, i]) => i > idx);
    box.append(h('div.section-title', null, h('span', null, 'Up next'),
      h('button.btn.ghost.small', { type: 'button', onclick: () => openTab('search', true) }, T.icon('plus'), 'Add')));
    if (!next.length) {
      box.append(h('div.empty', null, 'Nothing up next.', h('br'), 'Search for something, or paste a link.'));
    }
    next.forEach(([item, i]) => box.append(videoRow(item, i, 'next')));
    const past = st.queue.map((item, i) => [item, i]).filter(([, i]) => i < idx);
    if (past.length) {
      box.append(h('div.section-title', null, h('span', null, `Played (${past.length})`),
        h('button.btn.ghost.small', { type: 'button', onclick: () => { showPast = !showPast; renderQueue(); } }, showPast ? 'Hide' : 'Show')));
      if (showPast) past.reverse().forEach(([item, i]) => box.append(videoRow(item, i, 'past')));
    }
    box.scrollTop = scroll;
  }

  // ------------------------------------------------------------ search
  function runSearch(q) {
    q = q.trim();
    if (!q) return;
    searchState = { q, items: null, busy: true, error: '' };
    $('#searchInput').value = q;
    renderSearch();
    T.send({ t: 'search', q });
  }
  function renderSearch() {
    const box = $('#results');
    box.replaceChildren();
    if (searchState.busy) {
      for (let i = 0; i < 5; i++) {
        box.append(h('div.vrow', null, h('div.thumb.skeleton'), h('div.vtext', null,
          h('div.skeleton', { style: { height: '12px', width: '90%' } }), h('div.skeleton', { style: { height: '10px', width: '50%', marginTop: '6px' } }))));
      }
      return;
    }
    if (searchState.error) box.append(h('div.empty', null, searchState.error));
    if (!searchState.items) {
      box.append(h('div.empty', null, 'Search YouTube, or paste a link from YouTube, Twitch clips, TikTok, X, Reddit and most other sites.'));
      return;
    }
    if (!searchState.items.length && !searchState.error) box.append(h('div.empty', null, 'Nothing found.'));
    const nothingPlaying = !current();
    searchState.items.forEach((r) => {
      const add = (next) => {
        T.send({ t: 'add', q: r.url, next });
        T.toast(next ? 'Playing next' : nothingPlaying ? 'Starting it…' : 'Added to the queue');
        if (nothingPlaying || window.innerWidth > 820) openTab('queue');
      };
      box.append(h('div.vrow', null, thumb({ thumbnail: r.thumbnail, duration: r.duration }),
        h('div.vtext', null, h('div.vtitle', { title: r.title }, r.title),
          h('div.vmeta', null, h('span.ellipsis', null, [r.uploader, T.views(r.views), r.live ? 'LIVE' : ''].filter(Boolean).join(' · ')))),
        h('div.result-actions', null,
          h('button.btn.small' + (nothingPlaying ? '.primary' : ''), { type: 'button', onclick: () => add(false), title: nothingPlaying ? 'Play' : 'Add to the queue' },
            T.icon(nothingPlaying ? 'play' : 'plus', nothingPlaying ? 'fill' : ''), nothingPlaying ? 'Play' : 'Add'),
          nothingPlaying ? null : h('button.btn.small.ghost', { type: 'button', onclick: () => add(true), title: 'Play right after this one' }, T.icon('next'), 'Next'))));
    });
  }
  $('#searchForm').onsubmit = (e) => { e.preventDefault(); runSearch($('#searchInput').value); };
  $('#welcomeForm').onsubmit = (e) => {
    e.preventDefault();
    const q = $('#welcomeInput').value.trim();
    if (!q) return;
    $('#welcomeInput').value = '';
    openTab('search');
    runSearch(q);
  };
  renderSearch();

  // ------------------------------------------------------------ tabs
  function openTab(name, focus) {
    document.querySelectorAll('.tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
    ['queue', 'search', 'room'].forEach((t) => $('#tab-' + t).classList.toggle('hidden', t !== name));
    if (name === 'search' && focus) setTimeout(() => $('#searchInput').focus(), 0);
    if (window.innerWidth <= 820) $('.side').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  document.querySelectorAll('.tabs button').forEach((b) => { b.onclick = () => openTab(b.dataset.tab, true); });

  // ------------------------------------------------------------ room tab
  function roleBadge(role) {
    if (role === 'host') return h('span.badge', null, T.icon('crown'), 'Host');
    if (role === 'moderator') return h('span.badge', null, T.icon('shield'), 'Moderator');
    return null;
  }
  function select(key, options, value, disabled) {
    return h('select.input', { disabled, onchange: (e) => {
      const raw = e.target.value;
      T.send({ t: 'settings', [key]: /^[\d.]+$/.test(raw) ? Number(raw) : raw });
    } }, options.map(([val, label]) => h('option', { value: val, selected: String(val) === String(value) }, label)));
  }
  function toggle(key, value, disabled) {
    return h('label.switch', null, h('input', { type: 'checkbox', checked: value, disabled,
      onchange: (e) => T.send({ t: 'settings', [key]: e.target.checked }) }), h('span'));
  }
  function renderRoom() {
    const box = $('#tab-room');
    if (box.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
    const manager = T.isManager();
    const s = settings();
    box.replaceChildren(h('div.section-title', null, `In the room (${st.members.length})`));
    st.members.forEach((m) => {
      const me = T.me && m.id === T.me.id;
      box.append(h('div.person', null, T.avatar(m, 'large'), h('span.name.ellipsis', null, m.name + (me ? ' (you)' : '')), roleBadge(m.role),
        manager && !me && m.role !== 'host' ? h('button.btn.ghost.small', { type: 'button',
          title: m.role === 'moderator' ? 'They can no longer manage the room' : 'They can manage the queue and settings',
          onclick: () => T.send({ t: 'role', id: m.id, role: m.role === 'moderator' ? 'member' : 'moderator' }) },
        m.role === 'moderator' ? 'Remove mod' : 'Make mod') : null));
    });

    box.append(h('div.section-title', null, 'Playlists'));
    const lists = st.playlists || [];
    if (!lists.length) box.append(h('div.hint', null, 'Save the queue to watch it again another day.'));
    lists.forEach((p) => {
      const mine = T.me && p.owner_id === T.me.id;
      box.append(h('div.plrow', null, T.icon('bookmark'),
        h('div.name', null, h('div.ellipsis', null, p.name), h('small', null, `${p.count} videos · ${p.owner || 'someone'}`)),
        h('button.btn.small', { type: 'button', title: 'Add to the queue', onclick: () => T.send({ t: 'playlist_load', name: p.name }) }, T.icon('plus'), 'Queue'),
        manager || mine ? h('button.btn.ghost.small.icon', { type: 'button', title: 'Delete', 'aria-label': 'Delete playlist',
          onclick: () => { if (confirm(`Delete “${p.name}”?`)) T.send({ t: 'playlist_delete', name: p.name }); } }, T.icon('trash')) : null));
    });
    const nameInput = h('input.input', { maxlength: 50, placeholder: 'Save the queue as…', 'aria-label': 'Playlist name' });
    box.append(h('form.save-row', { onsubmit: (e) => {
      e.preventDefault();
      if (nameInput.value.trim()) { T.send({ t: 'playlist_save', name: nameInput.value.trim() }); nameInput.value = ''; nameInput.blur(); }
    } }, nameInput, h('button.btn.small', { type: 'submit', disabled: !st.queue.length }, 'Save')));

    box.append(h('div.section-title', null, 'Settings'));
    if (!manager) box.append(h('div.hint', null, T.icon('lock'), 'The host and moderators can change these.'));
    box.append(h('div.settings', null,
      h('div.setting', null, h('div', null, 'Who can play, pause and seek'),
        select('control_policy', [['everyone', 'Everyone'], ['managers', 'Host & mods']], s.control_policy, !manager)),
      h('div.setting', null, h('div', null, 'Skipping'),
        select('skip_policy', [['everyone', 'Anyone skips'], ['vote', 'Vote'], ['managers', 'Host & mods']], s.skip_policy, !manager)),
      s.skip_policy === 'vote' ? h('div.setting', null, h('div', null, 'Votes needed'),
        select('vote_threshold', [[0.25, '25%'], [0.5, '50%'], [0.67, '67%'], [1, 'Everyone']], s.vote_threshold, !manager)) : null,
      h('div.setting', null, h('div', null, 'Quality', h('small', null, 'For videos added from now on')),
        select('quality', [[360, '360p'], [480, '480p'], [720, '720p'], [1080, '1080p']], s.quality, !manager)),
      h('div.setting', null, h('div', null, 'Skip sponsor segments', h('small', null, 'SponsorBlock, on YouTube')), toggle('sponsorblock', s.sponsorblock, !manager)),
      st.browser_available ? h('div.setting', null, h('div', null, 'Block ads', h('small', null, 'In the shared browser')), toggle('adblock', s.adblock, !manager)) : null));

    if (st.browser_available && manager) {
      const url = h('input.input', { placeholder: 'A page to open (optional)', maxlength: 500, 'aria-label': 'Page address' });
      box.append(h('div.section-title', null, 'Shared browser'),
        h('div.hint', null, 'For sites the bot can’t download from: a real browser on the server that everyone watches.'),
        h('form.save-row', { onsubmit: (e) => { e.preventDefault(); T.send({ t: 'browser_start', url: url.value.trim() }); } },
          url, h('button.btn.small', { type: 'submit' }, T.icon('globe'), 'Open')));
    }
  }

  // ------------------------------------------------------------ shared browser
  function amController() { return st && st.browser && st.browser.active && T.me && st.browser.controller === T.me.id; }
  function renderBrowser() {
    const b = st.browser || {};
    const on = Boolean(b.active);
    $('#bwrap').classList.toggle('hidden', !on);
    $('#bbar').classList.toggle('hidden', !on);
    v.classList.toggle('hidden', on);
    if (!on) {
      stopStream();
      $('#bcursor').style.display = 'none';
      return;
    }
    const controller = st.members.find((m) => m.id === b.controller);
    const mine = amController();
    const manager = T.isManager();
    const url = h('input.input', { placeholder: 'Go to an address', maxlength: 500, value: '' });
    const bar = $('#bbar');
    if (bar.contains(document.activeElement)) return;
    bar.replaceChildren(
      h('span.who', null, T.icon('globe'), b.status === 'starting' ? 'Starting the browser…'
        : controller ? [T.avatar(controller, 'small'), `${controller.name} has the mouse`] : 'Nobody has the mouse'),
      manager && !mine ? h('button.btn.small', { type: 'button', onclick: () => T.send({ t: 'browser_control' }) }, T.icon('pointer'), 'Take the mouse') : null,
      mine ? h('form', { style: { display: 'flex', flex: '1', gap: '6px' }, onsubmit: (e) => { e.preventDefault(); if (url.value.trim()) T.send({ t: 'browser_nav', url: url.value.trim() }); } }, url) : null,
      manager ? h('button.btn.small', { type: 'button', onclick: () => { T.send({ t: 'browser_media' }); } }, T.icon('film'), 'Videos on this page', b.media_count ? ` (${b.media_count})` : '') : null,
      manager ? h('button.btn.small.danger', { type: 'button', onclick: () => T.send({ t: 'browser_stop' }) }, T.icon('x'), 'Close') : null,
      h('div.media-list#mediaList'));
    $('#bcanvas').classList.toggle('controlling', mine);
    if (!bPlayer && b.status === 'running') startStream();
  }
  function showMedia(d) {
    const list = $('#mediaList');
    if (!list) return;
    list.replaceChildren();
    if (!d.items || !d.items.length) {
      list.append(h('div.hint', null, 'No videos found yet. Start one playing on the page, then look again.'));
      return;
    }
    d.items.forEach((m) => list.append(h('div.media-row', null, h('span.k', null, m.kind), h('span.ellipsis', { style: { flex: 1 }, title: m.url }, m.name, ' ', h('span.muted', null, m.mime)),
      h('button.btn.small.primary', { type: 'button', onclick: () => { T.send({ t: 'browser_pick', url: m.url }); list.replaceChildren(); } }, 'Add'))));
  }
  function showCursor(d) {
    if (!st || !st.browser || !st.browser.active) return;
    const canvas = $('#bcanvas'), cur = $('#bcursor');
    const r = canvas.getBoundingClientRect(), w = $('#bwrap').getBoundingClientRect();
    cur.style.display = 'block';
    cur.style.left = (r.left - w.left + d.x * r.width) + 'px';
    cur.style.top = (r.top - w.top + d.y * r.height) + 'px';
    if (d.down) { cur.classList.remove('click'); void cur.offsetWidth; cur.classList.add('click'); }
  }
  function startStream() {
    loadScript('/watch/static/jsmpeg.min.js').then(() => {
      if (bPlayer || !window.JSMpeg || !st.browser || !st.browser.active) return;
      const key = mediaBase.split('/').filter(Boolean)[3] || '';
      const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      bPlayer = new window.JSMpeg.Player(`${proto}${location.host}/watch/stream?room=${encodeURIComponent(T.room)}&key=${encodeURIComponent(key)}`, {
        canvas: $('#bcanvas'), audio: true, pauseWhenHidden: false,
        videoBufferSize: 768 * 1024, audioBufferSize: 256 * 1024,
      });
    }).catch(() => T.toast('The browser stream did not load', { kind: 'error' }));
  }
  function stopStream() {
    if (bPlayer) { try { bPlayer.destroy(); } catch (_e) { /* gone */ } bPlayer = null; }
  }
  const bc = $('#bcanvas');
  let lastMove = 0;
  const bxy = (e) => { const r = bc.getBoundingClientRect(); return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height }; };
  const bbtn = (e) => (e.button === 2 ? 3 : e.button === 1 ? 2 : 1);
  bc.addEventListener('mousemove', (e) => {
    if (!amController()) return;
    const now = Date.now(); if (now - lastMove < 33) return; lastMove = now;
    const p = bxy(e); T.send({ t: 'bmouse', a: 'move', x: p.x, y: p.y });
  });
  bc.addEventListener('mousedown', (e) => { if (!amController()) return; e.preventDefault(); bc.focus(); const p = bxy(e); T.send({ t: 'bmouse', a: 'down', x: p.x, y: p.y, b: bbtn(e) }); });
  bc.addEventListener('mouseup', (e) => { if (!amController()) return; const p = bxy(e); T.send({ t: 'bmouse', a: 'up', x: p.x, y: p.y, b: bbtn(e) }); });
  bc.addEventListener('wheel', (e) => { if (!amController()) return; e.preventDefault(); T.send({ t: 'bmouse', a: 'wheel', dy: e.deltaY }); }, { passive: false });
  bc.addEventListener('contextmenu', (e) => e.preventDefault());
  bc.addEventListener('keydown', (e) => { if (!amController()) return; e.preventDefault(); T.send({ t: 'bkey', a: 'down', key: e.key }); });
  bc.addEventListener('keyup', (e) => { if (!amController()) return; e.preventDefault(); T.send({ t: 'bkey', a: 'up', key: e.key }); });
  bc.addEventListener('touchstart', (e) => { if (!amController()) return; e.preventDefault(); const p = bxy(e.touches[0]); T.send({ t: 'bmouse', a: 'down', x: p.x, y: p.y, b: 1 }); }, { passive: false });
  bc.addEventListener('touchend', (e) => { if (!amController()) return; e.preventDefault(); const p = bxy(e.changedTouches[0]); T.send({ t: 'bmouse', a: 'up', x: p.x, y: p.y, b: 1 }); }, { passive: false });

  paintVolume();
})();
