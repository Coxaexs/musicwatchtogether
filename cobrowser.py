"""Shared co-browser for Watch Together rooms.

Runs a real Firefox on a private virtual display (Xtightvnc), captures the
screen + audio with ffmpeg and streams MPEG-TS chunks to viewers over the
existing websocket route — no extra ports, no WebRTC. One participant holds
control; their mouse/keyboard is injected with XTEST (python-xlib).

Per session:
    Xtightvnc :9N          virtual 1280x720 display (VNC port bound to localhost,
                           never actually used - we only want the X server)
    xfwm4                  window manager so Firefox behaves (focus, popups)
    pipewire null sink     wt9N - Firefox plays audio into it via PULSE_SINK
    firefox (snap)         per-room profile in ~/snap/firefox/common/wt-rooms/
    ffmpeg                 x11grab + pulse monitor -> mpeg1/mp2 MPEG-TS on stdout

Sessions stop themselves when nobody has watched for IDLE_STOP seconds.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import time

HAS_XVFB = bool(shutil.which('Xvfb'))

logger = logging.getLogger('MusicBot.CoBrowser')

WIDTH, HEIGHT = 1280, 720
FPS = 25   # mpeg1 only allows standard rates (24/25/29.97/...)
MAX_SESSIONS = 2
IDLE_STOP = 180          # seconds with zero viewers before auto-stop
MAX_LIFETIME = 4 * 3600
DISPLAY_BASE = 91        # :91, :92, ...
PROFILE_ROOT = os.path.expanduser('~/snap/firefox/common/wt-rooms')
ADGUARD_XPI = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'webstatic', 'adguard.xpi')
ADGUARD_ID = 'adguardadblocker@adguard.com'

FIREFOX_PREFS = """\
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.page", 0);
user_pref("full-screen-api.warning.timeout", 0);
user_pref("media.videocontrols.picture-in-picture.video-toggle.enabled", false);
user_pref("extensions.autoDisableScopes", 0);
user_pref("extensions.enabledScopes", 15);
"""

# browser KeyboardEvent.key -> X keysym
SPECIAL_KEYS = {
    'Enter': 0xff0d, 'Backspace': 0xff08, 'Tab': 0xff09, 'Escape': 0xff1b,
    'ArrowLeft': 0xff51, 'ArrowUp': 0xff52, 'ArrowRight': 0xff53,
    'ArrowDown': 0xff54, 'Home': 0xff50, 'End': 0xff57, 'PageUp': 0xff55,
    'PageDown': 0xff56, 'Delete': 0xffff, 'Insert': 0xff63,
    'Control': 0xffe3, 'Alt': 0xffe9, 'Meta': 0xffe7,
    'F1': 0xffbe, 'F2': 0xffbf, 'F3': 0xffc0, 'F4': 0xffc1, 'F5': 0xffc2,
    'F6': 0xffc3, 'F7': 0xffc4, 'F8': 0xffc5, 'F9': 0xffc6, 'F10': 0xffc7,
    'F11': 0xffc8, 'F12': 0xffc9,
}


def _char_keysym(ch):
    cp = ord(ch)
    if cp < 0x100:
        return cp
    return 0x01000000 | cp   # unicode keysym


async def _run(*cmd, **kw):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kw)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors='replace'), err.decode(errors='replace')


class CoBrowserSession:
    def __init__(self, room_id):
        self.room_id = room_id
        self.display = None            # e.g. ':91'
        self.sink = None               # pipewire null-sink name
        self.procs = {}                # name -> Process
        self.viewers = {}              # ws -> asyncio.Queue
        self.controller = None         # participant name holding control
        self.started_by = None
        self.status = 'starting'
        self.started_at = time.time()
        self.last_viewer_at = time.time()
        self.on_change = None          # async callback set by watchtogether
        self._xd = None                # python-xlib Display
        self._reader_task = None
        self._watchdog_task = None
        self._audio_ok = False

    # ------------------------------------------------------------ lifecycle

    async def start(self, url, adblock=True):
        num = self._pick_display()
        self.display = f':{num}'
        self.sink = f'wt{num}'
        profile = os.path.join(PROFILE_ROOT, self.room_id)
        os.makedirs(profile, exist_ok=True)
        with open(os.path.join(profile, 'user.js'), 'w') as f:
            f.write(FIREFOX_PREFS)
        for lock in ('lock', '.parentlock'):
            try:
                os.remove(os.path.join(profile, lock))
            except OSError:
                pass
        # adblock: sideload AdGuard into the profile (or drop it if disabled)
        ext_dir = os.path.join(profile, 'extensions')
        ext_path = os.path.join(ext_dir, f'{ADGUARD_ID}.xpi')
        if adblock and os.path.isfile(ADGUARD_XPI):
            os.makedirs(ext_dir, exist_ok=True)
            if not os.path.isfile(ext_path):
                shutil.copyfile(ADGUARD_XPI, ext_path)
        elif not adblock and os.path.isfile(ext_path):
            os.remove(ext_path)

        # 1. virtual display (Xvfb if installed; Xtightvnc otherwise)
        if HAS_XVFB:
            xcmd = ['Xvfb', self.display, '-screen', '0',
                    f'{WIDTH}x{HEIGHT}x24', '-nolisten', 'tcp']
        else:
            xcmd = ['Xtightvnc', self.display, '-geometry', f'{WIDTH}x{HEIGHT}',
                    '-depth', '24', '-rfbport', str(15900 + num), '-localhost',
                    '-nolisten', 'tcp']
        self.procs['x'] = await asyncio.create_subprocess_exec(
            *xcmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        for _ in range(50):
            if os.path.exists(f'/tmp/.X11-unix/X{num}'):
                break
            await asyncio.sleep(0.1)
        else:
            await self.stop()
            raise RuntimeError('virtual display did not come up')

        # 2. window manager
        self.procs['wm'] = await asyncio.create_subprocess_exec(
            'xfwm4', '--display', self.display, '--sm-client-disable',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, 'DISPLAY': self.display},
            start_new_session=True)

        # 3. audio null sink (best effort - stream is video-only without it)
        try:
            rc, _o, err = await _run(
                'pw-cli', 'create-node', 'adapter',
                '{ factory.name=support.null-audio-sink node.name=%s '
                'media.class=Audio/Sink object.linger=true '
                'audio.position=[FL FR] }' % self.sink)
            self._audio_ok = rc == 0
            if rc != 0:
                logger.warning(f"null sink failed, going video-only: {err.strip()}")
        except FileNotFoundError:
            self._audio_ok = False

        # 4. firefox
        env = {**os.environ, 'DISPLAY': self.display, 'MOZ_ENABLE_WAYLAND': '0'}
        if self._audio_ok:
            env['PULSE_SINK'] = self.sink
        self.procs['ff'] = await asyncio.create_subprocess_exec(
            'firefox', '--no-remote', '--new-instance', '--profile', profile,
            url or 'https://duckduckgo.com',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=env, start_new_session=True)

        # 5. capture / encode
        # Xtightvnc advertises MIT-SHM but returns black frames through it, so
        # ffmpeg's x11grab can't be used there - instead a tiny xwd loop feeds
        # ffmpeg raw screenshots (xwd grabs cost ~7ms each).
        enc_stdin = None
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
        if HAS_XVFB:
            cmd += ['-f', 'x11grab', '-framerate', str(FPS),
                    '-video_size', f'{WIDTH}x{HEIGHT}', '-i', self.display]
        else:
            r_fd, w_fd = os.pipe()
            self.procs['grab'] = await asyncio.create_subprocess_exec(
                'sh', '-c',
                f'while :; do xwd -root -silent -display {self.display}; '
                f'sleep 0.03; done',
                stdout=w_fd, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
            os.close(w_fd)
            enc_stdin = r_fd
            cmd += ['-use_wallclock_as_timestamps', '1',
                    '-f', 'image2pipe', '-vcodec', 'xwd', '-i', '-']
        if self._audio_ok:
            cmd += ['-f', 'pulse', '-i', f'{self.sink}.monitor']
        cmd += ['-c:v', 'mpeg1video', '-b:v', '2500k', '-maxrate', '3000k',
                '-bufsize', '1000k', '-bf', '0', '-g', str(FPS),
                '-r', str(FPS), '-fps_mode', 'cfr']
        if self._audio_ok:
            cmd += ['-c:a', 'mp2', '-b:a', '128k', '-ar', '44100', '-ac', '2']
        cmd += ['-f', 'mpegts', '-muxdelay', '0.05', 'pipe:1']
        self.procs['enc'] = await asyncio.create_subprocess_exec(
            *cmd, stdin=enc_stdin, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        if enc_stdin is not None:
            os.close(enc_stdin)

        # 6. input connection
        loop = asyncio.get_running_loop()
        self._xd = await loop.run_in_executor(None, self._open_xlib)

        self.status = 'running'
        self._reader_task = asyncio.create_task(self._reader())
        self._watchdog_task = asyncio.create_task(self._watchdog())
        logger.info(f"🌐 co-browser up for {self.room_id} on {self.display} "
                    f"(audio={'yes' if self._audio_ok else 'no'})")

    def _open_xlib(self):
        from Xlib import display as xdisplay
        return xdisplay.Display(self.display)

    def _pick_display(self):
        used = {s.display for s in sessions.values() if s.display}
        for n in range(DISPLAY_BASE, DISPLAY_BASE + 10):
            if f':{n}' not in used and not os.path.exists(f'/tmp/.X11-unix/X{n}'):
                return n
        raise RuntimeError('no free display')

    async def stop(self):
        if self.status == 'stopped':
            return
        self.status = 'stopped'
        for task in (self._reader_task, self._watchdog_task):
            if task:
                task.cancel()
        for name in ('enc', 'grab', 'ff', 'wm', 'x'):
            proc = self.procs.get(name)
            if proc and proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        await asyncio.sleep(1.0)
        for name, proc in self.procs.items():
            if proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self._xd:
            try:
                self._xd.close()
            except Exception:
                pass
        if self._audio_ok:
            await self._destroy_sink()
        for q in self.viewers.values():
            try:
                q.put_nowait(None)   # sentinel: stream over
            except asyncio.QueueFull:
                pass
        sessions.pop(self.room_id, None)
        logger.info(f"co-browser stopped for {self.room_id}")
        if self.on_change:
            try:
                await self.on_change()
            except Exception:
                pass

    async def _destroy_sink(self):
        try:
            rc, out, _e = await _run('pw-dump')
            for obj in json.loads(out):
                props = obj.get('info', {}).get('props', {})
                if props.get('node.name') == self.sink:
                    await _run('pw-cli', 'destroy', str(obj['id']))
        except Exception as e:
            logger.warning(f"sink cleanup failed: {e}")

    # ------------------------------------------------------------ streaming

    async def _reader(self):
        enc = self.procs['enc']
        try:
            while True:
                chunk = await enc.stdout.read(8192)
                if not chunk:
                    break
                for q in list(self.viewers.values()):
                    try:
                        q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        pass   # slow viewer just glitches, others unaffected
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"co-browser stream reader died: {e}")
        if self.status == 'running':
            await self.stop()

    async def _watchdog(self):
        try:
            while True:
                await asyncio.sleep(15)
                if self.viewers:
                    self.last_viewer_at = time.time()
                elif time.time() - self.last_viewer_at > IDLE_STOP:
                    logger.info(f"co-browser idle, stopping ({self.room_id})")
                    await self.stop()
                    return
                if time.time() - self.started_at > MAX_LIFETIME:
                    await self.stop()
                    return
        except asyncio.CancelledError:
            return

    def add_viewer(self, ws):
        q = asyncio.Queue(maxsize=256)
        self.viewers[ws] = q
        self.last_viewer_at = time.time()
        return q

    def remove_viewer(self, ws):
        self.viewers.pop(ws, None)

    # ------------------------------------------------------------ input

    def mouse(self, action, x=0, y=0, button=1, dy=0):
        if not self._xd or self.status != 'running':
            return
        from Xlib import X
        from Xlib.ext import xtest
        d = self._xd
        try:
            px = max(0, min(WIDTH - 1, int(x * WIDTH)))
            py = max(0, min(HEIGHT - 1, int(y * HEIGHT)))
            if action == 'move':
                xtest.fake_input(d, X.MotionNotify, x=px, y=py)
            elif action == 'down':
                xtest.fake_input(d, X.MotionNotify, x=px, y=py)
                xtest.fake_input(d, X.ButtonPress, button)
            elif action == 'up':
                xtest.fake_input(d, X.ButtonRelease, button)
            elif action == 'wheel':
                btn = 5 if dy > 0 else 4
                for _ in range(min(5, max(1, abs(int(dy)) // 80 + 1))):
                    xtest.fake_input(d, X.ButtonPress, btn)
                    xtest.fake_input(d, X.ButtonRelease, btn)
            d.flush()
        except Exception as e:
            logger.debug(f"mouse inject failed: {e}")

    def key(self, action, keyname, ctrl=False, alt=False, meta=False):
        if not self._xd or self.status != 'running':
            return
        from Xlib import X
        from Xlib.ext import xtest
        d = self._xd
        try:
            if keyname == 'Shift':
                return
            if keyname in ('Control', 'Alt', 'Meta'):
                kc = d.keysym_to_keycode(SPECIAL_KEYS[keyname])
                if kc:
                    xtest.fake_input(
                        d, X.KeyPress if action == 'down' else X.KeyRelease, kc)
                    d.flush()
                return
            if action != 'down':
                return   # chars/specials fire as press+release on keydown
            ks = SPECIAL_KEYS.get(keyname)
            if ks is None:
                if len(keyname) != 1:
                    return
                ks = _char_keysym(keyname)
            kc = d.keysym_to_keycode(ks)
            if not kc:
                return
            need_shift = (d.keycode_to_keysym(kc, 0) != ks
                          and d.keycode_to_keysym(kc, 1) == ks)
            shift_kc = d.keysym_to_keycode(0xffe1)
            if need_shift:
                xtest.fake_input(d, X.KeyPress, shift_kc)
            xtest.fake_input(d, X.KeyPress, kc)
            xtest.fake_input(d, X.KeyRelease, kc)
            if need_shift:
                xtest.fake_input(d, X.KeyRelease, shift_kc)
            d.flush()
        except Exception as e:
            logger.debug(f"key inject failed: {e}")

    def type_text(self, text):
        for ch in str(text)[:500]:
            self.key('down', 'Enter' if ch == '\n' else ch)

    async def navigate(self, url):
        """Focus the browser, then ctrl+l, type the url, enter."""
        if not self._xd or self.status != 'running':
            return
        loop = asyncio.get_running_loop()

        def _nav():
            self._focus_firefox()
            self.key('down', 'Control')
            self.key('down', 'l', ctrl=True)
            self.key('up', 'Control')
            time.sleep(0.15)
            self.type_text(url)
            time.sleep(0.1)
            self.key('down', 'Enter')
        await loop.run_in_executor(None, _nav)

    def _focus_firefox(self):
        from Xlib import X
        d = self._xd
        try:
            root = d.screen().root
            best = None
            for w in root.query_tree().children:
                try:
                    cls = w.get_wm_class()
                    if cls and any('firefox' in c.lower() for c in cls):
                        if w.get_attributes().map_state != X.IsViewable:
                            continue
                        g = w.get_geometry()
                        if best is None or g.width * g.height > best[1]:
                            best = (w, g.width * g.height)
                except Exception:
                    continue
            if best:
                best[0].set_input_focus(X.RevertToParent, X.CurrentTime)
                d.sync()
        except Exception as e:
            logger.debug(f"focus failed: {e}")

    def public_state(self):
        return {
            'active': True,
            'status': self.status,
            'controller': self.controller,
            'started_by': self.started_by,
            'viewers': len(self.viewers),
            'audio': self._audio_ok,
        }


sessions = {}   # room_id -> CoBrowserSession


def get(room_id):
    s = sessions.get(room_id)
    return s if s and s.status != 'stopped' else None


async def start(room_id, url, started_by=None, on_change=None, adblock=True):
    if get(room_id):
        return sessions[room_id]
    if len(sessions) >= MAX_SESSIONS:
        raise RuntimeError(
            f'Browser limit reached ({MAX_SESSIONS} rooms at once) — try again later')
    s = CoBrowserSession(room_id)
    s.started_by = started_by
    s.controller = started_by
    s.on_change = on_change
    sessions[room_id] = s
    try:
        await s.start(url, adblock=adblock)
    except Exception:
        await s.stop()
        raise
    return s


async def stop_all():
    for s in list(sessions.values()):
        await s.stop()
