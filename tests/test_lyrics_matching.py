import unittest

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


if __name__ == "__main__":
    unittest.main()
