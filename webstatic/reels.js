/* Reels Together: a synced, swipeable Shorts feed for a Hoffle voice room. */
(function () {
  'use strict';
  const T = window.Together, h = T.h, $ = T.$;
  const frame = $('#frame'), track = $('#track');
  let st = null, clock = null, mediaBase = '', reactions = [], topics = [];
  let pending = 0, pendingTimer = null, swipeLock = 0;
  let soundBlocked = false, lastHard = 0, wantSound = true;
  const chosen = new Set();
  let sheet = null;   // the open sheet: 'tune' | 'add' | null

  // three stacked videos: previous, current, next
  const slots = [0, 1, 2].map(() => {
    const el = h('div.slot.off');
    const video = h('video', { playsinline: true, preload: 'auto', 'webkit-playsinline': true });
    video.playsInline = true;
    video.muted = true;
    el.append(video);
    track.append(el);
    const slot = { el, video, uid: null, pos: 'off', src: '', embed: null };
    video.addEventListener('ended', () => {
      if (slot.pos !== 'cur' || !st) return;
      T.send({ t: 'ended', index: st.index });
      video.currentTime = 0;
      play(video);
    });
    return slot;
  });

  $('#likeBtn').append(T.icon('heart'));
  $('#lessBtn').append(T.icon('less'));
  $('#reactBtn').append(T.icon('smile'));
  $('#addBtn').append(T.icon('plus'));
  $('#tuneBtn').append(T.icon('sliders'));
  $('#upBtn').append(T.icon('up'));
  $('#downBtn').append(T.icon('down'));
  $('#seedIcon').append(T.icon('sparkles'));
  $('#tapBtn').append(T.icon('play'));

  // ------------------------------------------------------------ state helpers
  const settings = () => (st && st.settings) || {};
  const itemAt = (i) => {
    if (!st) return null;
    const k = i - st.offset;
    return k >= 0 && k < st.queue.length ? st.queue[k] : null;
  };
  const playable = (item) => item && ((item.status === 'ready' && item.file) || item.status === 'embed');
  const viewIndex = () => (st ? st.index + pending : -1);
  const current = () => itemAt(viewIndex());
  const curSlot = () => slots.find((s) => s.pos === 'cur');
  const iLiked = (item) => item && T.me && (item.liked_ids || []).includes(T.me.id);

  function expected(item) {
    if (!clock) return 0;
    let t = clock.playing ? clock.position + (T.serverNow() - clock.now) / 1000 : clock.position;
    if (pending) t = 0;
    const d = item && item.duration;
    return d ? t % d : t;
  }
  function play(video) {
    const p = video.play();
    if (!p || !p.catch) return;
    p.then(() => { if (video === (curSlot() || {}).video) $('#tapCover').classList.add('hidden'); })
      .catch((err) => {
        if (!err || err.name !== 'NotAllowedError' || video !== (curSlot() || {}).video) return;
        if (!video.muted) {
          // No sound without a tap first: play muted and offer sound.
          video.muted = true;
          soundBlocked = true;
          paintSound();
          play(video);
        } else {
          $('#tapCover').classList.remove('hidden');
        }
      });
  }
  function unlockSound() {
    soundBlocked = false;
    wantSound = true;
    const slot = curSlot();
    if (slot) { slot.video.muted = false; play(slot.video); }
    $('#tapCover').classList.add('hidden');
    paintSound();
  }
  $('#tapCover').onclick = unlockSound;

  // ------------------------------------------------------------ messages
  T.boot({
    onMessage(d) {
      if (d.t === 'welcome') {
        mediaBase = d.media;
        reactions = d.reactions || [];
        topics = d.topics || [];
        $('#reactPop').replaceChildren(T.reactionPicker(reactions, (emoji) => {
          T.send({ t: 'react', emoji });
          $('#reactPop').classList.add('hidden');
        }));
        renderSeedChips();
      } else if (d.t === 'state') {
        st = d;
        clock = { index: d.index, playing: d.playing, position: d.position, now: d.now };
        pending = 0;
        clearTimeout(pendingTimer);
        render();
      } else if (d.t === 'sync') {
        if (!st) return;
        clock = { index: d.index, playing: d.playing, position: d.position, now: d.now };
        if (d.index !== st.index) { st.index = d.index; pending = 0; render(); }
        reconcile(true);
      } else if (d.t === 'heart') {
        const item = st && st.queue.find((it) => it.uid === d.uid);
        if (item) {
          item.likes = d.likes;
          item.liked_by = d.liked_by;
          const ids = new Set(item.liked_ids || []);
          if (d.liked) ids.add(d.by.id); else ids.delete(d.by.id);
          item.liked_ids = [...ids];
          renderInfo();
        }
        if (d.liked && d.by.id !== (T.me && T.me.id)) T.float($('#reactions'), '❤️', d.by);
      } else if (d.t === 'react') {
        T.float($('#reactions'), d.emoji, d.by);
      } else if (d.t === 'swiped') {
        if (d.by && T.me && d.by.id !== T.me.id) T.toast(`${d.by.name} swiped ${d.dir > 0 ? 'on' : 'back'}`, { by: d.by, ms: 1400 });
      } else if (d.t === 'notice' && d.kind === 'error') {
        // a refused swipe: put the feed back where the room is
        if (pending) { pending = 0; render(); }
      }
    },
  });

  // ------------------------------------------------------------ layout
  function srcFor(item) {
    return mediaBase + item.file.split('/').map(encodeURIComponent).join('/');
  }
  function assign(slot, item) {
    slot.uid = item ? item.uid : null;
    slot.el.classList.add('instant');
    if (slot.embed) { slot.embed.remove(); slot.embed = null; }
    slot.video.classList.remove('hidden');
    load(slot, item);
  }
  function load(slot, item) {
    slot.el.style.backgroundImage = item && item.thumbnail ? `url("${String(item.thumbnail).replace(/"/g, '%22')}")` : '';
    const ratio = item && item.width && item.height ? item.width / item.height : 0.5625;
    slot.el.classList.toggle('fit', ratio > 0.62 || ratio < 0.5);
    const src = item && item.status === 'ready' && item.file ? srcFor(item) : '';
    if (src !== slot.src) {
      slot.src = src;
      if (src) { slot.video.src = src; slot.video.load(); } else { slot.video.removeAttribute('src'); slot.video.load(); }
    }
  }
  function place(slot, pos) {
    slot.pos = pos;
    slot.el.classList.toggle('off', pos === 'off');
    slot.el.style.transform = pos === 'prev' ? 'translateY(-100%)' : pos === 'next' ? 'translateY(100%)' : 'translateY(0)';
  }
  function layout() {
    const idx = viewIndex();
    const want = [['cur', itemAt(idx)], ['next', itemAt(idx + 1)], ['prev', itemAt(idx - 1)]];
    const taken = new Set();
    const plan = [];
    for (const [pos, item] of want) {
      if (!item) continue;
      const slot = slots.find((s) => s.uid === item.uid && !taken.has(s));
      if (slot) { taken.add(slot); plan.push([slot, pos, item, false]); } else plan.push([null, pos, item, true]);
    }
    for (const entry of plan) {
      if (entry[0]) continue;
      const slot = slots.find((s) => !taken.has(s));
      taken.add(slot);
      entry[0] = slot;
      assign(slot, entry[2]);
    }
    slots.forEach((s) => { if (!taken.has(s)) { place(s, 'off'); s.video.pause(); } });
    for (const [slot, pos, item, fresh] of plan) {
      if (!fresh) load(slot, item);
      place(slot, pos);
      if (fresh) { void slot.el.offsetWidth; slot.el.classList.remove('instant'); }
      if (pos !== 'cur') {
        slot.video.muted = true;
        if (!slot.video.paused) slot.video.pause();
        if (slot.video.readyState >= 1 && slot.video.currentTime > 0.1 && pos === 'next') slot.video.currentTime = 0;
      }
    }
    const cur = curSlot();
    const item = current();
    if (cur && item && item.status === 'embed' && !cur.embed) {
      cur.video.classList.add('hidden');
      cur.embed = h('iframe', {
        src: item.embed_kind === 'youtube'
          ? `https://www.youtube-nocookie.com/embed/${encodeURIComponent(item.embed)}?autoplay=1&playsinline=1&loop=1&playlist=${encodeURIComponent(item.embed)}`
          : item.embed,
        allow: 'autoplay; fullscreen; encrypted-media', referrerpolicy: 'no-referrer',
        sandbox: 'allow-scripts allow-same-origin allow-presentation',
      });
      cur.el.append(cur.embed);
    }
    if (cur) cur.video.muted = !wantSound || soundBlocked;
    reconcile(true);
  }

  function reconcile(force) {
    const slot = curSlot();
    const item = current();
    if (!slot || !item || item.status !== 'ready' || !slot.src) return;
    const v = slot.video;
    const shouldPlay = clock && clock.playing;
    if (shouldPlay && v.paused && !v.ended) play(v);
    if (!shouldPlay && !v.paused) v.pause();
    if (v.readyState < 1 || pending) return;
    const d = isFinite(v.duration) && v.duration ? v.duration : item.duration;
    const want = expected(Object.assign({}, item, { duration: d }));
    let drift = v.currentTime - want;
    if (d) { if (drift > d / 2) drift -= d; if (drift < -d / 2) drift += d; }
    if (Math.abs(drift) > (shouldPlay ? 1.2 : 0.3)) {
      if (force || Date.now() - lastHard > 1500) { v.currentTime = Math.max(0, want); lastHard = Date.now(); }
      v.playbackRate = 1;
    } else if (shouldPlay && Math.abs(drift) > 0.2) {
      v.playbackRate = drift > 0 ? 0.94 : 1.06;
    } else {
      v.playbackRate = 1;
    }
  }
  setInterval(() => reconcile(false), 500);
  (function tick() {
    const slot = curSlot();
    const v = slot && slot.video;
    $('#progress').style.width = v && v.duration ? (v.currentTime / v.duration) * 100 + '%' : '0';
    requestAnimationFrame(tick);
  })();

  // ------------------------------------------------------------ rendering
  function render() {
    layout();
    renderInfo();
    renderCovers();
    renderTop();
    if (sheet === 'tune') openTune();
  }
  function renderTop() {
    $('#people').replaceChildren(T.stack(st.members, 4));
    const auto = $('#autoBtn');
    const on = Boolean(settings().autoscroll);
    auto.className = 'pill' + (on ? ' on' : '');
    auto.replaceChildren(T.icon('chevrons'), on ? 'Auto' : 'Manual');
    auto.title = on ? 'Moves on when a reel ends' : 'Stays on a reel until someone swipes';
    auto.disabled = !T.isManager();
    paintSound();
  }
  function paintSound() {
    let pill = $('#soundPill');
    if (soundBlocked) {
      if (!pill) {
        pill = h('button.pill#soundPill', { type: 'button', onclick: (e) => { e.stopPropagation(); unlockSound(); } }, T.icon('mute'), 'Tap for sound');
        $('.topbar').insertBefore(pill, $('#autoBtn'));
      }
    } else if (pill) pill.remove();
  }
  $('#autoBtn').onclick = () => { if (T.isManager()) T.send({ t: 'settings', autoscroll: !settings().autoscroll }); };

  function renderInfo() {
    const item = current();
    const creator = $('#creator');
    creator.replaceChildren();
    if (item && item.uploader) {
      creator.append(h('span.ellipsis', null, '@' + item.uploader));
      if (item.creator) creator.append(h('button', { type: 'button', title: 'Queue more shorts from them', onclick: () => T.send({ t: 'creator' }) }, 'More'));
    }
    $('#title').textContent = item ? (item.title || '').replace(/\s#\S+/g, '').trim() || item.title || '' : '';
    const why = $('#why');
    why.replaceChildren();
    if (item && item.why) {
      const parts = [item.why.label, item.why.for ? `for ${item.why.for}` : ''].filter(Boolean);
      why.append(T.icon(item.why.kind === 'added' ? 'plus' : 'sparkles'), h('span.ellipsis', null, parts.join(' · ')));
    }
    const likers = $('#likers');
    likers.replaceChildren();
    if (item && item.likes) {
      const names = (item.liked_by || []).filter(Boolean);
      const rest = item.likes - names.length;
      likers.append(T.icon('heart'), h('span', null, names.join(', ') + (rest > 0 ? ` and ${rest} more` : '')));
    }
    $('#likeBtn').classList.toggle('liked', iLiked(item));
    $('#likeCount').textContent = item && item.likes ? String(item.likes) : 'Like';
    const back = itemAt(viewIndex()) || itemAt(viewIndex() + 1);
    $('#backdrop').style.backgroundImage = back && back.thumbnail ? `url("${String(back.thumbnail).replace(/"/g, '%22')}")` : '';
    T.tellHoffle('now', { title: item ? item.title : null });
  }
  function renderCovers() {
    const feed = st.feed || {};
    const item = current();
    $('#seed').classList.toggle('hidden', !feed.need_seed);
    const waiting = !feed.need_seed && !playable(item);
    $('#loading').classList.toggle('hidden', !waiting);
    if (waiting) {
      $('#loadingText').textContent = item && item.status === 'downloading' ? 'Getting the next reel ready…'
        : st.total ? 'Finding more reels…' : 'Finding reels for you…';
    }
  }

  // ------------------------------------------------------------ seed screen
  function renderSeedChips() {
    const box = $('#seedChips');
    box.replaceChildren(...topics.map((topic) => h('button.chip' + (chosen.has(topic.tag) ? '.on' : ''), {
      type: 'button', 'aria-pressed': chosen.has(topic.tag) ? 'true' : 'false',
      onclick: () => { if (chosen.has(topic.tag)) chosen.delete(topic.tag); else chosen.add(topic.tag); renderSeedChips(); },
    }, topic.emoji + ' ' + topic.label)));
    $('#seedGo').disabled = !chosen.size && !$('#seedText').value.trim();
  }
  $('#seedText').addEventListener('input', renderSeedChips);
  function startFeed() {
    const text = $('#seedText').value.trim();
    if (!chosen.size && !text) return;
    T.send({ t: 'seed', topics: [...chosen], text });
    unlockSound();
    $('#seed').classList.add('hidden');
    $('#loading').classList.remove('hidden');
  }
  $('#seedGo').onclick = startFeed;
  $('#seedForm').onsubmit = (e) => { e.preventDefault(); startFeed(); };

  // ------------------------------------------------------------ actions
  function swipe(dir) {
    if (!st || Date.now() - swipeLock < 450) return;
    if (!T.can('swipe_policy', settings())) { T.toast('The host is the one swiping here', { kind: 'error' }); return; }
    swipeLock = Date.now();
    const target = itemAt(viewIndex() + dir);
    if (dir < 0 && !target) { T.toast('That was the first reel'); return; }
    _conclusion();
    if (playable(target)) {
      pending += dir;
      clearTimeout(pendingTimer);
      pendingTimer = setTimeout(() => { if (pending) { pending = 0; render(); } }, 2500);
      layout();
      renderInfo();
      renderCovers();
    }
    T.send({ t: 'swipe', dir });
  }
  function _conclusion() { $('#reactPop').classList.add('hidden'); }
  function like(at) {
    const item = current();
    if (!item) return;
    if (at) burst(at.x, at.y);
    if (at && iLiked(item)) return;      // double tap only ever likes
    T.send({ t: 'like' });
  }
  function burst(x, y) {
    const r = frame.getBoundingClientRect();
    const el = h('div.bigheart', null, T.icon('heart'));
    el.style.left = (x - r.left) + 'px';
    el.style.top = (y - r.top) + 'px';
    $('#hearts').append(el);
    setTimeout(() => el.remove(), 950);
  }
  function togglePlay() {
    if (soundBlocked) { unlockSound(); return; }
    const slot = curSlot();
    if (!slot || !clock || !current()) return;
    if (!T.can('swipe_policy', settings())) return;
    const pos = slot.video.currentTime;
    if (clock.playing) { T.send({ t: 'pause', pos }); clock = Object.assign({}, clock, { playing: false, position: pos, now: T.serverNow() }); slot.video.pause(); }
    else { T.send({ t: 'play', pos }); clock = Object.assign({}, clock, { playing: true, position: pos, now: T.serverNow() }); play(slot.video); }
  }
  $('#likeBtn').onclick = () => like(null);
  $('#lessBtn').onclick = () => { if (current()) T.send({ t: 'less' }); };
  $('#upBtn').onclick = () => swipe(-1);
  $('#downBtn').onclick = () => swipe(1);
  $('#reactBtn').onclick = (e) => {
    e.stopPropagation();
    const pop = $('#reactPop');
    pop.classList.toggle('hidden');
    const r = $('#reactBtn').getBoundingClientRect(), app = $('#rapp').getBoundingClientRect();
    pop.style.top = Math.max(8, r.top - app.top - 4) + 'px';
    pop.style.right = Math.max(8, app.right - r.left + 8) + 'px';
  };
  document.addEventListener('click', (e) => { if (!e.target.closest('#reactPop, #reactBtn')) $('#reactPop').classList.add('hidden'); });

  // gestures: drag up/down to swipe, tap to pause, double tap to like
  let drag = null, lastTap = 0, tapTimer = null;
  frame.addEventListener('pointerdown', (e) => {
    if (e.target.closest('button, input, .cover, .info button')) return;
    drag = { y: e.clientY, x: e.clientX, t: Date.now(), dy: 0, moved: false, id: e.pointerId };
  });
  frame.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    drag.dy = e.clientY - drag.y;
    if (!drag.moved && Math.abs(drag.dy) > 10) { drag.moved = true; track.classList.add('dragging'); frame.setPointerCapture(e.pointerId); }
    if (drag.moved) {
      const edge = (drag.dy > 0 && !itemAt(viewIndex() - 1)) || (drag.dy < 0 && !itemAt(viewIndex() + 1));
      track.style.transform = `translateY(${edge ? drag.dy * 0.25 : drag.dy}px)`;
    }
  });
  function endDrag(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    track.classList.remove('dragging');
    track.style.transition = 'transform .3s cubic-bezier(.2,.8,.2,1)';
    track.style.transform = '';
    setTimeout(() => { track.style.transition = ''; }, 320);
    if (d.moved) {
      const speed = Math.abs(d.dy) / Math.max(1, Date.now() - d.t);
      if (Math.abs(d.dy) > frame.clientHeight * 0.18 || (Math.abs(d.dy) > 40 && speed > 0.5)) swipe(d.dy < 0 ? 1 : -1);
      return;
    }
    if (e.type === 'pointercancel') return;
    const now = Date.now();
    if (now - lastTap < 300) {
      clearTimeout(tapTimer);
      lastTap = 0;
      like({ x: e.clientX, y: e.clientY });
      return;
    }
    lastTap = now;
    tapTimer = setTimeout(togglePlay, 300);
  }
  frame.addEventListener('pointerup', endDrag);
  frame.addEventListener('pointercancel', endDrag);
  let wheelSum = 0, wheelAt = 0;
  frame.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (Date.now() - wheelAt > 250) wheelSum = 0;
    wheelAt = Date.now();
    wheelSum += e.deltaY;
    if (Math.abs(wheelSum) > 60) { swipe(wheelSum > 0 ? 1 : -1); wheelSum = 0; }
  }, { passive: false });
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select')) return;
    if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); swipe(1); }
    else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); swipe(-1); }
    else if (e.key === ' ') { e.preventDefault(); togglePlay(); }
    else if (e.key === 'l') like(null);
    else if (e.key === 'm') { wantSound = !wantSound; const s = curSlot(); if (s) s.video.muted = !wantSound; }
  });

  // ------------------------------------------------------------ sheets
  function closeSheet() {
    sheet = null;
    document.querySelectorAll('.sheet, .sheet-back').forEach((el) => el.remove());
  }
  function showSheet(name, ...children) {
    const keepFocus = document.activeElement && document.activeElement.closest('.sheet') ? document.activeElement.name : null;
    document.querySelectorAll('.sheet, .sheet-back').forEach((el) => el.remove());
    sheet = name;
    const back = h('div.sheet-back', { onclick: closeSheet });
    const box = h('div.sheet', { role: 'dialog', 'aria-modal': 'true' }, children);
    document.body.append(back, box);
    if (keepFocus) { const el = box.querySelector(`[name="${keepFocus}"]`); if (el) el.focus(); }
    return box;
  }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && sheet) closeSheet(); });

  function openTune() {
    if (sheet === 'tune' && document.activeElement && document.activeElement.closest('.sheet') && document.activeElement.tagName === 'INPUT') return;
    const feed = st.feed || {};
    const manager = T.isManager();
    const s = settings();
    const add = h('input.input', { name: 'tuneAdd', maxlength: 120, placeholder: 'Add topics: chess, skate, baking…' });
    showSheet('tune',
      h('h3', null, T.icon('sliders'), 'The feed', h('span', { style: { flex: 1 } }),
        h('button.btn.ghost.small.icon', { type: 'button', 'aria-label': 'Close', onclick: closeSheet }, T.icon('x'))),
      h('h4', null, 'This room is into'),
      feed.interests && feed.interests.length
        ? h('div.chips', null, feed.interests.map((tag) => h('span.chip', null, '#' + tag,
          h('button.x', { type: 'button', title: `Less #${tag} for you`, 'aria-label': `Remove ${tag}`, style: { border: 0, background: 'none', color: 'inherit', padding: 0 },
            onclick: () => T.send({ t: 'tune', remove: tag }) }, T.icon('x')))))
        : h('p.muted', null, 'Nothing yet. Like a few reels, or add topics below.'),
      h('form', { style: { display: 'flex', gap: '8px', marginTop: '10px' }, onsubmit: (e) => {
        e.preventDefault();
        if (add.value.trim()) { T.send({ t: 'tune', add: add.value.trim() }); add.value = ''; T.toast('Added to your taste'); }
      } }, add, h('button.btn', { type: 'submit' }, 'Add')),
      h('p.muted', { style: { fontSize: '12px', margin: '8px 0 0' } },
        'Everyone here has their own taste, learnt from likes, “Less”, full watches and quick skips. The feed takes turns choosing for each of you.'),
      h('h4', null, 'Room'),
      h('div.setting', null, h('div', null, 'Auto-scroll', h('small', null, 'Move on when a reel ends')),
        h('label.switch', null, h('input', { type: 'checkbox', checked: Boolean(s.autoscroll), disabled: !manager,
          onchange: (e) => T.send({ t: 'settings', autoscroll: e.target.checked }) }), h('span'))),
      h('div.setting', null, h('div', null, 'Who can swipe'),
        h('select.input', { style: { width: 'auto' }, disabled: !manager, onchange: (e) => T.send({ t: 'settings', swipe_policy: e.target.value }) },
          [['everyone', 'Everyone'], ['managers', 'Host & mods']].map(([val, label]) => h('option', { value: val, selected: s.swipe_policy === val }, label)))),
      manager ? null : h('p.muted', { style: { fontSize: '12px' } }, 'The host and moderators can change these.'),
      h('h4', null, 'You'),
      h('button.btn.danger', { type: 'button', onclick: () => {
        if (confirm('Forget everything Reels learnt about your taste?')) T.send({ t: 'tune', reset_me: true });
      } }, T.icon('refresh'), 'Start my taste over'));
  }
  function openAdd() {
    const input = h('input.input', { name: 'addLink', maxlength: 500, placeholder: 'A Shorts, Reels or TikTok link, or search words' });
    const box = showSheet('add',
      h('h3', null, T.icon('plus'), 'Add a reel', h('span', { style: { flex: 1 } }),
        h('button.btn.ghost.small.icon', { type: 'button', 'aria-label': 'Close', onclick: closeSheet }, T.icon('x'))),
      h('form', { style: { display: 'flex', gap: '8px' }, onsubmit: (e) => {
        e.preventDefault();
        const q = input.value.trim();
        if (!q) return;
        T.send({ t: 'add', q });
        T.toast('It plays next, and the feed learns from it');
        closeSheet();
      } }, input, h('button.btn.primary', { type: 'submit' }, 'Add')),
      h('p.muted', { style: { fontSize: '12px', margin: '10px 0 0' } }, 'Added reels play right after this one, in the order they were added.'));
    setTimeout(() => input.focus(), 50);
    return box;
  }
  $('#tuneBtn').onclick = openTune;
  $('#addBtn').onclick = openAdd;
})();
