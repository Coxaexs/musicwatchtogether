import asyncio
import os
import subprocess
import tempfile
import unittest

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

import dj
import huddle_voice


def synth_track(path, bpm=124.0, seconds=40):
    """Kick on every beat + hat on the off-beat + an A-minor pad."""
    beat = 60.0 / bpm
    expr = (
        f"0.8*sin(2*PI*55*t)*exp(-mod(t,{beat})*18)"
        f"+0.15*(random(0)-0.5)*exp(-mod(t+{beat / 2},{beat})*60)"
        "+0.08*(sin(2*PI*220*t)+sin(2*PI*261.63*t)+sin(2*PI*329.63*t))"
    )
    subprocess.run([
        'ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
        '-i', f"aevalsrc='{expr}':s=44100:d={seconds}", '-ac', '2', path,
    ], check=True)


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.wav = os.path.join(cls.dir.name, 'beat.wav')
        synth_track(cls.wav)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_tempo_grid_and_peaks(self):
        result = dj.analyse_file(self.wav)
        self.assertAlmostEqual(result['bpm'], 124.0, delta=1.0)
        self.assertIsNotNone(result['beat_offset'])
        # The kick lands at t=0, so the grid must sit on a multiple of the beat.
        beat = 60.0 / result['bpm']
        phase = result['beat_offset'] % beat
        self.assertLess(min(phase, beat - phase), 0.04)
        self.assertAlmostEqual(result['duration'], 40.0, delta=0.2)
        self.assertEqual(len(result['peaks']) % 3, 0)
        self.assertAlmostEqual(len(result['peaks']) / 3 / dj.PEAK_RATE, 40.0, delta=0.5)
        self.assertLess(result['audible_start'], 0.2)

    def test_transcode_is_browser_aac(self):
        target = os.path.join(self.dir.name, 'full.m4a')
        dj.transcode(self.wav, target)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                '-show_entries', 'stream=codec_name,sample_rate,channels',
                                '-of', 'csv=p=0', target], capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), 'aac,44100,2')


class LibraryTests(unittest.TestCase):
    def test_ids_are_stable_and_case_insensitive_for_searches(self):
        self.assertEqual(dj.track_id_for('search', 'Daft Punk One More Time'),
                         dj.track_id_for('search', 'daft punk one more time '))
        self.assertNotEqual(dj.track_id_for('url', 'https://x/a'), dj.track_id_for('url', 'https://x/b'))

    def test_add_track_dedupes_and_crates(self):
        with tempfile.TemporaryDirectory() as folder:
            lib = dj.Library(os.path.join(folder, 'index.json'))
            a = lib.add_track(kind='search', value='song a', title='Song A')
            again = lib.add_track(kind='search', value='Song A', title='Other')
            self.assertIs(a, again)
            crate = lib.crate('Party', source='https://open.spotify.com/playlist/x')
            self.assertIs(crate, lib.crate('Renamed', source='https://open.spotify.com/playlist/x'))
            lib.save()
            reloaded = dj.Library(os.path.join(folder, 'index.json'))
            self.assertIn(a['id'], reloaded.tracks)
            self.assertNotIn('source', reloaded.public_track(a))

    def test_default_library_path_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            original = dj.INDEX_FILE
            dj.INDEX_FILE = os.path.join(folder, 'index.json')
            try:
                lib = dj.Library()
                lib.add_track(kind='search', value='persist me', title='Persist')
                lib.save()
                self.assertEqual(len(dj.Library().tracks), 1)
            finally:
                dj.INDEX_FILE = original

    def test_guess_stem_name(self):
        cases = {
            'Song (Vocals).wav': 'vocals', '03 - DRUMS.flac': 'drums', 'track_Bass_stem.mp3': 'bass',
            'Lead Synth.wav': 'other', 'Keys.aif': 'piano', 'Song (Acapella).wav': 'vocals',
            'Song (Instrumental).wav': None, 'random.wav': None, 'Perc Loop.wav': 'drums',
        }
        for filename, expected in cases.items():
            self.assertEqual(dj.guess_stem_name(filename), expected, filename)

    def test_spotify_without_keys_explains(self):
        saved = {k: os.environ.pop(k, None) for k in ('SPOTIFY_CLIENT_ID', 'SPOTIFY_CLIENT_SECRET')}
        try:
            with self.assertRaisesRegex(RuntimeError, 'SPOTIFY_CLIENT_ID'):
                dj.fetch_spotify_collection('https://open.spotify.com/playlist/abc')
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_track_dir_rejects_traversal(self):
        with self.assertRaises(ValueError):
            dj.track_dir('../../etc')


class LiveFeedTests(unittest.TestCase):
    def test_prebuffers_then_streams_exact_frames(self):
        feed = huddle_voice.LiveFeed()
        frame = huddle_voice.BYTES_PER_FRAME
        feed.push(b'\x01' * (frame * 3))
        self.assertIsNone(feed.pop_frame())  # still prebuffering
        feed.push(b'\x01' * (frame * 3))
        self.assertEqual(len(feed.pop_frame()), frame)
        for _ in range(5):
            feed.pop_frame()
        self.assertIsNone(feed.pop_frame())  # ran dry: rebuffer
        self.assertFalse(feed.started)

    def test_overflow_drops_oldest_audio(self):
        feed = huddle_voice.LiveFeed()
        frame = huddle_voice.BYTES_PER_FRAME
        feed.push(b'\x00' * (frame * 100) + b'\x02' * 6)
        self.assertLessEqual(feed.frames_buffered(), feed.MAX_FRAMES)
        self.assertEqual(len(feed.buffer) % 4, 2)  # trailing partial sample kept aligned
        self.assertTrue(feed.buffer.endswith(b'\x02' * 6))

    def test_room_track_plays_live_feed(self):
        async def run():
            track = huddle_voice.RoomAudioTrack('room-1')
            feed = huddle_voice.attach_live('room-1')
            feed.push(b'\x10\x00' * (huddle_voice.BYTES_PER_FRAME // 2) * 6)
            frame = await track.recv()
            self.assertTrue(any(bytes(frame.planes[0])))
            huddle_voice.detach_live('room-1', feed)
            self.assertNotIn('room-1', huddle_voice.LIVE_FEEDS)
            await track.shutdown()
        asyncio.run(run())


class FakeUI:
    bot = None
    cog = None

    def __init__(self, allowed=None):
        self.allowed = allowed

    def _get_allowed_guild_id(self, request):
        return self.allowed


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = {
            'DJ_DIR': dj.DJ_DIR, 'TRACKS_DIR': dj.TRACKS_DIR, 'INDEX_FILE': dj.INDEX_FILE,
            'DEMUCS_PYTHON': dj.DEMUCS_PYTHON,
        }
        dj.DJ_DIR = self.tmp.name
        dj.TRACKS_DIR = os.path.join(self.tmp.name, 'tracks')
        dj.INDEX_FILE = os.path.join(self.tmp.name, 'index.json')
        dj.DEMUCS_PYTHON = os.getenv('DJ_TEST_DEMUCS_PYTHON') or '/nonexistent/python'
        app = web.Application()
        self.service = dj.setup(app, FakeUI(allowed='huddle:room-1'))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.wav = os.path.join(self.tmp.name, 'Artist - Groove.wav')
        synth_track(self.wav, seconds=30)

    async def asyncTearDown(self):
        await self.client.close()
        for key, value in self.patches.items():
            setattr(dj, key, value)
        self.tmp.cleanup()

    async def wait_ready(self, track_id, field='job', timeout=60):
        for _ in range(int(timeout * 10)):
            track = self.service.library.tracks[track_id]
            if field == 'job' and track['job']['stage'] in ('ready', 'failed'):
                return track
            if field == 'stems' and track['stems']['status'] in ('ready', 'failed', 'none'):
                return track
            await asyncio.sleep(0.1)
        self.fail(f'{track_id} never finished: {self.service.library.tracks[track_id]}')

    async def test_page_and_static(self):
        r = await self.client.get('/dj/')
        self.assertEqual(r.status, 200)
        self.assertIn('DJ Booth', await r.text())
        r = await self.client.get('/dj/static/app.js')
        self.assertEqual(r.status, 200)
        r = await self.client.get('/dj/static/..%2Fdj.py')
        self.assertEqual(r.status, 404)

    async def test_upload_analyse_serve_and_edit(self):
        form = FormData()
        form.add_field('stems', '0')
        with open(self.wav, 'rb') as handle:
            form.add_field('file', handle.read(), filename='Artist - Groove.wav')
        r = await self.client.post('/api/dj/upload', data=form)
        self.assertEqual(r.status, 200, await r.text())
        track_id = (await r.json())['added'][0]
        track = await self.wait_ready(track_id)
        self.assertEqual(track['job']['stage'], 'ready', track['job'])
        self.assertEqual((track['title'], track['artist']), ('Groove', 'Artist'))
        self.assertAlmostEqual(track['analysis']['bpm'], 124, delta=1)

        lib = await (await self.client.get('/api/dj/library')).json()
        self.assertEqual(lib['tracks'][0]['id'], track_id)
        unchanged = await (await self.client.get(f"/api/dj/library?since={lib['version']}")).json()
        self.assertTrue(unchanged['unchanged'])

        r = await self.client.get(f'/api/dj/tracks/{track_id}/media/full')
        self.assertEqual(r.status, 200)
        self.assertGreater(len(await r.read()), 10000)
        r = await self.client.get(f'/api/dj/tracks/{track_id}/media/peaks')
        self.assertEqual(len(await r.read()) % 3, 0)
        r = await self.client.get(f'/api/dj/tracks/{track_id}/media/vocals')
        self.assertEqual(r.status, 404)

        r = await self.client.patch(f'/api/dj/tracks/{track_id}', json={
            'key': {'camelot': '8A', 'name': 'Am'}, 'cues': [1.5, None, 3], 'bpm': 62})
        body = await r.json()
        self.assertEqual(body['track']['key']['camelot'], '8A')
        self.assertEqual(body['track']['cues'], [1.5, None, 3])
        self.assertTrue(body['track']['analysis']['bpm_manual'])
        r = await self.client.patch(f'/api/dj/tracks/{track_id}', json={'key': {'camelot': '13Z'}})
        self.assertEqual(r.status, 400)

        # Upload a stem the DJ owns for this track.
        for name in ('vocals', 'drums'):
            form = FormData()
            with open(self.wav, 'rb') as handle:
                form.add_field('file', handle.read(), filename=f'{name}.wav')
            r = await self.client.post(f'/api/dj/upload?track={track_id}&stem={name}', data=form)
            self.assertEqual(r.status, 200, await r.text())
        stems = self.service.library.tracks[track_id]['stems']
        self.assertEqual((stems['status'], stems['names']), ('ready', ['vocals', 'drums']))
        r = await self.client.get(f'/api/dj/tracks/{track_id}/media/vocals')
        self.assertEqual(r.status, 200)

        # A whole stem pack at once, names detected from the files.
        form = FormData()
        with open(self.wav, 'rb') as handle:
            data = handle.read()
        for filename in ('Groove (Bass).wav', 'Groove - Keys.wav', 'notes.wav'):
            form.add_field('file', data, filename=filename)
        r = await self.client.post(f'/api/dj/upload?track={track_id}&stem=auto', data=form)
        body = await r.json()
        self.assertEqual(body['stems'], ['bass', 'piano'])
        self.assertEqual(body['skipped'], ['notes.wav'])
        self.assertEqual(self.service.library.tracks[track_id]['stems']['names'],
                         ['vocals', 'drums', 'bass', 'piano'])

        r = await self.client.delete(f'/api/dj/tracks/{track_id}')
        self.assertEqual(r.status, 200)
        self.assertFalse(os.path.exists(dj.track_dir(track_id)))

    async def test_rejects_non_audio_upload_and_bad_stem(self):
        form = FormData()
        form.add_field('file', b'MZ', filename='evil.exe')
        r = await self.client.post('/api/dj/upload', data=form)
        self.assertEqual(r.status, 400)
        r = await self.client.post('/api/dj/upload?track=0123456789abcdef&stem=kazoo', data=FormData({'x': '1'}))
        self.assertEqual(r.status, 400)

    async def test_live_requires_own_huddle_room(self):
        r = await self.client.get('/api/dj/live?room=huddle:someone-else')
        self.assertEqual(r.status, 403)
        r = await self.client.get('/api/dj/live?room=123456')
        self.assertEqual(r.status, 403)

    async def test_capabilities_without_demucs(self):
        caps = await (await self.client.get('/api/dj/capabilities')).json()
        if not os.getenv('DJ_TEST_DEMUCS_PYTHON'):
            self.assertFalse(caps['stems'])
        self.assertEqual(caps['room'], 'huddle:room-1')
        self.assertIn('vocals', caps['upload_stem_names'])

    @unittest.skipUnless(os.getenv('DJ_TEST_DEMUCS_PYTHON'), 'set DJ_TEST_DEMUCS_PYTHON to run Demucs')
    async def test_demucs_separation(self):
        form = FormData()
        with open(self.wav, 'rb') as handle:
            form.add_field('file', handle.read(), filename='Artist - Groove.wav')
        r = await self.client.post('/api/dj/upload', data=form)
        track_id = (await r.json())['added'][0]
        await self.wait_ready(track_id)
        track = await self.wait_ready(track_id, 'stems', timeout=600)
        self.assertEqual(track['stems']['status'], 'ready', track['stems'])
        self.assertEqual(set(track['stems']['names']), {'vocals', 'drums', 'bass', 'other'})
        for name in track['stems']['names']:
            r = await self.client.get(f'/api/dj/tracks/{track_id}/media/{name}')
            self.assertEqual(r.status, 200)


if __name__ == '__main__':
    unittest.main()
