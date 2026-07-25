"""Real WebRTC audio publishing for Huddle voice rooms.

The existing Huddle player remains the command/queue control plane. This module
watches that state, decodes each room's current source exactly once with FFmpeg,
and publishes the resulting audio as a genuine WebRTC bot participant.
"""

import asyncio
from array import array
from fractions import Fraction
import json
import logging
import sys
import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
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
import huddle


logger = logging.getLogger("MusicBot.HuddleVoice")
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

    async def configure(
        self,
        url: str,
        position_seconds: float,
        paused: bool,
        volume: int,
        duration_seconds: float = 0,
        settings: dict | None = None,
    ):
        self._volume = max(0.0, min(1.0, volume / 100))
        async with self._process_lock:
            await self._stop_process()
            self._paused = paused
            self._url = url
            if paused or not url:
                return
            settings = settings or {}
            filters = []
            preset = settings.get("audio_filter")
            if preset in MUSIC_FILTERS:
                filters.append(MUSIC_FILTERS[preset])
            # Use libsoxr's high-quality resampler. `async=1000` used to
            # continuously stretch/drop samples to chase source timestamps,
            # which can sound like a faint watery noise on music.
            filters.append("aresample=48000:resampler=soxr:precision=28")
            fade = (
                int(settings.get("automix_blend") or 8)
                if settings.get("automix")
                else int(settings.get("crossfade_seconds") or 0)
            )
            if fade:
                filters.append(f"afade=t=in:st=0:d={min(fade, 2)}")
                remaining = max(0, duration_seconds - position_seconds)
                fade_start = remaining - fade
                if fade_start > fade:
                    filters.append(
                        f"afade=t=out:st={fade_start:.2f}:d={fade}"
                    )

            command = [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, position_seconds):.3f}",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "2",
                "-i",
                url,
                "-vn",
                "-af",
                ",".join(filters),
                "-acodec",
                "pcm_s16le",
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                str(CHANNELS),
                "pipe:1",
            ]
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info(
                "Started Huddle audio at %.2fs (pid %s)",
                position_seconds,
                self._process.pid,
            )
            self._reader_task = asyncio.create_task(self._pump_frames())

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
        if not process or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), timeout=2)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()

    async def _pump_frames(self):
        process = self._process
        if not process or not process.stdout:
            return
        try:
            while process.returncode is None:
                data = await process.stdout.readexactly(BYTES_PER_FRAME)
                # Backpressure keeps FFmpeg close to the listener instead of
                # racing through the track and dropping decoded music.
                await self._frames.put(data)
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError):
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
        if not self._paused:
            try:
                # Wait for real PCM instead of injecting a silent frame every
                # time FFmpeg and the sender wake a few milliseconds apart.
                data = await asyncio.wait_for(self._frames.get(), timeout=1)
            except asyncio.TimeoutError:
                pass

        if self._volume < 0.999 and data.strip(b"\0"):
            samples = array("h")
            samples.frombytes(data)
            if sys.byteorder != "little":
                samples.byteswap()
            gain = self._volume
            for index, sample in enumerate(samples):
                samples[index] = max(-32768, min(32767, int(sample * gain)))
            if sys.byteorder != "little":
                samples.byteswap()
            data = samples.tobytes()

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
        self.task = asyncio.create_task(self._run())

    async def update(self, player: dict):
        track = player.get("track") or {}
        track_id = track.get("id")
        settings = huddle.settings_for(self.channel_id)
        settings_key = tuple(sorted(settings.items()))
        key = (
            track_id,
            bool(player.get("paused")),
            int(player.get("positionMs") or 0),
            int(player.get("updatedAt") or 0),
            settings_key,
        )
        volume = int(player.get("volume") or 100)
        if key == self.state_key:
            self.source.set_volume(volume)
            return

        self.state_key = key
        position_ms = int(player.get("positionMs") or 0)
        if track_id and not player.get("paused"):
            position_ms += max(
                0,
                int(time.time() * 1000) - int(player.get("updatedAt") or 0),
            )
        await self.source.configure(
            str(track.get("audioUrl") or ""),
            position_ms / 1000,
            bool(player.get("paused")),
            volume,
            float(track.get("duration") or 0),
            settings,
        )

    async def stop(self):
        self.active = False
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
        self.recorded_tracks = {}
        self.task = None

    async def start(self):
        if not config.HUDDLE_BASE_URL or not config.HUDDLE_BOT_TOKEN:
            logger.info("Huddle WebRTC publisher disabled (not configured)")
            return
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
                except asyncio.CancelledError:
                    break
                except Exception as error:
                    logger.warning("Huddle voice state poll failed: %s", error)
                await asyncio.sleep(1)

    async def _sync(self, data: dict):
        active = {}
        for server in data.get("servers") or []:
            for room in server.get("voiceChannels") or []:
                player = room.get("player") or {}
                if player.get("track"):
                    active[room["id"]] = player
                    await self._observe_room(room["id"], player)

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
