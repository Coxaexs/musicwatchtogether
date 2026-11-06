"""A room moving onto a new karaoke track the moment its split finishes."""
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import huddle  # noqa: E402
import huddle_voice  # noqa: E402
import stems  # noqa: E402

SONG = "/music/song.mp3"
BACKING = "/stems/abc.inst.flac"


def song(track_id="t1"):
    return {"id": track_id, "title": "Song", "audioUrl": SONG, "pageUrl": "https://youtu.be/x", "duration": 200}


def room(track, position=50_000, paused=False, ago=20):
    return {"track": track, "queue": [], "paused": paused, "positionMs": position, "updatedAt": (time.time() - ago) * 1000, "volume": 100}


class KaraokeSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pub = object.__new__(huddle_voice.RoomPublisher)
        self.pub.channel_id = "room"
        self.pub.state_key = ("t1", False, 50_000, 0, (), SONG)
        self.pub.source = SimpleNamespace(dj=None, track_id="t1", analysis=None, mix_info=None, configure=mock.AsyncMock(), set_volume=mock.Mock())
        self.pub.lights = SimpleNamespace(info=None)
        self.pub._analysis_for = "t1"
        self.pub.sung_for = {}
        self.pub._sing_along = mock.Mock()
        self.pub._maybe_plan = mock.Mock()
        self.pub._karaoke = mock.Mock(return_value=BACKING)
        self.pub.last_player = room(song())
        for patch in (
            mock.patch.object(dj, "sessions", {}),
            mock.patch.object(huddle, "settings_for", return_value={"karaoke_mode": True, "audio_filter": "karaoke"}),
            mock.patch.object(stems, "prepare", new=mock.AsyncMock(return_value="abc")),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def test_the_room_moves_onto_the_backing_track_when_the_split_finishes(self):
        await self.pub._prepare_karaoke(song())
        self.pub.source.configure.assert_awaited_once()
        self.assertEqual(self.pub.source.configure.await_args.args[0], BACKING)

    async def test_it_carries_on_from_the_same_place_in_the_song(self):
        await self.pub._prepare_karaoke(song())
        self.assertAlmostEqual(self.pub.source.configure.await_args.args[1], 70, delta=1)

    async def test_the_phase_cancel_filter_goes_with_it(self):
        await self.pub._prepare_karaoke(song())
        self.assertIsNone(self.pub.source.configure.await_args.args[5]["audio_filter"])

    async def test_a_paused_room_stays_paused(self):
        self.pub.last_player = room(song(), paused=True)
        await self.pub._prepare_karaoke(song())
        self.assertTrue(self.pub.source.configure.await_args.args[2])

    async def test_a_split_for_another_song_leaves_this_one_alone(self):
        self.pub.last_player = room(song("t2"))
        await self.pub._prepare_karaoke(song("t1"))
        self.pub.source.configure.assert_not_awaited()

    async def test_the_booth_keeps_the_room(self):
        self.pub.source.dj = object()
        await self.pub._prepare_karaoke(song())
        self.pub.source.configure.assert_not_awaited()

    async def test_automix_handing_over_still_does_not_restart_the_song(self):
        """The hub catching up: the mix already moved the source onto the new song."""
        self.pub._karaoke = mock.Mock(return_value=None)
        self.pub.state_key = ("old", False, 0, 0, (), "/music/old.mp3")
        await self.pub.update(room(song("t1")))
        self.pub.source.configure.assert_not_awaited()

    async def test_a_new_song_still_starts_normally(self):
        self.pub._karaoke = mock.Mock(return_value=None)
        self.pub.source.track_id = "old"
        self.pub.state_key = ("old", False, 0, 0, (), "/music/old.mp3")
        await self.pub.update(room(song("t1")))
        self.pub.source.configure.assert_awaited_once()

    async def test_coming_back_from_the_booth_picks_the_room_up(self):
        self.pub._karaoke = mock.Mock(return_value=None)
        self.pub.state_key = None
        self.pub.source.track_id = None
        await self.pub.update(room(song("t1")))
        self.pub.source.configure.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
