"""Musixmatch synced lyrics (through RapidAPI), for Hoffle's /lyricsnow only.

The plan allows about 300 requests a month, so this is careful with them:

* Only Hoffle's /lyricsnow for the song that is playing reaches it, and only
  after lrclib had no synced lyrics. Discord, the dashboard, /lyrics and the
  play-time prefetch never call it.
* Every answer is kept on disk under DATA_DIR/musixmatch/: lyrics for good,
  "not found" for 30 days, so a song costs one request (one more only when a
  different-length cut of it is played).
* Two people asking for the same song at once share one request, and requests
  go out one at a time so the budget check cannot race.
* RapidAPI says how many requests are left and when the plan renews
  (X-RateLimit-Requests-Remaining / -Reset); nothing is sent once that reaches
  zero. Before its first answer we count our own requests over 30 days. A
  daily cap keeps one busy day (or one person) from spending the month.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import unicodedata

import aiohttp

from storage import load_json, save_json

MISS_TTL = 30 * 86400        # ask about a song Musixmatch did not have after this
ERROR_RETRY = 15 * 60        # a request that failed for one song waits this long
BLOCK_ON_REJECT = 6 * 3600   # the key was refused (401/403): stop for this long
WINDOW = 30 * 86400          # our own monthly count, before RapidAPI has said
DAY = 86400
SAME_CUT = 15                # seconds: songs this close in length are the same cut
MAX_CUTS = 4                 # different-length cuts kept per song
KEEP_CALLS = 40 * 86400      # request timestamps older than this are dropped

LRC_STAMP = re.compile(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]')
# Musixmatch's free tier stamps restricted lyrics with notices like these.
NOTICE = re.compile(r'not for commercial use|^\*{3,}|^\(\d{6,}\)$', re.IGNORECASE)
NOTE = '♪'


def song_key(track, artist):
    """Accent-, case- and punctuation-blind identity of one song."""
    def norm(text):
        text = unicodedata.normalize('NFKD', str(text or ''))
        text = ''.join(ch for ch in text if not unicodedata.combining(ch))
        return ' '.join(re.findall(r'\w+', text.casefold().replace('ı', 'i')))
    return f'{norm(artist)}|{norm(track)}'


def _clock(seconds):
    seconds = max(0, int(round(seconds)))
    return f'{seconds // 60}:{seconds % 60:02d}'


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _tidy(lines):
    """Sorted [[seconds, text]], one ♪ per instrumental gap, notices dropped."""
    lines.sort(key=lambda line: line[0])
    out = []
    for at, text in lines:
        if NOTICE.search(text):
            continue
        if text == NOTE and (not out or out[-1][1] == NOTE):
            continue
        out.append([round(at, 2), text])
    return out if any(text != NOTE for _, text in out) else []


def _time_of(item):
    stamp = item.get('time')
    if isinstance(stamp, dict):
        total = _number(stamp.get('total'))
        if total is not None or stamp.get('total') == 0:
            return total or 0.0
        try:
            return (int(stamp.get('minutes') or 0) * 60 + int(stamp.get('seconds') or 0)
                    + int(stamp.get('hundredths') or 0) / 100)
        except (TypeError, ValueError):
            return None
    if isinstance(stamp, (int, float)):
        return float(stamp)
    if item.get('startTimeMs') is not None:
        try:
            return float(item['startTimeMs']) / 1000
        except (TypeError, ValueError):
            return None
    return None


def _timed_lines(items):
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get('text', item.get('words'))
        at = _time_of(item)
        if at is None or not isinstance(text, str):
            continue
        lines.append([at, text.strip() or NOTE])
    return _tidy(lines)


def _lrc_lines(text):
    lines = []
    for raw in text.splitlines():
        stamps = LRC_STAMP.findall(raw)
        words = LRC_STAMP.sub('', raw).strip() or NOTE
        for minutes, seconds, fraction in stamps:
            at = int(minutes) * 60 + int(seconds)
            if fraction:
                at += int(fraction) / 10 ** len(fraction)
            lines.append([at, words])
    return _tidy(lines)


def _plain(text):
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and line != NOTE and not NOTICE.search(line)]
    # A one- or two-line body is an error message, not a song.
    return '\n'.join(lines) if len(lines) >= 3 else ''


def parse_response(payload):
    """(lines, plain, info) from a Musixmatch answer.

    lines: [[seconds, text], ...] when the lyrics carry timings, else [].
    plain: the words as text when there are no timings, else ''.
    info:  what Musixmatch matched: {'track', 'artist', 'length'} when given.
    """
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    if isinstance(payload, str):
        lines = _lrc_lines(payload) if LRC_STAMP.search(payload) else []
        return lines, '' if lines else _plain(payload), {}
    if isinstance(payload, list):
        return _timed_lines(payload), '', {}
    if not isinstance(payload, dict):
        return [], '', {}

    track_info = payload.get('track_info')
    info = {}
    if isinstance(track_info, dict):
        info = {
            'track': track_info.get('track_name') or track_info.get('name'),
            'artist': track_info.get('artist_name') or track_info.get('artist'),
            'length': _number(track_info.get('track_length') or track_info.get('duration')),
        }
        info = {key: value for key, value in info.items() if value}
    if payload.get('success') is False or payload.get('error'):
        return [], '', info

    lines = []
    if isinstance(payload.get('lyrics'), list):
        lines = _timed_lines(payload['lyrics'])
    for key in ('lrc_lyrics', 'syncedLyrics', 'lyrics'):
        if not lines and isinstance(payload.get(key), str) and LRC_STAMP.search(payload[key]):
            lines = _lrc_lines(payload[key])
    plain = ''
    if not lines:
        for key in ('plain_lyrics', 'plainLyrics', 'lyrics', 'lyrics_body'):
            if isinstance(payload.get(key), str):
                plain = _plain(payload[key])
                if plain:
                    break
    return lines, plain, info


class MusixmatchLyrics:
    def __init__(self, data_dir, api_key, host, *, monthly_limit=300, daily_limit=30,
                 logger=None, http_get=None):
        self.root = os.path.join(data_dir, 'musixmatch')
        self.usage_path = os.path.join(self.root, 'usage.json')
        self.api_key = api_key or ''
        self.host = host
        self.monthly_limit = int(monthly_limit)
        self.daily_limit = int(daily_limit)
        self.log = logger or logging.getLogger(__name__)
        self._http_get = http_get or self._get
        self._lock = None
        self._inflight = {}
        self._failed = {}     # song key -> when a request for it last failed
        self._warned = {}     # reason -> when it was last logged

    # ---------- disk ----------

    def _path(self, key):
        slug = re.sub(r'[^a-z0-9]+', '_', key.encode('ascii', 'ignore').decode().lower()).strip('_')
        digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]
        return os.path.join(self.root, 'lyrics', f'{slug[:60]}-{digest}.json')

    def _entry(self, key):
        entry = load_json(self._path(key), None, self.log)
        return entry if isinstance(entry, dict) else None

    async def _save(self, path, data):
        await asyncio.to_thread(save_json, path, data, self.log)

    def usage(self):
        usage = load_json(self.usage_path, {}, self.log)
        return usage if isinstance(usage, dict) else {}

    # ---------- answers ----------

    @staticmethod
    def _closeness(cut, duration):
        lengths = [cut.get('length'), *(cut.get('asked') or [])]
        gaps = [abs(float(length) - duration) for length in lengths if _number(length)]
        return min(gaps) if gaps else None

    def _pick(self, entry, duration):
        cuts = entry.get('cuts') or []
        if not cuts:
            return None
        if not duration:
            return cuts[0]
        scored = [(self._closeness(cut, duration), cut) for cut in cuts]
        near = [(gap, cut) for gap, cut in scored if gap is not None and gap <= SAME_CUT]
        if near:
            return min(near, key=lambda pair: pair[0])[1]
        unknown = [cut for gap, cut in scored if gap is None]
        if unknown:
            return unknown[0]
        if len(cuts) >= MAX_CUTS:
            return min(scored, key=lambda pair: pair[0])[1]
        return None

    @staticmethod
    def _result(cut, duration):
        result = {
            'track': cut.get('track'),
            'artist': cut.get('artist'),
            'lines': cut.get('lines') or [],
            'lyrics': cut.get('lyrics') or '',
            'length': cut.get('length'),
            'source': 'musixmatch',
        }
        length = _number(cut.get('length'))
        if duration and length and abs(length - duration) > SAME_CUT:
            result['version_length'] = length
        return result

    def cached(self, track, artist, duration=None):
        """(answer, known) from disk, spending nothing.

        known is True when disk settles it: lyrics for a cut of about this
        length, or a recent "Musixmatch does not have it".
        """
        if not (track or '').strip():
            return None, True
        entry = self._entry(song_key(track, artist))
        if not entry:
            return None, False
        cut = self._pick(entry, _number(duration))
        if cut:
            return self._result(cut, _number(duration)), True
        if time.time() - float(entry.get('miss_at') or 0) < MISS_TTL:
            return None, True
        return None, False

    async def lookup(self, track, artist, duration=None):
        """Lyrics for one song: disk first, then at most one request."""
        found, known = self.cached(track, artist, duration)
        if known:
            return found
        key = song_key(track, artist)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(key, track, artist, _number(duration)))
            self._inflight[key] = task
            task.add_done_callback(lambda _done, key=key: self._inflight.pop(key, None))
        # shield: someone giving up on the answer must not cancel a paid request
        return await asyncio.shield(task)

    # ---------- budget ----------

    def left(self, usage=None, now=None):
        """Requests we may still send right now."""
        return max(0, min(self.budget(usage, now)))

    def budget(self, usage=None, now=None):
        """(left this plan month, left today)."""
        usage = self.usage() if usage is None else usage
        now = time.time() if now is None else now
        if float(usage.get('blocked_until') or 0) > now:
            return 0, 0
        calls = [float(stamp) for stamp in usage.get('calls') or []]
        today = self.daily_limit - sum(1 for stamp in calls if stamp > now - DAY)
        remaining = usage.get('remaining')
        reset_at = float(usage.get('reset_at') or 0)
        if remaining is not None and reset_at > now:
            seen_at = float(usage.get('seen_at') or 0)
            month = int(remaining) - sum(1 for stamp in calls if stamp > seen_at)
        elif reset_at:
            # The plan renewed after RapidAPI last told us; count from then.
            limit = int(usage.get('limit') or self.monthly_limit)
            month = limit - sum(1 for stamp in calls if stamp > reset_at)
        else:
            month = self.monthly_limit - sum(1 for stamp in calls if stamp > now - WINDOW)
        return max(0, month), max(0, today)

    def _note_headers(self, usage, headers, now):
        headers = {str(name).lower(): value for name, value in (headers or {}).items()}
        remaining = headers.get('x-ratelimit-requests-remaining')
        if remaining is None:
            return
        try:
            usage['remaining'] = int(remaining)
            usage['seen_at'] = now
            if headers.get('x-ratelimit-requests-limit') is not None:
                usage['limit'] = int(headers['x-ratelimit-requests-limit'])
            if headers.get('x-ratelimit-requests-reset') is not None:
                usage['reset_at'] = now + int(headers['x-ratelimit-requests-reset'])
        except (TypeError, ValueError):
            pass

    def _warn(self, reason, message, *args, every=3600):
        now = time.time()
        if now - self._warned.get(reason, 0) >= every:
            self._warned[reason] = now
            self.log.warning(message, *args)

    # ---------- the request ----------

    async def _get(self, url, params, headers):
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                return resp.status, dict(resp.headers), await resp.text()

    async def _fetch(self, key, track, artist, duration):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            found, known = self.cached(track, artist, duration)
            if known:       # answered while this one waited its turn
                return found
            if not self.api_key:
                self._warn('key', 'Musixmatch skipped: no API key (secrets/lyrics_api_key.enc)')
                return None
            now = time.time()
            if now - self._failed.get(key, 0) < ERROR_RETRY:
                return None
            usage = self.usage()
            if self.left(usage, now) <= 0:
                self._warn('budget', 'Musixmatch skipped: request budget used up (%s)',
                           json.dumps({k: usage.get(k) for k in ('remaining', 'reset_at', 'blocked_until')}))
                return None
            # Counted before sending, so a crash mid-request still counts.
            usage['calls'] = [stamp for stamp in usage.get('calls') or []
                              if float(stamp) > now - KEEP_CALLS] + [now]
            await self._save(self.usage_path, usage)

            params = {'t': track, 'type': 'json'}
            if artist:
                params['a'] = artist
            if duration:
                params['d'] = _clock(duration)
            try:
                status, headers, body = await self._http_get(
                    f'https://{self.host}/songs/lyrics', params,
                    {'x-rapidapi-key': self.api_key, 'x-rapidapi-host': self.host})
            except Exception as exc:
                self._failed[key] = now
                self.log.warning('Musixmatch request failed for %s - %s: %s', artist, track, exc)
                return None

            self._note_headers(usage, headers, now)
            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                payload = body
            # The wrapper also reports errors as {"success": false, "code": 5xx}
            # under HTTP 200; a passing hiccup must not be kept as "not found".
            code = payload.get('code') if isinstance(payload, dict) else None
            if status == 200 and isinstance(code, int) and code >= 400:
                status = code
            if status == 429:
                out_of_month = usage.get('remaining') == 0 and usage.get('reset_at')
                usage['blocked_until'] = usage['reset_at'] if out_of_month else now + 60
            elif status in (401, 403):
                usage['blocked_until'] = now + BLOCK_ON_REJECT
                self.log.error('Musixmatch refused the API key (HTTP %s); pausing for 6 hours', status)
            await self._save(self.usage_path, usage)

            if status not in (200, 404):
                self._failed[key] = now
                self.log.warning('Musixmatch HTTP %s for %s - %s', status, artist, track)
                return None
            lines, plain, info = parse_response(payload)

            entry = self._entry(key) or {'artist': artist, 'track': track}
            entry.setdefault('cuts', [])
            if lines or plain:
                cut = {
                    'track': info.get('track') or track,
                    'artist': info.get('artist') or artist,
                    'length': info.get('length'),
                    'asked': [duration] if duration else [],
                    'lines': lines,
                    'lyrics': '' if lines else plain,
                    'fetched_at': now,
                }
                same = next((old for old in entry['cuts']
                             if cut['length'] and _number(old.get('length'))
                             and abs(float(old['length']) - cut['length']) < 1), None)
                if same:     # the cut we already had: remember this length too
                    if duration:
                        same['asked'] = sorted(set(same.get('asked') or []) | {duration})
                    cut = same
                else:
                    entry['cuts'].append(cut)
                entry.pop('miss_at', None)
            else:
                entry['miss_at'] = now
            await self._save(self._path(key), entry)
            self.log.info('Musixmatch: %s - %s -> %s (%s requests left this month, %s today)',
                          artist, track, 'synced' if lines else 'plain' if plain else 'not found',
                          *self.budget(usage, now))
            return self._result(cut, duration) if lines or plain else None
