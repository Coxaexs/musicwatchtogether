import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import dj
import songlib
import webui


def handler_class():
    return next(c for c in vars(webui).values() if isinstance(c, type) and hasattr(c, "api_library_delete"))


class Request:
    def __init__(self, body=None, query=None):
        self._body, self.query = body, query or {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def call(name, request):
    ui = SimpleNamespace()
    response = asyncio.run(getattr(handler_class(), name)(ui, request))
    return response.status, json.loads(response.text)


class LibraryApiTests(unittest.TestCase):
    def test_search_passes_the_query_through(self):
        with mock.patch.object(songlib, "search", return_value=[{"id": "x"}]) as search:
            status, body = call("api_library_songs", Request(query={"q": "  duman "}))
        search.assert_called_once_with("duman")
        self.assertEqual((status, body), (200, {"songs": [{"id": "x"}]}))

    def test_delete_success_and_error_codes(self):
        cases = [
            (ValueError("nope"), 400),
            (LookupError("gone"), 404),
            (RuntimeError("disk"), 500),
        ]
        with mock.patch.object(songlib, "delete", return_value={"id": "aBcDeFgHiJk", "files": ["a.mp3"]}):
            status, body = call("api_library_delete", Request({"id": "aBcDeFgHiJk"}))
        self.assertEqual((status, body["files"]), (200, ["a.mp3"]))
        for error, code in cases:
            with mock.patch.object(songlib, "delete", side_effect=error):
                self.assertEqual(call("api_library_delete", Request({"id": "aBcDeFgHiJk"}))[0], code)

    def test_missing_body_is_a_400_not_a_crash(self):
        self.assertEqual(call("api_library_delete", Request(None))[0], 400)

    def test_a_song_playing_in_the_booth_is_protected(self):
        deck = SimpleNamespace(playing=True, info={"keys": ["yt:aBcDeFgHiJk"]})
        session = SimpleNamespace(engine=SimpleNamespace(decks={"A": deck}))
        with mock.patch.dict(dj.sessions, {"room": session}, clear=True), \
                mock.patch.object(songlib, "delete", side_effect=AssertionError("must not delete")):
            status, body = call("api_library_delete", Request({"id": "aBcDeFgHiJk"}))
        self.assertEqual(status, 409)
        deck.playing = False                                    # a loaded but silent deck does not block
        with mock.patch.dict(dj.sessions, {"room": session}, clear=True), \
                mock.patch.object(songlib, "delete", return_value={"id": "aBcDeFgHiJk"}):
            self.assertEqual(call("api_library_delete", Request({"id": "aBcDeFgHiJk"}))[0], 200)


if __name__ == "__main__":
    unittest.main()
