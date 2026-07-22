import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import security
import storage
import watchtogether
import webui


class JsonRequest:
    def __init__(self, body, remote="127.0.0.1"):
        self._body = body
        self.remote = remote
        self.headers = {}
        self.secure = False

    async def json(self):
        return self._body


class PublicURLTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_loopback_literal(self):
        with self.assertRaises(security.PublicURLRequired):
            await security.validate_public_url("http://127.0.0.1/admin")

    async def test_rejects_hostname_resolving_private(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 80))]
        with mock.patch("security.socket.getaddrinfo", return_value=answer):
            with self.assertRaises(security.PublicURLRequired):
                await security.validate_public_url("https://example.test/video")

    async def test_accepts_hostname_when_every_address_is_public(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch("security.socket.getaddrinfo", return_value=answer):
            value = await security.validate_public_url("https://example.test/video")
        self.assertEqual(value, "https://example.test/video")


class LimiterTests(unittest.TestCase):
    def test_window_limit(self):
        limiter = security.SlidingWindowLimiter(2, 60)
        self.assertTrue(limiter.allow("client"))
        self.assertTrue(limiter.allow("client"))
        self.assertFalse(limiter.allow("client"))
        limiter.discard("client")
        self.assertTrue(limiter.allow("client"))

    def test_spoofed_forwarding_header_is_ignored_for_untrusted_peer(self):
        request = mock.Mock(remote="198.51.100.10", headers={"CF-Connecting-IP": "1.1.1.1"})
        self.assertEqual(security.client_identity(request), "198.51.100.10")


class PersistenceTests(unittest.TestCase):
    def test_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertTrue(storage.save_json(path, {"ok": [1, 2, 3]}))
            self.assertEqual(storage.load_json(path, {}), {"ok": [1, 2, 3]})

    def test_sqlite_store_migrates_json_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "legacy.json"
            database = Path(directory) / "state.sqlite3"
            storage.save_json(legacy, {"rooms": ["one"]})
            store = storage.SQLiteDocumentStore(database)
            self.assertEqual(store.load("rooms", {}, legacy), {"rooms": ["one"]})
            storage.save_json(legacy, {"rooms": ["changed"]})
            reopened = storage.SQLiteDocumentStore(database)
            self.assertEqual(reopened.load("rooms", {}, legacy), {"rooms": ["one"]})
            self.assertTrue(reopened.healthy())

    def test_atomic_binary_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cover.png"
            self.assertTrue(storage.save_bytes_atomic(path, b"image-data"))
            self.assertEqual(path.read_bytes(), b"image-data")


class ImageSafetyTests(unittest.TestCase):
    def test_png_dimensions_are_read_without_decoding(self):
        image = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (640).to_bytes(4, "big") \
            + (480).to_bytes(4, "big")
        self.assertEqual(webui._image_dimensions(image, "png"), (640, 480))

    def test_truncated_image_is_rejected(self):
        self.assertIsNone(webui._image_dimensions(b"\x89PNG", "png"))


class AuthorizationTests(unittest.TestCase):
    def test_admin_api_accepts_master_session_but_not_guild_invitation(self):
        ui = webui.WebUI(None)
        now = webui.time.time()
        webui.temp_tokens["admin-test"] = {
            "guild_id": None, "kind": "session", "created_at": now}
        webui.temp_tokens["invite-test"] = {
            "guild_id": 42, "user_id": 7, "created_at": now}
        try:
            self.assertTrue(ui._authorized_admin(
                mock.Mock(cookies={"mb_session": "admin-test"})))
            self.assertFalse(ui._authorized_admin(
                mock.Mock(cookies={"mb_session": "invite-test"})))
        finally:
            webui.temp_tokens.pop("admin-test", None)
            webui.temp_tokens.pop("invite-test", None)

    def test_expired_dashboard_token_is_rejected_and_removed(self):
        token = "expired-test-token"
        webui.temp_tokens[token] = {
            "guild_id": 1,
            "created_at": 0,
        }
        with mock.patch("webui._save_tokens"):
            self.assertIsNone(webui._valid_token_info(token))
        self.assertNotIn(token, webui.temp_tokens)

    def test_media_is_bound_to_room_queue(self):
        room = watchtogether.Room("w1", "Room")
        room.queue.append({"file": "video.mp4"})
        room.queue.append({"file": "hls/abc/index.m3u8"})
        self.assertTrue(watchtogether._room_owns_file(room, "video.mp4"))
        self.assertTrue(watchtogether._room_owns_file(room, "hls/abc/s000001.ts"))
        self.assertFalse(watchtogether._room_owns_file(room, "other.mp4"))
        self.assertFalse(watchtogether._room_owns_file(room, "hls/other/s000001.ts"))
        self.assertFalse(
            watchtogether._room_owns_file(room, "hls/abc/../../other.mp4")
        )

    def test_invitation_links_keep_credentials_out_of_query_string(self):
        room_id = "w987654321"
        try:
            with mock.patch.object(watchtogether, "_save_tokens"):
                link = watchtogether.get_room_link(987654321, "Test", "watch")
            self.assertIn(f"?room={room_id}#token=", link)
            self.assertNotIn("&token=", link)
        finally:
            for token, info in list(watchtogether.tokens.items()):
                if info.get("room") == room_id:
                    watchtogether.tokens.pop(token, None)


class SessionExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_link_exchanges_for_httponly_room_cookie(self):
        token = "watch-session-test"
        room_id = "w-session-test"
        watchtogether.tokens[token] = {
            "room": room_id, "name": "Test", "created_at": watchtogether.time.time()
        }
        try:
            response = await watchtogether.api_session(
                JsonRequest({"room": room_id, "token": token})
            )
            self.assertEqual(response.status, 200)
            name = watchtogether._room_cookie_name(room_id)
            member_name = watchtogether._room_member_cookie_name(room_id)
            self.assertIn(name, response.cookies)
            self.assertTrue(response.cookies[name]["httponly"])
            self.assertIn(member_name, response.cookies)
            self.assertTrue(response.cookies[member_name]["httponly"])
        finally:
            watchtogether.tokens.pop(token, None)

    async def test_dashboard_password_exchanges_for_opaque_cookie(self):
        ui = webui.WebUI(None)
        try:
            with mock.patch.object(webui.config, "WEB_UI_PASSWORD", "correct-horse"), \
                    mock.patch("webui._save_tokens"):
                response = await ui.api_session(
                    JsonRequest({"password": "correct-horse"})
                )
            self.assertEqual(response.status, 200)
            self.assertIn("mb_session", response.cookies)
            self.assertTrue(response.cookies["mb_session"]["httponly"])
        finally:
            for token, info in list(webui.temp_tokens.items()):
                if info.get("kind") == "session":
                    webui.temp_tokens.pop(token, None)


if __name__ == "__main__":
    unittest.main()
