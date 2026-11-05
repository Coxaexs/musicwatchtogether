import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui

HOFFLE_PATHS = [
    ("GET", "/api/guilds"),
    ("GET", "/api/guilds/huddle:room"),
    ("POST", "/api/guilds/huddle:room/action"),
    ("POST", "/api/guilds/huddle:room/play"),
    ("GET", "/api/guilds/huddle:room/lyrics"),
    ("GET", "/api/guilds/huddle:room/playlists"),
    ("POST", "/api/guilds/huddle:room/dj"),
    ("GET", "/api/huddle/search"),
    ("POST", "/api/huddle/resolve"),
    ("GET", "/api/lyrics/search"),
    ("GET", "/api/lyrics/candidates"),
    ("POST", "/api/lyrics/pick"),
    ("POST", "/api/lyrics/ask-musixmatch"),
    ("GET", "/api/library/songs"),
    ("POST", "/api/library/delete"),
    ("POST", "/api/admin/huddle-link"),
    ("POST", "/api/admin/watch-ticket"),
    ("POST", "/api/admin/watch-command"),
    ("GET", "/api/admin/musixmatch"),
]
DASHBOARD_ONLY = [
    ("POST", "/api/guilds/huddle:room/stems"),
    ("GET", "/api/guilds/huddle:room/playlists/mix"),
    ("POST", "/api/admin/something-else"),
    ("GET", "/api/stats"),
]


async def ok(request):
    return web.json_response({"ok": True})


class HoffleBotKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ui = webui.WebUI.__new__(webui.WebUI)
        app = web.Application(middlewares=[self.ui.auth_middleware])
        for method, path in HOFFLE_PATHS + DASHBOARD_ONLY:
            app.router.add_route(method, path, ok)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        patches = [
            mock.patch.object(webui.config, "HOFFLE_BOT_KEY", "the-key"),
            mock.patch.object(webui.config, "WEB_UI_PASSWORD", "dashboard"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def asyncTearDown(self):
        await self.client.close()

    async def status(self, method, path, key=None):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        response = await self.client.request(method, path, headers=headers)
        return response.status

    async def test_key_opens_every_route_hoffle_uses(self):
        for method, path in HOFFLE_PATHS:
            self.assertEqual(await self.status(method, path, "the-key"), 200, path)

    async def test_key_does_not_open_the_rest_of_the_dashboard(self):
        for method, path in DASHBOARD_ONLY:
            self.assertEqual(await self.status(method, path, "the-key"), 401, path)

    async def test_wrong_or_missing_key_is_refused(self):
        for method, path in HOFFLE_PATHS:
            self.assertEqual(await self.status(method, path, "wrong"), 401, path)
            self.assertEqual(await self.status(method, path), 401, path)

    async def test_no_key_set_means_the_key_never_works(self):
        with mock.patch.object(webui.config, "HOFFLE_BOT_KEY", ""):
            self.assertEqual(await self.status("GET", "/api/guilds", "the-key"), 401)
            self.assertEqual(await self.status("GET", "/api/guilds", " "), 401)


if __name__ == "__main__":
    unittest.main()
