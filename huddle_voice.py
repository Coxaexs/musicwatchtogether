"""Real WebRTC audio publishing for Huddle voice rooms.

The existing Huddle player remains the command/queue control plane. This module
watches that state, decodes each room's current source exactly once with FFmpeg,
and publishes the resulting audio as a genuine WebRTC bot participant.
"""

import asyncio
import gc
from fractions import Fraction
import json
import logging
import sys
import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
import numpy as np
from av import AudioFrame
import aiortc.codecs
from aiortc.codecs import OpusEncoder as DefaultOpusEncoder
from aiortc import (
    MediaStreamTrack,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp

import config
import dj
import huddle
import mixer
from loop_watchdog import LoopWatchdog
import stems


def _mix_keys(track: dict):
    """Cache keys for a Huddle track's analysis."""
    return [mixer.track_key(track.get("pageUrl")),
            mixer.track_key(track.get("query"))]


logger = logging.getLogger("MusicBot.HuddleVoice")
#: The running HuddleVoiceManager, so the DJ booth can see room players.
MANAGER = None
SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = 960
CHANNELS = 2
BYTES_PER_FRAME = SAMPLES_PER_FRAME * CHANNELS * 2
OPUS_BITRATE = 256_000
# Keep three seconds of already-decoded PCM ahead of the sender. This does not
# add three seconds of playback latency: FFmpeg fills it faster than realtime,
# while the first frame is still sent immediately. It does absorb CDN and
# scheduler stalls that otherwise arrive as tiny silent "dips".
BUFFERED_FRAMES = 150
MUSIC_FILTERS = {
    "bassboost": "bass=g=10:f=110:w=0.6",
    "nightcore": "aresample=48000,asetrate=48000*1.25",
    "slowed": "aresample=48000,asetrate=48000*0.85",
    "8d": "apulsator=hz=0.09",
    "karaoke": "pan=stereo|c0=c0-c1|c1=c1-c0",
}


class MusicOpusEncoder(DefaultOpusEncoder):
    """Use Discord-like music bandwidth instead of aiortc's 96 kbps default."""

    def __init__(self):
        super().__init__()
        self.codec.bit_rate = OPUS_BITRATE
        # aiortc defaults to Opus' speech-tuned VOIP mode. That aggressively
        # models voices and can add a faint watery/noisy texture to music.
        self.codec.options = {
            "application": "audio",
            "vbr": "on",
            "compression_level": "10",
        }


# aiortc resolves this module global when an RTP sender starts.
aiortc.codecs.OpusEncoder = MusicOpusEncoder
aiortc.codecs.CODECS["audio"][0].parameters.update(
    {
        "stereo": 1,
        "sprop-stereo": 1,
        "useinbandfec": 1,
        "maxaveragebitrate": OPUS_BITRATE,
    }
)


def _api_url(path: str) -> str:
    return urljoin(config.HUDDLE_BASE_URL.rstrip("/") + "/", path.lstrip("/"))


def _socket_url() -> str:
    parsed = urlparse(_api_url("api/realtime"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse(parsed._replace(scheme=scheme))


class RoomAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._process = None
        self._reader_task = None
        # Nearly half a second absorbs FFmpeg/network scheduling jitter. The
        # WebRTC sender consumes this queue at an exact 20 ms cadence.
        self._frames = asyncio.Queue(maxsize=BUFFERED_FRAMES)
        self._process_lock = asyncio.Lock()
        self._paused = True
        self._volume = 1.0
        self._pts = 0
        self._next_frame_at = None
        self._url = None
        self.track_id = None
        self._filters = []
        self._mix = None
        # Seconds into the source that the decoder has delivered so far.
        self._start_pos = 0.0
        self._src_pos = 0.0
        # A live DJ booth (dj.DJEngine) replaces the FFmpeg source entirely.
        self.dj = None

    async def configure(
        self,
        url: str,
        position_seconds: float,
        paused: bool,
        volume: int,
        duration_seconds: float = 0,
        settings: dict | None = None,
        track_id: str | None = None,
    ):
        self._volume = max(0.0, min(1.0, volume / 100))
        async with self._process_lock:
            await self._stop_process()
            self._paused = paused
            self._url = url
            self.track_id = track_id
            self._start_pos = self._src_pos = max(0.0, position_seconds)
            if paused or not url:
                return
            settings = settings or {}
            filters = self._base_filters(settings)
            # With AutoMix the mixer renders the transition; the old
            # fade-out/fade-in only applies to the plain crossfade setting.
            fade = 0 if settings.get("automix") else int(settings.get("crossfade_seconds") or 0)
            if fade:
                filters.append(f"afade=t=in:st=0:d={min(fade, 2)}")
                remaining = max(0, duration_seconds - position_seconds)
                fade_start = remaining - fade
                if fade_start > fade:
                    filters.append(
                        f"afade=t=out:st={fade_start:.2f}:d={fade}"
                    )
            self._filters = self._base_filters(settings)
            self._process = await self._spawn(url, position_seconds, filters)
            logger.info(
                "Started Huddle audio at %.2fs (pid %s)",
                position_seconds,
                self._process.pid,
            )
            self._reader_task = asyncio.create_task(self._pump_frames())

    @staticmethod
    def _base_filters(settings: dict) -> list:
        filters = []
        preset = settings.get("audio_filter")
        if preset in MUSIC_FILTERS:
            filters.append(MUSIC_FILTERS[preset])
        # Use libsoxr's high-quality resampler. `async=1000` used to
        # continuously stretch/drop samples to chase source timestamps,
        # which can sound like a faint watery noise on music.
        filters.append("aresample=48000:resampler=soxr:precision=28")
        return filters

    @staticmethod
    async def _spawn(url: str, position_seconds: float, filters: list):
        command = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-ss", f"{max(0.0, position_seconds):.3f}",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            "-i", url, "-vn", "-af", ",".join(filters),
            "-acodec", "pcm_s16le", "-f", "s16le",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "pipe:1",
        ]
        return await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    def set_mix(self, mix: dict | None):
        """Arm a rendered transition out of the song now playing.

        mix: {url, out_at, segment (s16le bytes), next_url, next_resume,
              next_track_id, on_start (called when the segment goes out)}
        """
        self._mix = mix

    def mix_armed_for(self, url: str) -> dict | None:
        mix = self._mix
        if mix and mix["url"] == url and self._start_pos < mix["out_at"] - 0.25:
            return mix
        return None

    def set_volume(self, volume: int):
        self._volume = max(0.0, min(1.0, volume / 100))

    async def _stop_process(self):
        reader_task = self._reader_task
        self._reader_task = None
        if reader_task:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break

        process = self._process
        self._process = None
        await self._kill(process)

    @staticmethod
    async def _kill(process):
        if not process or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), timeout=2)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()

    async def _put_bytes(self, pending: bytearray):
        while len(pending) >= BYTES_PER_FRAME:
            await self._frames.put(bytes(pending[:BYTES_PER_FRAME]))
            del pending[:BYTES_PER_FRAME]

    async def _pump_frames(self):
        pending = bytearray()
        bytes_per_second = SAMPLE_RATE * CHANNELS * 2
        try:
            while True:
                process = self._process
                if not process or not process.stdout:
                    return
                try:
                    data = await process.stdout.readexactly(BYTES_PER_FRAME)
                except asyncio.IncompleteReadError as partial:
                    pending.extend(partial.partial)
                    await self._put_bytes(pending)
                    return
                mix = self.mix_armed_for(self._url)
                frame_seconds = len(data) / bytes_per_second
                if mix and self._src_pos + frame_seconds >= mix["out_at"]:
                    # Cut the outgoing song on the exact sample, splice in
                    # the rendered transition, then carry on with the next
                    # song where the transition leaves it.
                    keep = int((mix["out_at"] - self._src_pos) * SAMPLE_RATE) * CHANNELS * 2
                    pending.extend(data[: max(0, keep)])
                    await self._put_bytes(pending)
                    await self._frames.put(mix["on_start"])
                    pending.extend(mix["segment"])
                    self._mix = None
                    self._url = mix["next_url"]
                    self.track_id = mix["next_track_id"]
                    self._start_pos = self._src_pos = mix["next_resume"]
                    replacement = await self._spawn(
                        mix["next_url"], mix["next_resume"], self._filters)
                    self._process = replacement
                    asyncio.create_task(self._kill(process))
                    logger.info("AutoMix: transition into %s (pid %s)",
                                mix["next_track_id"], replacement.pid)
                    await self._put_bytes(pending)
                    continue
                self._src_pos += frame_seconds
                pending.extend(data)
                # Backpressure keeps FFmpeg close to the listener instead of
                # racing through the track and dropping decoded music.
                await self._put_bytes(pending)
        except (asyncio.CancelledError, ConnectionError):
            return

    async def shutdown(self):
        async with self._process_lock:
            await self._stop_process()
        super().stop()

    async def recv(self):
        loop = asyncio.get_running_loop()
        frame_duration = SAMPLES_PER_FRAME / SAMPLE_RATE
        if self._next_frame_at is None:
            self._next_frame_at = loop.time()
        else:
            self._next_frame_at += frame_duration
            now = loop.time()
            # Never burst several packets to "catch up" after FFmpeg/network
            # briefly delayed a frame. Bursts sound like tiny dips at the
            # receiver even though no PCM was lost.
            if self._next_frame_at < now:
                self._next_frame_at = now
            else:
                await asyncio.sleep(self._next_frame_at - now)

        data = bytes(BYTES_PER_FRAME)
        engine = self.dj
        if engine is not None:
            try:
                data = engine.render()
            except Exception:
                logger.exception("DJ render failed")
            frame = AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
            frame.planes[0].update(data)
            frame.sample_rate = SAMPLE_RATE
            frame.pts = self._pts
            frame.time_base = Fraction(1, SAMPLE_RATE)
            self._pts += SAMPLES_PER_FRAME
            return frame
        if not self._paused:
            try:
                # Wait for real PCM instead of injecting a silent frame every
                # time FFmpeg and the sender wake a few milliseconds apart.
                item = await asyncio.wait_for(self._frames.get(), timeout=1)
                if callable(item):
                    # A transition marker: the mix starts with this frame.
                    try:
                        item()
                    except Exception:
                        logger.exception("AutoMix start callback failed")
                    item = await asyncio.wait_for(self._frames.get(), timeout=1)
                data = item if isinstance(item, bytes) else data
            except asyncio.TimeoutError:
                pass

        if self._volume < 0.999 and data.strip(b"\0"):
            # Vectorised: the per-sample Python loop cost ~0.4 ms of every
            # 20 ms frame on the event loop the sender shares with everything.
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
            data = np.clip(samples * self._volume, -32768, 32767).astype("<i2").tobytes()

        frame = AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        frame.planes[0].update(data)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self._pts += SAMPLES_PER_FRAME
        return frame


class RoomPublisher:
    def __init__(self, channel_id: str, headers: dict):
        self.channel_id = channel_id
        self.headers = headers
        self.source = RoomAudioTrack()
        self.relay = MediaRelay()
        self.peers = {}
        self.pending_candidates = {}
        self.state_key = None
        self.active = True
        self.websocket = None
        self.mix_key = None
        self.mix_task = None
        self.last_player = None
        self.karaoke_tasks = {}          # track id -> instrumental prep task
        self.task = asyncio.create_task(self._run())

    async def _update_dj(self, session, player: dict):
        """The booth is on air: keep the room's own player out of the way."""
        if self.source.dj is not session.engine:
            await self.source.configure("", 0, True, 100)  # stop FFmpeg
            self.source.set_mix(None)
            self.mix_key = None
            self.source.dj = session.engine
            logger.info("DJ booth is live in Huddle room %s", self.channel_id)
        # Someone pressed play on the room player: pause it again, or the
        # hub would advance through the DJ's queue on its own.
        if player.get("track") and not player.get("paused"):
            now = time.monotonic()
            if now - getattr(self, "_dj_paused_at", 0) > 3:
                self._dj_paused_at = now
                asyncio.create_task(session.pause_hub())

    async def update(self, player: dict):
        session = dj.sessions.get(huddle.PREFIX + self.channel_id)
        if session and session.live:
            await self._update_dj(session, player)
            return
        if self.source.dj is not None:
            self.source.dj = None
            self.state_key = None  # pick the room player back up from scratch
        self.last_player = player
        track = player.get("track") or {}
        track_id = track.get("id")
        settings = huddle.settings_for(self.channel_id)
        settings_key = tuple(sorted(settings.items()))
        url = str(track.get("audioUrl") or "")
        instrumental = self._karaoke(player, settings)
        if instrumental:
            # Real stems karaoke: the song minus its vocal track, so the
            # phase-cancel filter is not needed (and would damage it).
            url, settings = instrumental, {**settings, "audio_filter": None}
        key = (
            track_id,
            bool(player.get("paused")),
            int(player.get("positionMs") or 0),
            int(player.get("updatedAt") or 0),
            settings_key,
            url,
        )
        volume = int(player.get("volume") or 100)
        if key == self.state_key:
            self.source.set_volume(volume)
            self._maybe_plan(player, settings)
            return
        # The hub catching up with a transition we already played: the new
        # song is running from the mix, so don't restart it.
        previous_id = self.state_key[0] if self.state_key else None
        if (track_id and track_id != previous_id
                and track_id == self.source.track_id
                and not player.get("paused")):
            self.state_key = key
            self.source.set_volume(volume)
            self._maybe_plan(player, settings)
            return

        # A seek or pause keeps the rendered transition: the source only
        # plays it if the new position is still before the mix point.
        self.state_key = key
        position_ms = int(player.get("positionMs") or 0)
        if track_id and not player.get("paused"):
            position_ms += max(
                0,
                int(time.time() * 1000) - int(player.get("updatedAt") or 0),
            )
        await self.source.configure(
            url,
            position_ms / 1000,
            bool(player.get("paused")),
            volume,
            float(track.get("duration") or 0),
            settings,
            track_id,
        )
        self._maybe_plan(player, settings)

    # ---------------------------------------------------------------- karaoke

    def _karaoke(self, player: dict, settings: dict):
        """The instrumental to play for the current song in karaoke mode.

        Returns None (keep the phase-cancel filter) until it is ready; the
        current and next songs are prepared in the background, and the room
        switches over at the same position when the current one is done.
        """
        if not (settings.get("karaoke_mode") or settings.get("audio_filter") == "karaoke"):
            return None
        if not stems.enabled():
            return None
        track = player.get("track") or {}
        upcoming = (player.get("queue") or [None])[0]
        for item in (track, upcoming):
            if item and item.get("audioUrl") and item.get("id") not in self.karaoke_tasks:
                self.karaoke_tasks[item["id"]] = asyncio.create_task(self._prepare_karaoke(item))
        if len(self.karaoke_tasks) > 20:
            for done in [k for k, t in self.karaoke_tasks.items() if t.done()][:10]:
                self.karaoke_tasks.pop(done, None)
        return stems.instrumental_for(_mix_keys(track)) if track.get("audioUrl") else None

    async def _prepare_karaoke(self, track: dict):
        try:
            try:
                await stems.prepare(track["audioUrl"], _mix_keys(track), track.get("title"),
                                    instrumental=True)
            except Exception:
                # The queued song's stream link may have expired (403).
                lookup = track.get("pageUrl") or track.get("query") or track.get("title")
                if not (dj.resolver and lookup):
                    raise
                resolved = await dj.resolver(lookup)
                await stems.prepare(resolved["audio_url"], _mix_keys(track), track.get("title"),
                                    instrumental=True)
        except Exception as error:
            logger.warning("Karaoke: could not split %r: %s", track.get("title"), error)
            return
        player = self.last_player or {}
        if (player.get("track") or {}).get("id") == track.get("id") and self.source.dj is None:
            self.state_key = None            # re-sync onto the instrumental
            await self.update(player)

    # ---------------------------------------------------------------- mixing

    def _mix_spec(self, track: dict, settings: dict):
        """The transition out of `track`: its own, else Auto with AutoMix."""
        spec = track.get("mix")
        if spec:
            return spec
        if settings.get("automix"):
            return {"preset": "auto"}
        return None

    def _maybe_plan(self, player: dict, settings: dict):
        track = player.get("track") or {}
        queue = player.get("queue") or []
        upcoming = queue[0] if queue else None
        spec = self._mix_spec(track, settings)
        if (not spec or not upcoming
                or player.get("loop") == "track"
                or settings.get("audio_filter") in ("nightcore", "slowed")
                or not track.get("audioUrl") or not upcoming.get("audioUrl")):
            if self.mix_key is not None:
                self.mix_key = None
                self.source.set_mix(None)
            return
        key = (track.get("id"), upcoming.get("id"), json.dumps(spec, sort_keys=True))
        if key == self.mix_key or player.get("paused"):
            return
        self.mix_key = key
        self.source.set_mix(None)
        if self.mix_task and not self.mix_task.done():
            self.mix_task.cancel()
        self.mix_task = asyncio.create_task(self._prepare_mix(key, track, upcoming, spec))

    async def _prepare_mix(self, key, track: dict, upcoming: dict, spec: dict):
        out_url, in_url = track["audioUrl"], upcoming["audioUrl"]
        try:
            out_meta, in_meta = await asyncio.gather(
                mixer.analyze_async(out_url, *_mix_keys(track)),
                mixer.analyze_async(in_url, *_mix_keys(upcoming)),
            )
            plan = mixer.plan(out_meta, in_meta, spec, track.get("duration"),
                              self.source._src_pos)
            if not plan:
                return
            loop = asyncio.get_running_loop()
            segment = await loop.run_in_executor(None, mixer.render, out_url, in_url, plan)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("AutoMix: could not prepare %r -> %r: %s",
                           track.get("title"), upcoming.get("title"), error)
            return
        if self.mix_key != key or self.source._url != out_url:
            return
        channel_id, from_id = self.channel_id, track["id"]
        position_ms = int(plan["in_at"] * 1000)

        def on_start():
            asyncio.create_task(huddle._request("POST", "/api/bot/player", {
                "channelId": channel_id,
                "action": {"name": "mixAdvance", "fromTrackId": from_id,
                           "positionMs": position_ms},
            }))

        self.source.set_mix({
            "url": out_url, "out_at": plan["out_at"], "segment": segment,
            "next_url": in_url, "next_resume": plan["in_resume_at"],
            "next_track_id": upcoming["id"], "on_start": on_start,
        })
        logger.info("AutoMix: %s (%s bars) %r -> %r at %.1fs",
                    plan["spec"]["preset"], plan["spec"]["bars"],
                    track.get("title"), upcoming.get("title"), plan["out_at"])

    async def stop(self):
        self.active = False
        if self.mix_task and not self.mix_task.done():
            self.mix_task.cancel()
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()
        for peer in list(self.peers.values()):
            await peer.close()
        self.peers.clear()
        await self.source.shutdown()
        if self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self):
        while self.active:
            try:
                async with aiohttp.ClientSession(headers=self.headers) as session:
                    async with session.ws_connect(
                        _socket_url(), heartbeat=20
                    ) as websocket:
                        self.websocket = websocket
                        await websocket.send_json(
                            {"t": "voice-join", "channelId": self.channel_id}
                        )
                        # A music publisher only sends. Marking it deafened is
                        # both truthful in the UI and prevents browsers from
                        # treating it like another microphone listener.
                        await websocket.send_json(
                            {
                                "t": "voice-state",
                                "muted": False,
                                "deafened": True,
                            }
                        )
                        logger.info(
                            "Music publisher joined Huddle room %s",
                            self.channel_id,
                        )
                        async for message in websocket:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                await self._message(json.loads(message.data))
                            elif message.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
            except asyncio.CancelledError:
                break
            except Exception as error:
                if self.active:
                    logger.warning(
                        "Huddle publisher %s reconnecting: %s",
                        self.channel_id,
                        error,
                    )
            self.websocket = None
            if self.active:
                await asyncio.sleep(2)

    async def _message(self, payload: dict):
        if payload.get("t") != "signal":
            return
        remote_id = payload.get("from")
        data = payload.get("data") or {}
        kind = data.get("kind")
        if not remote_id:
            return
        if kind == "offer" and data.get("description"):
            await self._answer(remote_id, data["description"])
        elif kind == "candidate" and data.get("candidate"):
            await self._candidate(remote_id, data["candidate"])

    async def _answer(self, remote_id: str, description: dict):
        peer = self.peers.get(remote_id)
        if peer is None or peer.connectionState in ("closed", "failed"):
            peer = RTCPeerConnection()
            self.peers[remote_id] = peer

            @peer.on("connectionstatechange")
            async def connectionstatechange():
                if peer.connectionState in ("failed", "closed"):
                    await peer.close()
                    if self.peers.get(remote_id) is peer:
                        self.peers.pop(remote_id, None)

        await peer.setRemoteDescription(
            RTCSessionDescription(
                sdp=description["sdp"],
                type=description["type"],
            )
        )
        # Reuse the offer's first audio m-line for the music stream and reject
        # every incoming media section. The bot is permanently deafened but
        # never muted: it sends music and receives no microphones/screens.
        music_attached = False
        for transceiver in peer.getTransceivers():
            if transceiver.kind == "audio" and not music_attached:
                transceiver.direction = "sendonly"
                if transceiver.sender.track is None:
                    transceiver.sender.replaceTrack(
                        self.relay.subscribe(self.source)
                    )
                music_attached = True
            else:
                transceiver.direction = "inactive"
                if transceiver.sender.track is not None:
                    transceiver.sender.replaceTrack(None)
        if not music_attached:
            peer.addTransceiver(
                self.relay.subscribe(self.source),
                direction="sendonly",
            )
        for candidate in self.pending_candidates.pop(remote_id, []):
            await peer.addIceCandidate(candidate)
        answer = await peer.createAnswer()
        await peer.setLocalDescription(answer)
        await self.websocket.send_json(
            {
                "t": "signal",
                "to": remote_id,
                "data": {
                    "kind": "answer",
                    "description": {
                        "type": peer.localDescription.type,
                        "sdp": peer.localDescription.sdp,
                    },
                },
            }
        )

    async def _candidate(self, remote_id: str, raw: dict):
        candidate_text = raw.get("candidate") or ""
        if not candidate_text:
            return
        if candidate_text.startswith("candidate:"):
            candidate_text = candidate_text[len("candidate:") :]
        candidate = candidate_from_sdp(candidate_text)
        candidate.sdpMid = raw.get("sdpMid")
        candidate.sdpMLineIndex = raw.get("sdpMLineIndex")
        peer = self.peers.get(remote_id)
        if peer and peer.remoteDescription:
            await peer.addIceCandidate(candidate)
        else:
            self.pending_candidates.setdefault(remote_id, []).append(candidate)


def keep_audio_loop_responsive():
    """Settings that stop the rest of the bot from starving the audio sender.

    - Thread switch interval 5 ms -> 0.5 ms: with two busy Python threads
      (DJ, stems, analysis) driving the real sender for 15 s, the default lost
      72% of the audio in holes up to ~900 ms; 1 ms still lost 5-9% (holes up
      to ~100 ms); 0.5 ms lost 0-0.6%.
    - gc.freeze(): everything loaded by now (modules, caches, the library
      index) leaves the collector's scans, so a full collection no longer
      walks it and freezes the loop for hundreds of milliseconds. Frozen
      objects are still freed normally when nothing refers to them.
    """
    sys.setswitchinterval(0.0005)
    gc.collect()
    gc.freeze()
    logger.info("Audio loop: 0.5 ms thread switching, %d startup objects frozen out of GC", gc.get_freeze_count())


class HuddleVoiceManager:
    def __init__(self, cog=None, song_class=None):
        self.headers = {
            "Authorization": f"Bearer {config.HUDDLE_BOT_TOKEN}",
            "Accept": "application/json",
        }
        self.publishers = {}
        self.cog = cog
        self.song_class = song_class
        self.autoplay_tasks = {}
        self.resolve_tasks = {}
        self.recorded_tracks = {}
        #: channel id -> the room's raw hub player, refreshed every poll
        self.players = {}
        self.task = None

    async def start(self):
        # Logs where the loop was stuck whenever a stall makes audio drop out.
        if getattr(self, "watchdog", None) is None:
            self.watchdog = LoopWatchdog.maybe_start()
            keep_audio_loop_responsive()
        global MANAGER
        if not config.HUDDLE_BASE_URL or not config.HUDDLE_BOT_TOKEN:
            logger.info("Huddle WebRTC publisher disabled (not configured)")
            return
        MANAGER = self
        self.task = asyncio.create_task(self._run())
        logger.info("Huddle WebRTC publisher started")

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        for publisher in list(self.publishers.values()):
            await publisher.stop()
        self.publishers.clear()
        for task in self.autoplay_tasks.values():
            task.cancel()
        self.autoplay_tasks.clear()

    async def _run(self):
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(
            headers=self.headers, timeout=timeout
        ) as session:
            while True:
                try:
                    async with session.get(_api_url("api/bot/servers")) as response:
                        data = await response.json(content_type=None)
                        if response.status >= 400:
                            raise RuntimeError(
                                data.get("error") or f"Huddle returned {response.status}"
                            )
                    await self._sync(data)
                    delay = self._poll_delay(data)
                except asyncio.CancelledError:
                    break
                except Exception as error:
                    logger.warning("Huddle voice state poll failed: %s", error)
                    delay = 1
                await asyncio.sleep(delay)

    #: Seconds between polls while someone is in voice or something plays.
    POLL_ACTIVE = 1
    #: Seconds between polls while every voice room is empty and silent.
    POLL_IDLE = 5

    def _poll_delay(self, data: dict) -> float:
        """Poll every second only while music could start or is playing.

        Music only plays in a voice room someone is in, and /play needs its
        caller in voice, so while every room is empty nothing can start; the
        once-a-second poll was ~10,000 requests every three hours to Huddle
        regardless.
        """
        if self.publishers:
            return self.POLL_ACTIVE
        for server in data.get("servers") or []:
            for room in server.get("voiceChannels") or []:
                if (room.get("player") or {}).get("track"):
                    return self.POLL_ACTIVE
                if any(not member.get("bot") for member in room.get("members") or []):
                    return self.POLL_ACTIVE
        return self.POLL_IDLE

    #: How many queued placeholders to have resolved ahead of time.
    RESOLVE_AHEAD = 2

    def _resolve_upcoming(self, channel_id: str, player: dict):
        """Look up playlist placeholders just before they are needed."""
        upcoming = [player.get("track")] + list(player.get("queue") or [])[: self.RESOLVE_AHEAD]
        for index, track in enumerate(upcoming):
            if not track or track.get("audioUrl") or not track.get("query"):
                continue
            track_id = track.get("id")
            task = self.resolve_tasks.get(track_id)
            if task and not task.done():
                continue
            self.resolve_tasks[track_id] = asyncio.create_task(
                self._resolve_track(channel_id, track, current=index == 0)
            )
        for track_id, task in list(self.resolve_tasks.items()):
            if task.done():
                self.resolve_tasks.pop(track_id, None)

    async def _resolve_track(self, channel_id: str, track: dict, current: bool):
        try:
            await huddle.resolve(channel_id, track["id"])
        except Exception as error:
            logger.warning("Could not resolve %r: %s", track.get("title"), error)
            if current:
                # Don't sit silently on a song that cannot be found.
                try:
                    await huddle._request("POST", "/api/bot/player", {
                        "channelId": channel_id,
                        "action": {"name": "skip"},
                    })
                except Exception:
                    pass

    async def _sync(self, data: dict):
        active = {}
        players = {}
        for server in data.get("servers") or []:
            for room in server.get("voiceChannels") or []:
                player = room.get("player") or {}
                players[room["id"]] = player
                if player.get("track"):
                    self._resolve_upcoming(room["id"], player)
                    active[room["id"]] = player
                    await self._observe_room(room["id"], player)
        self.players = players
        # A room with the DJ booth on stays published even with no track.
        for key, session in list(dj.sessions.items()):
            channel_id = huddle.channel_id_from(key)
            if channel_id and session.live:
                if channel_id not in players:
                    await session.stop()  # the room was deleted
                    continue
                active.setdefault(channel_id, players[channel_id])

        for channel_id in set(self.publishers) - set(active):
            publisher = self.publishers.pop(channel_id)
            await publisher.stop()
            logger.info("Music publisher left Huddle room %s", channel_id)

        for channel_id, player in active.items():
            publisher = self.publishers.get(channel_id)
            if publisher is None:
                publisher = RoomPublisher(channel_id, self.headers)
                self.publishers[channel_id] = publisher
            await publisher.update(player)

    async def _observe_room(self, channel_id: str, player: dict):
        track = player.get("track") or {}
        track_id = track.get("id")
        if track_id and self.recorded_tracks.get(channel_id) != track_id:
            self.recorded_tracks[channel_id] = track_id
            huddle.record_play(channel_id, track)

        settings = huddle.settings_for(channel_id)
        if not settings.get("autoplay") or player.get("queue"):
            return
        duration = float(track.get("duration") or 0)
        if not duration:
            return
        position = float(player.get("positionMs") or 0) / 1000
        if not player.get("paused"):
            position += max(
                0,
                time.time() - float(player.get("updatedAt") or 0) / 1000,
            )
        if duration - position > 75:
            return
        existing = self.autoplay_tasks.get(channel_id)
        if existing and not existing.done():
            return
        self.autoplay_tasks[channel_id] = asyncio.create_task(
            self._queue_autoplay(channel_id, track)
        )

    async def _queue_autoplay(self, channel_id: str, track: dict):
        if not self.cog or not self.song_class:
            return
        try:
            guild = SimpleNamespace(
                id=huddle.PREFIX + channel_id,
                name=f"Huddle · {channel_id}",
                voice_client=None,
                me=SimpleNamespace(
                    id=0,
                    bot=True,
                    display_name="Huddle Autoplay",
                ),
            )
            player = self.cog.get_player(guild)
            requester = guild.me
            song = self.song_class(
                title=track.get("title") or "Unknown",
                url=track.get("pageUrl") or track.get("audioUrl"),
                duration=huddle._format_duration(track.get("duration")) or "Unknown",
                requester=requester,
                source_type="youtube",
                thumbnail=track.get("thumbnail"),
                artist=track.get("artist"),
            )
            if not player.current or player.current.url != song.url:
                if player.current:
                    player.history.appendleft(player.current)
                player.current = song
            settings = huddle.settings_for(channel_id)
            player.autoplay = True
            player.artist_diversity = bool(settings.get("artist_diversity"))
            player.vibe_match = bool(settings.get("vibe_match"))
            recommendation = await self.cog.pick_autoplay_recommendation(player)
            if recommendation:
                await huddle.play(
                    huddle.PREFIX + channel_id,
                    recommendation.url,
                    requested_by="Smart Autoplay",
                )
                logger.info(
                    "Queued Huddle autoplay for %s: %s",
                    channel_id,
                    recommendation.title,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Huddle autoplay failed for %s: %s", channel_id, error)
