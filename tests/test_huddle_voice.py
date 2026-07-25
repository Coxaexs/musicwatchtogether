import asyncio
import json
import os
import unittest

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

import config
import huddle_voice


class RoomAudioTrackTests(unittest.IsolatedAsyncioTestCase):
    async def test_music_opus_profile(self):
        encoder = huddle_voice.MusicOpusEncoder()
        self.assertEqual(encoder.codec.bit_rate, 256_000)
        self.assertEqual(encoder.codec.options.get("application"), "audio")

    async def test_silence_frame_shape(self):
        track = huddle_voice.RoomAudioTrack()
        frame = await track.recv()
        self.assertEqual(frame.sample_rate, 48_000)
        self.assertEqual(frame.samples, 960)
        self.assertEqual(frame.layout.name, "stereo")
        self.assertFalse(any(bytes(frame.planes[0])))
        await track.shutdown()


@unittest.skipUnless(
    os.getenv("HUDDLE_LIVE_TEST") == "1",
    "set HUDDLE_LIVE_TEST=1 for the live WebRTC probe",
)
class LiveHuddleWebRTCTests(unittest.IsolatedAsyncioTestCase):
    async def test_receives_non_silent_bot_audio(self):
        headers = {
            "Authorization": f"Bearer {config.HUDDLE_BOT_TOKEN}",
        }
        peer = RTCPeerConnection()
        peer.addTransceiver("audio", direction="recvonly")
        incoming_track = asyncio.get_running_loop().create_future()

        @peer.on("track")
        def track_received(track):
            if track.kind == "audio" and not incoming_track.done():
                incoming_track.set_result(track)

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.ws_connect(
                huddle_voice._socket_url(), heartbeat=20
            ) as websocket:
                ready = json.loads((await websocket.receive()).data)
                self.assertEqual(ready["t"], "ready")
                own_id = ready["connectionId"]
                await websocket.send_json(
                    {"t": "voice-join", "channelId": "kitchen-table"}
                )

                target_id = None
                while not target_id:
                    payload = json.loads((await websocket.receive()).data)
                    if payload.get("t") != "voice":
                        continue
                    for participant in payload.get("participants") or []:
                        if (
                            participant.get("bot")
                            and participant["connectionId"] != own_id
                        ):
                            target_id = participant["connectionId"]
                            break

                offer = await peer.createOffer()
                await peer.setLocalDescription(offer)
                await websocket.send_json(
                    {
                        "t": "signal",
                        "to": target_id,
                        "data": {
                            "kind": "offer",
                            "description": {
                                "type": peer.localDescription.type,
                                "sdp": peer.localDescription.sdp,
                            },
                        },
                    }
                )

                while peer.remoteDescription is None:
                    payload = json.loads((await websocket.receive()).data)
                    if payload.get("t") != "signal":
                        continue
                    data = payload.get("data") or {}
                    if data.get("kind") == "answer":
                        description = data["description"]
                        await peer.setRemoteDescription(
                            RTCSessionDescription(
                                sdp=description["sdp"],
                                type=description["type"],
                            )
                        )

                track = await asyncio.wait_for(incoming_track, timeout=15)
                audible = False
                silent_after_audio = 0
                observed_after_audio = 0
                for _ in range(130):
                    frame = await asyncio.wait_for(track.recv(), timeout=3)
                    has_signal = any(bytes(frame.planes[0]))
                    if has_signal:
                        audible = True
                    if audible:
                        observed_after_audio += 1
                        if not has_signal:
                            silent_after_audio += 1
                        if observed_after_audio >= 100:
                            break
                self.assertTrue(audible, "WebRTC arrived but carried only silence")
                self.assertGreaterEqual(
                    observed_after_audio,
                    100,
                    "WebRTC audio stopped before the continuity window completed",
                )
                self.assertLessEqual(
                    silent_after_audio,
                    1,
                    "WebRTC inserted silent holes into continuous music",
                )

        await peer.close()
