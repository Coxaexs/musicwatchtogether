"""/playnext in the DJ booth is forced: Real DJ does not score it, and it takes the cued deck's place."""
import asyncio
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import realdj  # noqa: E402


class Crate:
    def __init__(self, items):
        self.queue = list(items)
        self.returned = []

    def items(self):
        return [dict(item) for item in self.queue]

    async def take(self, index=0):
        return self.queue.pop(index) if 0 <= index < len(self.queue) else None

    async def put_back(self, item):
        self.returned.append(item.get("title"))
        self.queue.insert(0, {"title": item.get("title"), "hub_track": item.get("hub_track")})


class PlayNext(unittest.TestCase):
    def test_real_dj_takes_the_request_whatever_fits(self):
        items = [{"title": "Fits", "keys": []}, {"title": "Asked for", "keys": [], "next": True}]
        for seed in range(5):
            self.assertEqual(realdj.pick_next(items, None, 10, [], random.Random(seed)), 1)

    def _session(self, queue, cued_info):
        session = dj.DJSession("huddle:room", "huddle", Crate(queue))
        a, b = session.engine.decks["A"], session.engine.decks["B"]
        a.audio = b.audio = np.zeros((dj.SR * 4, 2), dtype=np.float32)
        a.playing = True
        session.engine.master_name = "A"
        b.info = cued_info
        loads = []
        session.load_in_background = lambda deck, item, **kw: loads.append((deck, item.get("title")))
        return session, loads

    def test_it_takes_the_cued_decks_place(self):
        session, loads = self._session([{"title": "Other"}, {"title": "Asked for", "next": True}],
                                       {"title": "Cued", "hub_track": {"id": "t1", "title": "Cued"}})
        asyncio.run(session._force_play_next())
        self.assertEqual(loads, [("B", "Asked for")])
        self.assertEqual(session.crate.returned, ["Cued"])          # the bumped song goes back to the front
        self.assertEqual([i["title"] for i in session.crate.queue], ["Cued", "Other"])

    def test_it_waits_out_a_mix_and_leaves_a_requested_cue_alone(self):
        session, loads = self._session([{"title": "Asked for", "next": True}], {"title": "Also asked", "next": True})
        asyncio.run(session._force_play_next())
        session2, loads2 = self._session([{"title": "Asked for", "next": True}], {"title": "Cued"})
        session2.engine.transition = object()
        asyncio.run(session2._force_play_next())
        session3, loads3 = self._session([{"title": "Nothing asked"}], {"title": "Cued"})
        asyncio.run(session3._force_play_next())
        self.assertEqual((loads, loads2, loads3), ([], [], []))


if __name__ == "__main__":
    unittest.main()
