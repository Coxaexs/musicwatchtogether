import asyncio
import unittest
from array import array
from collections import deque
from types import SimpleNamespace
from unittest import mock

import music


class FakeSource:
    def __init__(self, value=1000, fail=False):
        self.value = value
        self.fail = fail
        self.cleaned = False

    def read(self):
        if self.fail:
            raise AttributeError("source already cleaned")
        return array('h', [self.value] * 960 * 2).tobytes()

    def cleanup(self):
        self.cleaned = True


class AutoMixTests(unittest.TestCase):
    def test_tempo_estimator_exposes_beat_phase(self):
        rate = music.AUTOMIX_ANALYSIS_RATE
        samples = array('h', [0] * (rate * 12))
        for start in range(0, len(samples), rate // 2):  # 120 BPM pulses
            for index in range(start, min(start + 180, len(samples))):
                samples[index] = 22000
        bpm, confidence, phase = music._estimate_bpm(
            samples.tobytes(), include_phase=True)
        self.assertIsNotNone(bpm)
        self.assertGreater(confidence, 0)
        self.assertIsNotNone(phase)

    def test_transition_survives_outgoing_cleanup_and_releases_tempo(self):
        broken = music.AutoMixTransition(FakeSource(fail=True), FakeSource(850), 1)
        self.assertTrue(broken.read())

        released = []
        outgoing = FakeSource(1000)
        incoming = FakeSource(900)
        continuation = FakeSource(700)
        transition = music.AutoMixTransition(
            outgoing, incoming, 0.02, continuation=continuation,
            on_release=lambda: released.append(True))

        first = transition.read()
        second = transition.read()

        self.assertTrue(first)
        self.assertEqual(array('h', second)[0], 700)
        self.assertTrue(incoming.cleaned)
        self.assertEqual(released, [True])


class AutoplayScoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_candidate_does_not_displace_human_queue(self):
        guild = SimpleNamespace(id=7, voice_client=None)
        bot = SimpleNamespace(loop=asyncio.get_running_loop(), get_cog=lambda _name: None)
        player = music.MusicPlayer(bot, guild)
        current = music.Song('Current', 'current', '3:00', None, 'local')
        prepared = music.Song('Prepared', 'prepared', '3:00', None, 'local',
                              autoplay=True)
        human = music.Song('Human request', 'human', '3:00', None, 'local')
        player.current = current
        player.current_song_key = 'current-key'
        player.autoplay = True
        player._autoplay_candidate = prepared
        player._autoplay_candidate_for_key = 'current-key'
        player.queue.append(human)

        result = await player._ensure_autoplay_queued(current, 'current-key')

        self.assertIs(result, human)
        self.assertEqual(list(player.queue), [human])

    def test_weighted_order_prefers_score_without_becoming_deterministic_api(self):
        candidates = [{'score': 20, 'name': 'low'}, {'score': 100, 'name': 'high'}]
        with mock.patch('music.random.choices',
                        side_effect=lambda population, weights, k: [
                            population[weights.index(max(weights))]
                        ]):
            ordered = music.MusicCog._weighted_candidate_order(candidates)
        self.assertEqual(ordered[0]['name'], 'high')

    async def test_youtube_validation_rejects_karaoke_for_official_match(self):
        cog = object.__new__(music.MusicCog)
        cog.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        results = {'entries': [
            {'title': 'Fire Karaoke Cover', 'uploader': 'Karaoke World',
             'duration': 200, 'webpage_url': 'https://youtu.be/badbadbad00'},
            {'title': 'Raevin - Fire (Official Audio)', 'artist': 'Raevin',
             'duration': 202, 'webpage_url': 'https://youtu.be/goodgood000'},
        ]}
        with mock.patch.object(music.ytdl_search, 'extract_info', return_value=results):
            song = await cog._resolve_autoplay_match(
                {'artist': 'Raevin', 'title': 'Fire', 'duration': 202},
                SimpleNamespace())
        self.assertEqual(song.url, 'https://youtu.be/goodgood000')
        self.assertTrue(song.autoplay)

    async def test_unified_pool_returns_explainable_related_pick(self):
        cog = object.__new__(music.MusicCog)
        cog.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        cog._audio_features = {}
        cog._transition_feedback = {}
        seed = music.Song('Seed', 'seed-url', '3:00', None, 'youtube',
                          artist='Seed Artist')
        guild = SimpleNamespace(id=9, me=SimpleNamespace(), voice_client=None)
        player = SimpleNamespace(
            current=seed, history=deque([seed]), guild=guild,
            vibe_match=True, artist_diversity=True,
            _resolve_local_file=lambda _song: None,
        )
        station = [{'artist': 'Related Artist', 'title': 'Next Track',
                    'album': 'Next Album', 'duration': 190}]
        resolved = music.Song('Next Track', 'next-url', '3:10', guild.me, 'youtube',
                              artist='Related Artist', autoplay=True)
        with mock.patch.object(cog, '_autoplay_station', new=mock.AsyncMock(return_value=station)), \
                mock.patch.object(cog, '_dislike_index', return_value=(set(), set(), {})), \
                mock.patch.object(cog, '_favorite_index', return_value=(set(), set(), set())), \
                mock.patch.object(cog, '_resolve_autoplay_match',
                                  new=mock.AsyncMock(return_value=resolved)), \
                mock.patch('music.list_library_files', return_value=[]):
            song = await cog.pick_autoplay_recommendation(player)
        self.assertIs(song, resolved)
        self.assertIsNotNone(song.autoplay_score)
        self.assertIn('related artist', song.autoplay_reason)

    async def test_early_skip_learns_transition_not_song_dislike(self):
        cog = object.__new__(music.MusicCog)
        cog.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        cog._transition_feedback = {}
        previous = music.Song('Previous', '/previous.mp3', '3:00', None, 'local',
                              artist='Artist A')
        current = music.Song('Current', '/current.mp3', '3:20', None, 'local',
                             artist='Artist B', autoplay=True)
        profiles = {
            '/previous.mp3': {'energy': .8, 'bpm': 128, 'genre': 'dance'},
            '/current.mp3': {'energy': .3, 'bpm': 78, 'genre': 'acoustic'},
        }
        cog.get_local_features = lambda path: profiles.get(path)
        player = SimpleNamespace(
            current=current, history=deque([current, previous]),
            guild=SimpleNamespace(id=42),
            get_playback_position_seconds=lambda: 18,
            _resolve_local_file=lambda song: song.url,
        )
        with mock.patch.object(music.state_store, 'save'), \
                mock.patch('music.save_json'), \
                mock.patch.object(cog, '_write_dislikes') as write_dislikes:
            await cog.record_autoplay_transition(player, 'skip')

        records = cog._transition_feedback['42']
        self.assertGreater(records['artist:change']['bad'], 0)
        self.assertGreater(records['energy:down']['bad'], 0)
        write_dislikes.assert_not_called()


if __name__ == '__main__':
    unittest.main()
