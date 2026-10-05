import asyncio
import json
import unittest
from collections import deque
from types import SimpleNamespace

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

import webui


class FakeVoice:
    channel = SimpleNamespace(name="Music")

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False


class LiveDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_socket_pushes_changed_state(self):
        player = SimpleNamespace(
            current=None, queue=deque(), history=deque(), volume=0.5,
            loop=False, loop_queue=False, autoplay=False, artist_diversity=True,
            vibe_match=True, audio_filter=None, crossfade_seconds=0,
            automix_enabled=False, automix_blend_seconds=0, karaoke_mode=False,
            idle_disconnect_seconds=300, sleep_timer_ends_at=None,
            is_247_mode=False,
        )
        guild = SimpleNamespace(id=42, name="Test guild", icon=None,
                                voice_client=FakeVoice())
        cog = SimpleNamespace(get_player=lambda _guild: player)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 42 else None,
                              get_cog=lambda name: cog if name == "MusicCog" else None)
        ui = webui.WebUI(bot)
        app = web.Application()
        app.router.add_get("/api/live", ui.api_live)
        server = TestServer(app)
        try:
            await server.start_server()
        except PermissionError:
            self.skipTest("this sandbox does not permit local listener sockets")
        try:
            async with ClientSession() as session:
                socket = await session.ws_connect(server.make_url("/api/live?guild_id=42"))
                first = json.loads((await asyncio.wait_for(socket.receive(), 2)).data)
                self.assertEqual(first["volume"], 50)
                player.volume = 0.72
                second = json.loads((await asyncio.wait_for(socket.receive(), 2)).data)
                self.assertEqual(second["volume"], 72)
                await socket.close()
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
