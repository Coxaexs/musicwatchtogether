"""Smart Autoplay in Hoffle rooms: what it asks Hoffle for, and how hard it tries."""
import asyncio
import os
import sys
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import huddle  # noqa: E402
import huddle_voice  # noqa: E402
from huddle_voice import AUTOPLAY_TRIES, HuddleVoiceManager, hoffle_query  # noqa: E402


def local(name, title="Kaniyorduk", artist="paga play"):
    return SimpleNamespace(source_type="local", url=f"/music/paga play/{name}", title=title, artist=artist)


class HoffleQueryTests(unittest.TestCase):
    def test_a_library_song_goes_by_the_youtube_id_in_its_filename(self):
        self.assertEqual(hoffle_query(local("Redd - Kaniyorduk - [lyn2QKgM7tI].mp3")), "https://www.youtube.com/watch?v=lyn2QKgM7tI")
        self.assertEqual(hoffle_query(local("ytdl_-ixXF3l96vo.mp3")), "https://www.youtube.com/watch?v=-ixXF3l96vo")

    def test_a_library_song_without_an_id_goes_by_name(self):
        self.assertEqual(hoffle_query(local("my rip.mp3")), "paga play Kaniyorduk")
        self.assertEqual(hoffle_query(local("my rip.mp3", artist=None)), "Kaniyorduk")

    def test_brackets_in_a_title_are_not_an_id(self):
        self.assertEqual(hoffle_query(local("[Official Video] song.mp3", title="song", artist=None)), "song")

    def test_anything_else_keeps_its_url(self):
        song = SimpleNamespace(source_type="youtube", url="https://youtu.be/x", title="t", artist="a")
        self.assertEqual(hoffle_query(song), "https://youtu.be/x")


def room(track_id="song-1", left=30):
    return {
        "track": {"id": track_id, "title": "Now", "duration": 200, "pageUrl": "https://youtu.be/now"},
        "queue": [], "paused": True, "positionMs": (200 - left) * 1000, "updatedAt": 0,
    }


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.picks = [local(f"Song {n} - [aaaaaaaaaa{n}].mp3", title=f"Song {n}") for n in range(9)]
        self.asked_to_skip = []

        async def pick(_player, skip=()):
            self.asked_to_skip.append(list(skip))
            return self.picks[len(self.asked_to_skip) - 1]

        cog = mock.Mock()
        cog.get_player.return_value = SimpleNamespace(current=None, history=deque(), autoplay=False, artist_diversity=False, vibe_match=False)
        cog.pick_autoplay_recommendation = pick
        self.manager = HuddleVoiceManager(cog=cog, song_class=lambda **song: SimpleNamespace(**song))
        self.play = mock.AsyncMock(side_effect=RuntimeError("The result has no playable audio stream."))
        self.radio = mock.AsyncMock(return_value=None)
        self.settings = {"autoplay": True}
        for patch in (
            mock.patch.object(huddle, "play", self.play),
            mock.patch.object(huddle, "radio_next", self.radio),
            mock.patch.object(huddle, "record_play"),
            mock.patch.object(huddle, "settings_for", lambda _channel: self.settings),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def poll(self, player=None, times=1, wait_over=False):
        """The bot looks at each room every few seconds; `wait_over` skips the backoff."""
        for _ in range(times):
            if wait_over and "afk" in self.manager.autoplay_retries:
                self.manager.autoplay_retries["afk"]["at"] = 0
            await self.manager._observe_room("afk", player or room())
            task = self.manager.autoplay_tasks.get("afk")
            if task:
                await task

    async def test_a_library_pick_is_sent_as_a_youtube_link(self):
        self.play.side_effect = None
        await self.poll()
        self.assertEqual(self.play.await_args.args[1], "https://www.youtube.com/watch?v=aaaaaaaaaa0")
        self.assertEqual(self.manager.autoplay_retries, {})

    async def test_it_waits_after_a_failure_instead_of_trying_on_every_poll(self):
        await self.poll(times=50)
        self.assertEqual(self.play.await_count, 1)

    async def test_it_gives_up_on_a_song_after_a_few_tries(self):
        await self.poll(times=50, wait_over=True)
        self.assertEqual(self.play.await_count, AUTOPLAY_TRIES)

    async def test_waits_grow_and_all_fit_before_the_song_ends(self):
        self.assertEqual(len(huddle_voice.AUTOPLAY_WAITS), AUTOPLAY_TRIES - 1)
        self.assertEqual(list(huddle_voice.AUTOPLAY_WAITS), sorted(huddle_voice.AUTOPLAY_WAITS))
        self.assertLess(sum(huddle_voice.AUTOPLAY_WAITS), 75)

    async def test_a_new_song_gets_fresh_tries(self):
        await self.poll(times=50, wait_over=True)
        await self.poll(room("song-2"), times=50, wait_over=True)
        self.assertEqual(self.play.await_count, AUTOPLAY_TRIES * 2)

    async def test_the_next_try_is_told_what_failed(self):
        await self.poll(times=3, wait_over=True)
        self.assertEqual(self.asked_to_skip[0], [])
        self.assertEqual(self.asked_to_skip[1], [self.picks[0]])
        self.assertEqual(self.asked_to_skip[2], [self.picks[0], self.picks[1]])

    async def test_a_try_that_works_clears_the_count(self):
        await self.poll()
        self.play.side_effect = None
        await self.poll(wait_over=True)
        self.assertEqual(self.manager.autoplay_retries, {})

    async def test_finding_nothing_also_waits(self):
        self.manager.cog.pick_autoplay_recommendation = mock.AsyncMock(return_value=None)
        await self.poll(times=50)
        self.assertEqual(self.manager.cog.pick_autoplay_recommendation.await_count, 1)

    async def test_the_radio_waits_too(self):
        self.settings = {"radio": "chill"}
        with mock.patch.object(huddle_voice.dj, "sessions", {}):
            await self.poll(times=50)
            self.assertEqual(self.radio.await_count, 1)
            self.radio.side_effect = RuntimeError("no stream")
            await self.poll(times=50, wait_over=True)
        self.assertEqual(self.radio.await_count, AUTOPLAY_TRIES)

    async def test_it_leaves_a_room_alone_until_the_song_is_ending(self):
        await self.poll(room(left=120), times=5)
        self.assertEqual(self.play.await_count, 0)


if __name__ == "__main__":
    unittest.main()
