"""Hoffle rooms playing the files we already have instead of streaming YouTube."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import huddle  # noqa: E402
import huddle_voice  # noqa: E402
import music  # noqa: E402
import webui  # noqa: E402
from huddle_voice import HuddleVoiceManager, RoomAudioTrack  # noqa: E402

LINK = "https://www.youtube.com/watch?v=lyn2QKgM7tI"
STREAM = "https://rr1.googlevideo.com/videoplayback?id=1"
FILE = "/music/paga play/Redd - Kaniyorduk - [lyn2QKgM7tI].mp3"


def track(track_id="t1", audio=STREAM, page=LINK, **more):
    return {"id": track_id, "title": "Song", "audioUrl": audio, "pageUrl": page, "duration": 200, **more}


def manager(files=None):
    calls = []
    files = {LINK: FILE} if files is None else files

    def download_of(link):
        calls.append(link)
        return files.get(link)

    room_manager = HuddleVoiceManager(download_of=download_of)
    room_manager.lookups = calls
    room_manager.files = files
    return room_manager


class OpeningAFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_ffmpeg_really_plays_a_file_with_the_rooms_command(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "song.mp3")
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", path], check=True)
            process = await RoomAudioTrack._spawn(path, 1.0, ["volume=1"])
            audio, _ = await process.communicate()
        self.assertGreater(len(audio), 48_000 * 4)  # at least a second of it
        self.assertTrue(any(audio), "the file played as silence")

    async def test_a_stream_keeps_reconnecting_and_a_file_does_not_try(self):
        with mock.patch("asyncio.create_subprocess_exec", new=mock.AsyncMock()) as spawn:
            await RoomAudioTrack._spawn(STREAM, 0, ["volume=1"])
            await RoomAudioTrack._spawn(FILE, 0, ["volume=1"])
        stream, local = (call.args for call in spawn.await_args_list)
        self.assertIn("-reconnect", stream)
        self.assertNotIn("-reconnect", local)


class UsingDownloadsTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_downloaded_song_plays_from_its_file(self):
        player = {"track": track(), "queue": []}
        await manager()._use_downloads(player)
        self.assertEqual(player["track"]["audioUrl"], FILE)

    async def test_songs_in_the_queue_too_so_automix_sees_the_same_source(self):
        player = {"track": track("t0", page="https://other"), "queue": [track()]}
        await manager()._use_downloads(player)
        self.assertEqual(player["queue"][0]["audioUrl"], FILE)

    async def test_a_song_we_do_not_have_keeps_its_stream(self):
        player = {"track": track(page="https://www.youtube.com/watch?v=aaaaaaaaaaa"), "queue": []}
        await manager()._use_downloads(player)
        self.assertEqual(player["track"]["audioUrl"], STREAM)

    async def test_hoffle_library_marker_becomes_the_file(self):
        player = {"track": track(audio="library:lyn2QKgM7tI"), "queue": []}
        await manager()._use_downloads(player)
        self.assertEqual(player["track"]["audioUrl"], FILE)

    async def test_if_the_file_has_gone_the_song_is_looked_up_again(self):
        player = {"track": track(audio="library:lyn2QKgM7tI", query=LINK), "queue": []}
        room_manager = manager({})
        await room_manager._use_downloads(player)
        self.assertEqual(player["track"]["audioUrl"], "")
        with mock.patch.object(huddle, "resolve", new=mock.AsyncMock()) as resolve:
            room_manager._resolve_upcoming("room", player)
            await asyncio.gather(*room_manager.resolve_tasks.values())
        resolve.assert_awaited_once_with("room", "t1")

    async def test_a_placeholder_waits_for_hoffle(self):
        player = {"track": track(audio="", query=LINK), "queue": []}
        room_manager = manager()
        await room_manager._use_downloads(player)
        self.assertEqual(player["track"]["audioUrl"], "")
        self.assertEqual(room_manager.lookups, [])

    async def test_each_song_is_looked_up_once(self):
        room_manager = manager()
        for _ in range(5):
            await room_manager._use_downloads({"track": track(), "queue": []})
        self.assertEqual(room_manager.lookups, [LINK])

    async def test_a_download_finishing_mid_song_does_not_restart_it(self):
        room_manager = manager({})
        await room_manager._use_downloads({"track": track(), "queue": []})
        room_manager.files[LINK] = FILE
        player = {"track": track(), "queue": []}
        await room_manager._use_downloads(player)
        self.assertEqual(player["track"]["audioUrl"], STREAM)

    async def test_the_same_source_comes_back_every_poll(self):
        room_manager = manager()
        sources = []
        for _ in range(3):
            player = {"track": track(), "queue": []}  # the hub sends a fresh copy each time
            await room_manager._use_downloads(player)
            sources.append(player["track"]["audioUrl"])
        self.assertEqual(sources, [FILE, FILE, FILE])

    async def test_a_failed_lookup_leaves_the_stream_playing(self):
        room_manager = HuddleVoiceManager(download_of=mock.Mock(side_effect=OSError("disk")))
        player = {"track": track(), "queue": []}
        await room_manager._use_downloads(player)
        self.assertEqual(player["track"]["audioUrl"], STREAM)

    async def test_the_lookup_stays_off_the_event_loop(self):
        seen = []

        def download_of(_link):
            try:
                asyncio.get_running_loop()
                seen.append("on the loop")
            except RuntimeError:
                seen.append("off the loop")

        await HuddleVoiceManager(download_of=download_of)._use_downloads({"track": track(), "queue": []})
        self.assertEqual(seen, ["off the loop"])

    async def test_it_works_with_no_lookup_wired_up(self):
        player = {"track": track(), "queue": []}
        await HuddleVoiceManager()._use_downloads(player)
        self.assertEqual(player["track"]["audioUrl"], STREAM)

    async def test_the_whole_poll_hands_the_player_and_the_dj_booth_the_file(self):
        room_manager = manager()
        publisher = mock.Mock(update=mock.AsyncMock())
        data = {"servers": [{"voiceChannels": [{"id": "room", "player": {"track": track(), "queue": []}}]}]}
        with mock.patch.object(huddle_voice, "RoomPublisher", return_value=publisher), \
                mock.patch.object(huddle, "record_play"), \
                mock.patch.object(huddle, "settings_for", return_value={}), \
                mock.patch.object(dj, "sessions", {}):
            await room_manager._sync(data)
        self.assertEqual(publisher.update.await_args.args[0]["track"]["audioUrl"], FILE)
        self.assertEqual(room_manager.players["room"]["track"]["audioUrl"], FILE)


class NeverLeavingThePathTests(unittest.IsolatedAsyncioTestCase):
    async def test_hoffle_gets_its_own_value_back(self):
        player = {"track": track(), "queue": []}
        await manager()._use_downloads(player)
        known = huddle.as_hub_knows_it(player["track"])
        self.assertEqual(known["audioUrl"], STREAM)
        self.assertNotIn("hubAudioUrl", known)
        self.assertNotIn(FILE, json.dumps(known))

    def test_a_track_we_never_changed_goes_back_as_it_was(self):
        self.assertEqual(huddle.as_hub_knows_it(track()), track())


def song(url, source_type="youtube", thumbnail=None, title="Song", artist="Artist", duration="3:20"):
    return SimpleNamespace(url=url, source_type=source_type, thumbnail=thumbnail, title=title, artist=artist, duration=duration)


class ResolveRouteTests(unittest.IsolatedAsyncioTestCase):
    def ui(self, *, by_name=None, by_link=None, searched=None, have=()):
        cog = SimpleNamespace(
            _local_song_for_query=mock.Mock(return_value=by_name),
            _download_for_link=mock.Mock(return_value=by_link),
            process_youtube=mock.AsyncMock(return_value=searched),
            downloaded_video_id=lambda url: url[-11:] if url in have else None,
        )
        bot = SimpleNamespace(guilds=[SimpleNamespace(me=SimpleNamespace())], loop=asyncio.get_running_loop())
        return SimpleNamespace(bot=bot, cog=cog)

    async def resolve(self, ui, query):
        request = SimpleNamespace(json=mock.AsyncMock(return_value={"query": query}))
        response = await webui.WebUI.api_huddle_resolve(ui, request)
        return json.loads(response.text)["tracks"][0]

    async def test_a_link_we_have_never_asks_youtube(self):
        ui = self.ui(by_link=song(FILE, "local"))
        answer = await self.resolve(ui, LINK)
        ui.cog.process_youtube.assert_not_awaited()
        self.assertEqual(answer["library"], "lyn2QKgM7tI")
        self.assertEqual(answer["page_url"], LINK)
        self.assertEqual(answer["duration"], 200)

    async def test_the_file_path_never_reaches_hoffle(self):
        answer = await self.resolve(self.ui(by_link=song(FILE, "local")), LINK)
        self.assertNotIn("/music/", json.dumps(answer))

    async def test_a_library_song_gets_a_cover(self):
        answer = await self.resolve(self.ui(by_link=song(FILE, "local")), LINK)
        self.assertEqual(answer["thumbnail"], "https://i.ytimg.com/vi/lyn2QKgM7tI/hqdefault.jpg")

    async def test_an_exact_title_from_the_library_works_too(self):
        answer = await self.resolve(self.ui(by_name=song(FILE, "local")), "Kaniyorduk")
        self.assertEqual(answer["library"], "lyn2QKgM7tI")

    async def test_a_search_that_lands_on_a_song_we_have_needs_no_stream(self):
        answer = await self.resolve(self.ui(searched=song(LINK, thumbnail="https://cover"), have=[LINK]), "redd kaniyorduk")
        self.assertEqual(answer["library"], "lyn2QKgM7tI")
        self.assertEqual(answer["thumbnail"], "https://cover")

    async def test_a_song_we_do_not_have_goes_to_youtube_as_before(self):
        other = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        answer = await self.resolve(self.ui(searched=song(other)), other)
        self.assertIsNone(answer["library"])
        self.assertEqual(answer["resolve_query"], other)

    async def test_a_lazy_spotify_entry_still_searches_by_text(self):
        answer = await self.resolve(self.ui(searched=song("spotify:search:redd kaniyorduk")), "redd kaniyorduk")
        self.assertIsNone(answer["library"])
        self.assertEqual(answer["resolve_query"], "redd kaniyorduk")


class LibraryLookupTests(unittest.TestCase):
    def setUp(self):
        self.cog = object.__new__(music.MusicCog)

    def test_a_link_finds_its_file_with_its_real_name_and_length(self):
        with mock.patch("music.find_cached_song", return_value=FILE) as find, \
                mock.patch("music._probe_duration_seconds", return_value=200):
            found = self.cog._download_for_link(LINK, None)
        find.assert_called_once_with("lyn2QKgM7tI")
        self.assertEqual((found.url, found.source_type), (FILE, "local"))
        self.assertEqual(found.duration, "3:20")

    def test_plain_text_is_not_a_link(self):
        with mock.patch("music.find_cached_song") as find:
            self.assertIsNone(self.cog._download_for_link("redd kaniyorduk", None))
        find.assert_not_called()

    def test_a_link_we_do_not_have(self):
        with mock.patch("music.find_cached_song", return_value=None):
            self.assertIsNone(self.cog._download_for_link(LINK, None))

    def test_filenames(self):
        self.assertEqual(huddle.library_video_id(FILE), "lyn2QKgM7tI")
        self.assertEqual(huddle.library_video_id("/a/ytdl_-ixXF3l96vo.mp3"), "-ixXF3l96vo")
        self.assertIsNone(huddle.library_video_id("/a/my rip.mp3"))


if __name__ == "__main__":
    unittest.main()
