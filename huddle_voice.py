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
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
from av import AudioFrame
from aiortc import (
    MediaStreamTrack,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp

import config


logger = logging.getLogger("MusicBot.HuddleVoice")
SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = 960
CHANNELS = 2
BYTES_PER_FRAME = SAMPLES_PER_FRAME * CHANNELS * 2


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
        self._frames = asyncio.Queue(maxsize=4)
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
    ):
        self._volume = max(0.0, min(1.0, volume / 100))
        async with self._process_lock:
            await self._stop_process()
            self._paused = paused
            self._url = url
            if paused or not url:
                return
            command = [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-re",
                "-ss",
                f"{max(0.0, position_seconds):.3f}",
                "-i",
                url,
                "-vn",
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
                if self._frames.full():
                    try:
                        self._frames.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                self._frames.put_nowait(data)
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
            await asyncio.sleep(max(0.0, self._next_frame_at - loop.time()))

        data = bytes(BYTES_PER_FRAME)
        if not self._paused:
            try:
                data = self._frames.get_nowait()
            except asyncio.QueueEmpty:
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
        key = (
            track_id,
            bool(player.get("paused")),
            int(player.get("positionMs") or 0),
            int(player.get("updatedAt") or 0),
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
            peer.addTrack(self.relay.subscribe(self.source))

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
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {config.HUDDLE_BOT_TOKEN}",
            "Accept": "application/json",
        }
        self.publishers = {}
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
