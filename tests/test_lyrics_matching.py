import unittest
from unittest import mock

from music import MusicCog, Song


class LyricsMatchingTests(unittest.TestCase):
    def setUp(self):
        self.cog = MusicCog.__new__(MusicCog)

    def test_artist_is_removed_from_track_but_kept_as_context(self):
        track, artist = self.cog._lyrics_search_context(
            "Adele - Hello (Official Video)", "Adele")
        self.assertEqual(track, "Hello")
        self.assertEqual(artist, "Adele")

    def test_artist_matching_rejects_same_title_from_another_artist(self):
        self.assertTrue(self.cog._lyrics_artist_matches("Adele", "Adele"))
        self.assertFalse(self.cog._lyrics_artist_matches("Adele", "Lionel Richie"))

    def test_title_artist_beats_unrelated_upload_channel(self):
        song = Song("Adele - Hello", "url", "4:55", None, "youtube",
                    artist="Music Label Channel")
        self.assertEqual(self.cog._lyrics_artist_for_song(song), "Adele")

    def test_artist_aware_cache_keys_do_not_collide(self):
        adele = self.cog._get_lyrics_cache_path("Hello", "Adele")
        lionel = self.cog._get_lyrics_cache_path("Hello", "Lionel Richie")
        self.assertNotEqual(adele, lionel)

    def test_duplicate_artists_are_case_insensitive_and_keep_first_spelling(self):
        artists = self.cog._dedupe_artist_names(
            "mor ve ötesi, mor ve ötesi, Mor ve Ötesi, Mor ve Ötesi, Tarkan Gözübüyük")
        self.assertEqual(artists, ["mor ve ötesi", "Tarkan Gözübüyük"])


class LyricsArtistFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_synced_lyrics_tries_unique_artists_one_at_a_time(self):
        cog = MusicCog.__new__(MusicCog)
        song = Song(
            ("Bir Derdim Var — mor ve ötesi, mor ve ötesi, Mor ve Ötesi, "
             "Mor ve Ötesi, Tarkan Gözübüyük"),
            "spotify:search:Bir Derdim Var", "4:10", None, "spotify",
            artist=("mor ve ötesi, mor ve ötesi, Mor ve Ötesi, "
                    "Mor ve Ötesi, Tarkan Gözübüyük"),
        )
        found = {"track": "Bir Derdim Var", "artist": "Tarkan Gözübüyük", "lines": []}
        with mock.patch.object(
                cog, "_fetch_synced_lyrics", new=mock.AsyncMock(
                    side_effect=[None, found])) as fetch:
            result = await cog._fetch_synced_lyrics_for_song(song)

        self.assertIs(result, found)
        self.assertEqual(
            [call.args[1] for call in fetch.await_args_list],
            ["mor ve ötesi", "Tarkan Gözübüyük"],
        )
        self.assertEqual([call.args[0] for call in fetch.await_args_list],
                         ["Bir Derdim Var", "Bir Derdim Var"])


if __name__ == "__main__":
    unittest.main()
