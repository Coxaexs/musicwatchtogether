"""ReelsTogether's feed: which short a Hoffle voice room sees next.

Where shorts come from (all real Shorts: vertical and short):

* a hashtag's Shorts feed, youtube.com/hashtag/<tag>/shorts: about a second,
  30-40 shorts, popular first;
* a creator's Shorts tab, youtube.com/channel/<id>/shorts: half a second,
  newest first;
* a plain search, only for words that have no hashtag feed.

Each of those is an *arm*. The room counts, per arm, how often what it brought
was watched to the end or liked (a success) and how often it was skipped fast
or marked "show less" (a failure), and picks the next arm by Thompson sampling,
leaning towards topics the people in the room like. A few picks in every
hundred go to something nobody asked for, so the feed can find new things.

Every person has a taste of their own (hashtags and creators, learnt from what
they like, mark "show less", watch to the end or skip fast) that follows them
from room to room. The room takes turns choosing for each person who is there,
so the feed fits the group and no one person's taste takes it over.

Everything here is plain dicts and lists so it can be stored as JSON.
"""

import math
import random
import re
import time

#: Hashtags that say nothing about what a short is about.
JUNK_TAGS = {
    'shorts', 'short', 'shortvideo', 'shortsvideo', 'shortsfeed', 'shortfeed',
    'youtubeshorts', 'ytshorts', 'viral', 'viralvideo', 'viralshorts', 'fyp',
    'foryou', 'foryoupage', 'fy', 'fypシ', 'trending', 'trend', 'explore',
    'explorepage', 'reels', 'reel', 'instagram', 'tiktok', 'youtube', 'video',
    'videos', 'subscribe', 'like', 'likes', 'follow', 'new', 'best', 'top',
    'shortsviral', 'shortsyoutube', 'youtuber', 'yt', 'capcut', 'edit',
    'edits', 'meme', 'memes', 'funnyshorts', 'shortsfunny',
}

#: Title words that are not topics.
STOPWORDS = {
    'the', 'and', 'for', 'with', 'this', 'that', 'you', 'your', 'when', 'what',
    'how', 'why', 'his', 'her', 'him', 'she', 'they', 'them', 'its', 'are',
    'was', 'has', 'have', 'not', 'but', 'all', 'can', 'out', 'get', 'got',
    'just', 'from', 'part', 'ever', 'omg', 'wow', 'will', 'been', 'into',
    'than', 'then', 'there', 'their', 'these', 'those', 'would', 'could',
    'should', 'about', 'after', 'before', 'every', 'never', 'always', 'only',
    'very', 'much', 'more', 'most', 'some', 'like', 'over', 'make', 'made',
    'does', 'doing', 'did', 'done', 'being', 'were', 'which', 'while', 'who',
    'whom', 'where', 'here', 'look', 'watch', 'wait', 'till', 'until', 'end',
    'bir', 've', 'ile', 'için', 'gibi', 'çok', 'daha', 'ama', 'bu', 'şu',
    'one', 'two', 'way', 'day', 'time', 'year', 'years', 'really', 'still',
}

#: Topics people can pick to start a feed: (hashtag, label, emoji).
SEED_TOPICS = [
    ('funny', 'Funny', '😂'), ('cats', 'Cats', '🐱'), ('dogs', 'Dogs', '🐶'),
    ('animals', 'Animals', '🦊'), ('satisfying', 'Satisfying', '🫧'),
    ('football', 'Football', '⚽'), ('basketball', 'Basketball', '🏀'),
    ('gaming', 'Gaming', '🎮'), ('minecraft', 'Minecraft', '⛏️'),
    ('cooking', 'Cooking', '🍳'), ('streetfood', 'Street food', '🌮'),
    ('cars', 'Cars', '🏎️'), ('science', 'Science', '🔬'),
    ('magic', 'Magic', '🪄'), ('dance', 'Dance', '💃'), ('music', 'Music', '🎵'),
    ('art', 'Art', '🎨'), ('travel', 'Travel', '✈️'), ('fitness', 'Fitness', '💪'),
    ('tech', 'Tech', '💻'), ('lifehacks', 'Life hacks', '💡'),
    ('pranks', 'Pranks', '🙈'), ('parkour', 'Parkour', '🤸'),
    ('nature', 'Nature', '🌿'), ('movies', 'Movies', '🎬'), ('anime', 'Anime', '🌸'),
    ('asmr', 'ASMR', '🎧'), ('diy', 'DIY', '🔨'),
]

#: Where an exploring pick looks when it wants something new.
EXPLORE_TOPICS = [tag for tag, _label, _emoji in SEED_TOPICS] + [
    'comedy', 'fails', 'wildlife', 'skateboarding', 'baking', 'gadgets',
    'space', 'history', 'ocean', 'rescue', 'woodworking', 'drawing',
    'skills', 'tricks', 'oddlysatisfying', 'puppy', 'kitten', 'fishing',
    'camping', 'football skills', 'chess', 'lego', 'pottery', 'piano',
]

EXPLORE_RATE = 0.15          # share of picks that go somewhere new
TASTE_MIN, TASTE_MAX = -10.0, 30.0
MAX_TAGS, MAX_CREATORS = 120, 40
MAX_SEEN = 1500
MAX_ARMS = 160
DECAY = 0.985                # every lesson ages a person's older taste a little

#: What each lesson does: (arm successes, arm failures, tag delta, creator delta).
#: Tag deltas are multiplied by each tag's weight in the short.
SIGNALS = {
    'complete': (1.0, 0.0, 0.6, 0.8),   # watched to the end (or looped)
    'partial': (0.4, 0.0, 0.2, 0.2),    # watched more than half
    'skip': (0.0, 1.0, -0.7, -0.6),     # swiped away in the first third
    'like': (1.0, 0.0, 2.0, 2.5),
    'unlike': (-1.0, 0.0, -2.0, -2.5),
    'less': (0.0, 2.0, -3.0, -4.0),     # "show me less of this"
    'react': (0.3, 0.0, 0.5, 0.5),
    'added': (0.0, 0.0, 3.0, 2.0),      # pasted it into the feed
}

_HASHTAG_RE = re.compile(r'#([^\s#.,!?;:()\[\]{}"\'`|/\\]+)', re.UNICODE)
_WORD_RE = re.compile(r'[^\W\d_]{4,}', re.UNICODE)


def normalize_tag(text):
    """'#Street Food!' -> 'streetfood'. Empty when it is not a usable tag."""
    tag = str(text or '').strip().lower().lstrip('#')
    tag = re.sub(r'[\W_]+', '', tag, flags=re.UNICODE)[:40]
    if len(tag) < 3 or tag.isdigit() or tag in JUNK_TAGS:
        return ''
    return tag


def item_tags(title='', tags=None):
    """What a short is about, as {tag: weight}.

    Hashtags in the title are what the creator says it is about (1.0); the
    video's own tags come next (0.7). Only without either do plain title words
    count, as a weak hint (0.25).
    """
    found = {}

    def put(tag, weight):
        if tag and weight > found.get(tag, 0):
            found[tag] = weight

    title = str(title or '')
    for raw in _HASHTAG_RE.findall(title)[:8]:
        put(normalize_tag(raw), 1.0)
    for raw in list(tags or [])[:8]:
        put(normalize_tag(raw), 0.7)
    if not found:
        # No hashtags and no tags: title words are all there is, and a weak hint.
        words = [w.lower() for w in _WORD_RE.findall(title) if len(w) >= 5]
        for word in [w for w in words if w not in STOPWORDS][:5]:
            put(normalize_tag(word), 0.25)
    return found


# ---------------------------------------------------------------- people

def new_profile():
    return {'tags': {}, 'creators': {}, 'likes': 0, 'updated': 0}


def profile_size(profile):
    return len((profile or {}).get('tags') or {}) + len((profile or {}).get('creators') or {})


def _clamp(value):
    return max(TASTE_MIN, min(TASTE_MAX, value))


def learn_person(profile, item, signal, now=None):
    """Teach one person's taste what they did with *item*."""
    if signal not in SIGNALS or not item:
        return
    _s, _f, tag_delta, creator_delta = SIGNALS[signal]
    tags = profile.setdefault('tags', {})
    creators = profile.setdefault('creators', {})
    for key in list(tags):
        tags[key] = round(tags[key] * DECAY, 3)
        if abs(tags[key]) < 0.15:
            tags.pop(key)
    for key in list(creators):
        creators[key]['w'] = round(creators[key]['w'] * DECAY, 3)
        if abs(creators[key]['w']) < 0.15:
            creators.pop(key)
    for tag, weight in (item.get('tags_w') or {}).items():
        tags[tag] = round(_clamp(tags.get(tag, 0) + tag_delta * weight), 3)
    cid = item.get('channel_id')
    if cid:
        entry = creators.setdefault(cid, {'w': 0, 'name': item.get('uploader') or ''})
        entry['w'] = round(_clamp(entry['w'] + creator_delta), 3)
        if item.get('uploader'):
            entry['name'] = item['uploader']
    if signal == 'like':
        profile['likes'] = profile.get('likes', 0) + 1
    if len(tags) > MAX_TAGS:
        for key, _w in sorted(tags.items(), key=lambda kv: abs(kv[1]))[:len(tags) - MAX_TAGS]:
            tags.pop(key, None)
    if len(creators) > MAX_CREATORS:
        weakest = sorted(creators.items(), key=lambda kv: abs(kv[1]['w']))
        for key, _w in weakest[:len(creators) - MAX_CREATORS]:
            creators.pop(key, None)
    profile['updated'] = now or time.time()


def split_topics(text):
    """'cats, street food' -> ['cats', 'street food']; 'cats dogs' -> ['cats', 'dogs']."""
    text = str(text or '')
    parts = text.split(',') if ',' in text else text.split()
    return [part.strip() for part in parts if part.strip()][:20]


def seed_person(profile, words):
    """Topics someone picked: a firm start, not a lifelong commitment."""
    tags = profile.setdefault('tags', {})
    added = []
    for raw in words:
        tag = normalize_tag(raw)
        if tag:
            tags[tag] = max(tags.get(tag, 0), 4.0)
            added.append(tag)
    profile['updated'] = time.time()
    return added


def forget_tag(profile, tag):
    tag = normalize_tag(tag)
    if tag and tag in (profile.get('tags') or {}):
        profile['tags'][tag] = min(profile['tags'][tag], -2.0)
        return True
    return False


def _normalized(weights):
    top = max((w for w in weights.values() if w > 0), default=0)
    if top <= 0:
        return {k: v for k, v in weights.items() if v < 0}
    return {k: v / top for k, v in weights.items()}


def taste(profiles, seeds=None):
    """One {tag: weight} for a group of people, each counting the same.

    Weights are scaled so each person's favourite is 1, then averaged, so a
    heavy user can't drown out someone who just arrived. Room seeds count as
    one more person.
    """
    merged = {}
    people = [p for p in profiles if p and p.get('tags')]
    if seeds:
        people.append({'tags': dict(seeds)})
    if not people:
        return {}
    for person in people:
        for tag, weight in _normalized(person['tags']).items():
            merged[tag] = merged.get(tag, 0) + weight / len(people)
    return merged


def creators_of(profiles):
    """Creators the group likes: [(channel id, name, weight)], best first."""
    merged = {}
    for person in profiles:
        for cid, entry in ((person or {}).get('creators') or {}).items():
            name, weight = entry.get('name') or '', entry.get('w', 0)
            old = merged.get(cid, ('', 0))
            merged[cid] = (name or old[0], old[1] + weight)
    ranked = sorted(((cid, n, w) for cid, (n, w) in merged.items() if w > 0.5),
                    key=lambda row: -row[2])
    return ranked


# ---------------------------------------------------------------- rooms

def new_feed():
    return {'arms': {}, 'seen': [], 'seeds': {}, 'turn': 0, 'recent_creators': []}


def arm_url(arm):
    kind, _, value = arm.partition(':')
    if kind == 'tag':
        return f'https://www.youtube.com/hashtag/{value}/shorts'
    if kind == 'creator':
        return f'https://www.youtube.com/channel/{value}/shorts'
    return f'ytsearch30:{value} #shorts'


def arm_label(arm, creator_names=None):
    kind, _, value = arm.partition(':')
    if kind == 'tag':
        return f'#{value}'
    if kind == 'creator':
        return '@' + ((creator_names or {}).get(value) or 'a creator you like')
    return value


def learn_arm(feed, arm, signal):
    if not arm or signal not in SIGNALS:
        return
    success, failure, _t, _c = SIGNALS[signal]
    arms = feed.setdefault('arms', {})
    stats = arms.setdefault(arm, [0.0, 0.0, 0.0])
    stats[0] = round(max(0.0, stats[0] + success), 2)
    stats[1] = round(max(0.0, stats[1] + failure), 2)
    # Old evidence fades so a topic that flopped once can come back later.
    if stats[0] + stats[1] > 40:
        stats[0], stats[1] = round(stats[0] * 0.8, 2), round(stats[1] * 0.8, 2)
    if len(arms) > MAX_ARMS:
        stale = sorted(arms.items(), key=lambda kv: kv[1][2])[:len(arms) - MAX_ARMS]
        for key, _stats in stale:
            arms.pop(key, None)


def mark_seen(feed, vid):
    if not vid:
        return
    seen = feed.setdefault('seen', [])
    if vid not in seen:
        seen.append(vid)
        if len(seen) > MAX_SEEN:
            del seen[:len(seen) - MAX_SEEN]


def choose_driver(feed, people):
    """Whose turn it is: [(user id, profile)] -> (user id, profile) or (None, None).

    Only people with some taste get a turn; a newcomer with none still counts
    in the group's taste.
    """
    tasted = [(uid, prof) for uid, prof in people if profile_size(prof) >= 2]
    if not tasted:
        return None, None
    turn = feed.get('turn', 0)
    feed['turn'] = turn + 1
    return tasted[turn % len(tasted)]


def choose_arm(feed, group_taste, driver_taste=None, creators=None, rng=None, now=None,
               exclude=()):
    """The source the next shorts come from: (arm, why) where why is
    'taste', 'creator' or 'explore'."""
    rng = rng or random
    now = now or time.time()
    arms = feed.get('arms') or {}
    blend = dict(group_taste or {})
    if driver_taste:
        blend = {tag: 0.4 * blend.get(tag, 0) + 0.6 * driver_taste.get(tag, 0)
                 for tag in set(blend) | set(driver_taste)}
    candidates = []
    positive = sorted(((t, w) for t, w in blend.items() if w > 0.05), key=lambda kv: -kv[1])
    top = positive[0][1] if positive else 1
    for tag, weight in positive[:10]:
        candidates.append((f'tag:{tag}', weight / top, 'taste'))
    for cid, _name, weight in (creators or [])[:4]:
        candidates.append((f'creator:{cid}', min(1.0, weight / 6), 'creator'))
    candidates = [c for c in candidates if c[0] not in exclude]

    # A new feed sticks to what people asked for before it wanders.
    explore = EXPLORE_RATE if len(feed.get('seen') or []) >= 8 else 0.0
    if candidates and rng.random() >= explore:
        best, best_score, best_why = None, -1.0, 'taste'
        for arm, prior, why in candidates:
            succ, fail, last = (arms.get(arm) or [0.0, 0.0, 0.0])[:3]
            sample = rng.betavariate(1 + succ, 1 + fail)
            score = sample * (0.5 + 0.5 * prior)
            if now - last < 60:
                score *= 0.55     # spread the feed over a few topics
            if score > best_score:
                best, best_score, best_why = arm, score, why
        arms.setdefault(best, [0.0, 0.0, 0.0])[2] = now
        feed['arms'] = arms
        return best, best_why

    disliked = {t for t, w in blend.items() if w < -0.2}
    recently = {a for a, s in arms.items() if now - s[2] < 600}
    pool = [t for t in EXPLORE_TOPICS
            if normalize_tag(t) not in disliked and f'tag:{normalize_tag(t)}' not in recently
            and f'tag:{normalize_tag(t)}' not in exclude]
    topic = rng.choice(pool or EXPLORE_TOPICS)
    arm = f'tag:{normalize_tag(topic)}' if ' ' not in topic else f'search:{topic}'
    arms.setdefault(arm, [0.0, 0.0, 0.0])[2] = now
    feed['arms'] = arms
    return arm, 'explore'


def rank_entries(entries, feed, blend, rng=None, limit=3):
    """Best unseen entries of a flat source listing, for this room.

    Entries are yt-dlp flat entries: id, title, view_count, maybe channel_id.
    """
    rng = rng or random
    seen = set(feed.get('seen') or [])
    recent = list(feed.get('recent_creators') or [])
    scored = []
    for entry in entries:
        vid = entry.get('id')
        if not vid or vid in seen:
            continue
        duration = entry.get('duration')
        if duration and duration > 180:
            continue
        tags = item_tags(entry.get('title'))
        affinity = sum(blend.get(tag, 0) * weight for tag, weight in tags.items())
        affinity /= math.sqrt(max(1, len(tags)))
        views = entry.get('view_count') or 0
        score = affinity + 0.12 * math.log10(views + 10)
        creator = entry.get('channel_id') or (entry.get('channel') or '').lower()
        if creator and creator in recent:
            score -= 1.0 + 0.5 * recent.count(creator)
        score += rng.gauss(0, 0.35)
        scored.append((score, entry))
    scored.sort(key=lambda pair: -pair[0])
    return [entry for _score, entry in scored[:limit]]


def note_creator(feed, creator):
    if not creator:
        return
    recent = feed.setdefault('recent_creators', [])
    recent.append(creator)
    del recent[:-10]


def top_interests(group_taste, limit=12):
    return [tag for tag, weight in sorted(group_taste.items(), key=lambda kv: -kv[1])
            if weight > 0.05][:limit]
