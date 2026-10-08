import asyncio
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stems  # noqa: E402
import webui  # noqa: E402


def song(video_id, title):
    return {'title': title, 'url': f'https://www.youtube.com/watch?v={video_id}',
            'duration': '3:00', 'source_type': 'youtube', 'thumbnail': None}


class StartIndexTests(unittest.TestCase):
    def test_start_index(self):
        entries = [{}, {}, {}]
        self.assertEqual(webui._start_index({'start': 2}, entries), 2)
        self.assertEqual(webui._start_index({'start': '1'}, entries), 1)
        self.assertEqual(webui._start_index({}, entries), 0)
        self.assertEqual(webui._start_index({'start': 3}, entries), 0)
        self.assertEqual(webui._start_index({'start': -1}, entries), 0)
        self.assertEqual(webui._start_index({'start': 'soon'}, entries), 0)

    def test_start_follows_the_title_when_the_list_moved(self):
        entries = [{'title': 'New'}, {'title': 'A'}, {'title': 'B'}]
        self.assertEqual(webui._start_index({'start': 1, 'start_title': 'B'}, entries), 2)
        self.assertEqual(webui._start_index({'start': 2, 'start_title': 'B'}, entries), 2)
        self.assertEqual(webui._start_index({'start': 1, 'start_title': 'Gone'}, entries), 0)


class RequestStemsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (stems.CACHE_DIR, webui._prepare_entry)
        stems.CACHE_DIR = self.tmp.name
        webui._stem_requests.clear()
        webui._stem_queue = None
        self.prepared = []

        async def fake_prepare(entry, force=False, pin_reason=None, background=True):
            self.prepared.append((entry['title'], pin_reason, background))
            if entry['title'] == 'Broken':
                raise RuntimeError('no audio')

        webui._prepare_entry = fake_prepare
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        stems.CACHE_DIR, webui._prepare_entry = self.saved
        webui._stem_requests.clear()
        webui._stem_queue = None
        tasks = asyncio.all_tasks(self.loop)
        for task in tasks:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        self.loop.close()
        self.tmp.cleanup()

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    def test_queues_once_and_separates_in_order(self):
        async def scenario():
            first = webui.request_stems([song('aaaaaaaaaaa', 'One'), song('aaaaaaaaaaa', 'One'), song('bbbbbbbbbbb', 'Broken')])
            again = webui.request_stems([song('aaaaaaaaaaa', 'One')])     # still waiting: not queued twice
            await webui._stem_queue.join()
            return first, again

        first, again = self.run_async(scenario())
        self.assertEqual(first, (2, 0))
        self.assertEqual(again, (0, 0))
        self.assertEqual(self.prepared, [('One', webui.STEMS_PIN, False), ('Broken', webui.STEMS_PIN, False)])
        # Done ones leave the list; failures stay so the page can say so.
        self.assertEqual(list(webui._stem_requests.values()), [{'title': 'Broken', 'status': 'failed'}])

    def test_songs_with_stems_are_not_separated_again(self):
        np.save(stems.stems_path('done'), np.zeros((3, 10, 2), dtype=np.int16))
        open(stems.instrumental_path('done'), 'wb').close()
        stems.remember(['yt:ccccccccccc'], 'done', 'Done')

        async def scenario():
            return webui.request_stems([song('ccccccccccc', 'Done')])

        self.assertEqual(self.run_async(scenario()), (0, 1))
        self.assertEqual(self.prepared, [])
        self.assertIn('done', stems.pinned())


if __name__ == '__main__':
    unittest.main()
