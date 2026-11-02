import random
import unittest
from unittest import mock

import reels_brain as brain


class TagTests(unittest.TestCase):
    def test_normalize_tag(self):
        self.assertEqual(brain.normalize_tag("#Street Food!"), "streetfood")
        self.assertEqual(brain.normalize_tag("Çiğköfte"), "çiğköfte")
        for junk in ("#shorts", "#fyp", "ab", "1234", "", None):
            self.assertEqual(brain.normalize_tag(junk), "")

    def test_item_tags_weigh_hashtags_over_words(self):
        tags = brain.item_tags("Kitten learns to jump #cats #shorts #funny", ["Pets"])
        self.assertEqual(tags["cats"], 1.0)
        self.assertEqual(tags["funny"], 1.0)
        self.assertEqual(tags["pets"], 0.7)
        self.assertNotIn("kitten", tags)
        self.assertNotIn("shorts", tags)
        words = brain.item_tags("Kitten learns trampoline, wow")
        self.assertEqual(words, {"kitten": 0.25, "learns": 0.25, "trampoline": 0.25})


class PersonTests(unittest.TestCase):
    item = {"tags_w": {"cats": 1.0, "kitten": 0.25}, "channel_id": "UC1", "uploader": "Cat TV"}

    def test_like_and_less_move_tags_and_creator(self):
        profile = brain.new_profile()
        brain.learn_person(profile, self.item, "like")
        self.assertGreater(profile["tags"]["cats"], profile["tags"]["kitten"])
        self.assertGreater(profile["creators"]["UC1"]["w"], 0)
        self.assertEqual(profile["creators"]["UC1"]["name"], "Cat TV")
        brain.learn_person(profile, self.item, "less")
        brain.learn_person(profile, self.item, "less")
        self.assertLess(profile["tags"]["cats"], 0)
        self.assertLess(profile["creators"]["UC1"]["w"], 0)

    def test_old_taste_fades(self):
        profile = brain.new_profile()
        brain.learn_person(profile, {"tags_w": {"chess": 1.0}}, "like")
        start = profile["tags"]["chess"]
        for _ in range(40):
            brain.learn_person(profile, {"tags_w": {"cooking": 1.0}}, "complete")
        self.assertLess(profile["tags"]["chess"], start * 0.6)

    def test_taste_counts_each_person_the_same(self):
        heavy = {"tags": {"football": 30.0, "cars": 15.0}}
        light = {"tags": {"cats": 2.0}}
        merged = brain.taste([heavy, light])
        self.assertAlmostEqual(merged["football"], merged["cats"])
        self.assertLess(merged["cars"], merged["cats"])

    def test_split_topics(self):
        self.assertEqual(brain.split_topics("cats, street food"), ["cats", "street food"])
        self.assertEqual(brain.split_topics("cats dogs"), ["cats", "dogs"])

    def test_seed_and_forget(self):
        profile = brain.new_profile()
        self.assertEqual(brain.seed_person(profile, ["#Cats", "fyp", "street food"]), ["cats", "streetfood"])
        self.assertTrue(brain.forget_tag(profile, "cats"))
        self.assertLess(profile["tags"]["cats"], 0)


class FeedTests(unittest.TestCase):
    def test_drivers_take_turns(self):
        feed = brain.new_feed()
        tasted = {"tags": {"a1": 1, "b1": 1}}
        people = [("ann", tasted), ("new", brain.new_profile()), ("bob", tasted)]
        order = [brain.choose_driver(feed, people)[0] for _ in range(4)]
        self.assertEqual(order, ["ann", "bob", "ann", "bob"])
        self.assertEqual(brain.choose_driver(brain.new_feed(), [("new", None)]), (None, None))

    def test_arms_that_flop_lose_to_arms_that_land(self):
        feed = brain.new_feed()
        feed["arms"] = {"tag:cats": [30.0, 2.0, 0.0], "tag:dogs": [1.0, 30.0, 0.0]}
        rng = random.Random(7)
        picks = []
        with mock.patch.object(brain, "EXPLORE_RATE", 0.0):
            for _ in range(200):
                arm, why = brain.choose_arm(feed, {"cats": 1.0, "dogs": 1.0}, rng=rng, now=10_000)
                feed["arms"][arm][2] = 0.0       # no recency damping in this test
                picks.append(arm)
        self.assertEqual(why, "taste")
        self.assertGreater(picks.count("tag:cats"), 180)

    def test_exploring_avoids_what_the_room_dislikes(self):
        feed = brain.new_feed()
        rng = random.Random(3)
        disliked = {brain.normalize_tag(t): -5.0 for t in brain.EXPLORE_TOPICS[:-1]}
        arm, why = brain.choose_arm(feed, disliked, rng=rng)
        self.assertEqual(why, "explore")
        last = brain.EXPLORE_TOPICS[-1]
        self.assertIn(brain.normalize_tag(last) if " " not in last else last, arm)

    def test_rank_skips_seen_and_long_and_spreads_creators(self):
        feed = brain.new_feed()
        brain.mark_seen(feed, "seen1")
        brain.note_creator(feed, "uc-repeat")
        brain.note_creator(feed, "uc-repeat")
        entries = [
            {"id": "seen1", "title": "#cats"},
            {"id": "long1", "title": "#cats", "duration": 600},
            {"id": "rep", "title": "#cats", "channel_id": "uc-repeat", "view_count": 10_000_000},
            {"id": "fresh", "title": "#cats", "channel_id": "uc-new", "view_count": 10_000_000},
        ]
        ranked = brain.rank_entries(entries, feed, {"cats": 1.0}, rng=random.Random(1), limit=4)
        ids = [e["id"] for e in ranked]
        self.assertEqual(ids, ["fresh", "rep"])

    def test_arm_urls(self):
        self.assertEqual(brain.arm_url("tag:cats"), "https://www.youtube.com/hashtag/cats/shorts")
        self.assertEqual(brain.arm_url("creator:UC1"), "https://www.youtube.com/channel/UC1/shorts")
        self.assertTrue(brain.arm_url("search:football skills").startswith("ytsearch"))


if __name__ == "__main__":
    unittest.main()
