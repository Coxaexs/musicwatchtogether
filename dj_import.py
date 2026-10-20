#!/usr/bin/env python3
"""Import playlists into the DJ booth library and prepare them ahead of a set.

    ./env/bin/python dj_import.py https://open.spotify.com/playlist/... [more links]

Each Spotify playlist/album (or YouTube playlist, song link, or plain search)
becomes a crate in the booth. Every track is downloaded through the same
YouTube matching that /play uses, analysed (tempo, beat grid, waveform) and,
when Demucs is installed (DJ_DEMUCS_PYTHON), split into stems. Re-running is
safe: finished tracks are skipped, failed ones are retried.

Run it while the bot is stopped (both write dj_cache/index.json), or let the
running booth do the same work with "Prepare all" on a crate.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import urllib.request

import config  # noqa: F401  (loads .env)
import dj


def bot_is_running() -> bool:
    port = os.getenv('WEB_UI_PORT', '8722')
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2):
            return True
    except Exception:
        return False


def summary(service: dj.DJService, ids: set) -> dict:
    counts = {'ready': 0, 'stems': 0, 'working': 0, 'failed': 0}
    for track_id in ids:
        track = service.library.tracks.get(track_id)
        if not track:
            continue
        stage = track['job']['stage']
        stems = track['stems']['status']
        if stage == 'failed' or stems == 'failed':
            counts['failed'] += 1
        elif stage != 'ready' or stems in ('queued', 'running'):
            counts['working'] += 1
        else:
            counts['ready'] += 1
            if stems == 'ready':
                counts['stems'] += 1
    return counts


async def run(args) -> int:
    service = dj.DJService()
    stems = not args.no_stems
    if stems and not await service.stems_available():
        print('! Demucs not found — tracks will be prepared without stems. '
              'Set DJ_DEMUCS_PYTHON (see README) to split them.')
        stems = False

    ids = set()
    for link in args.links:
        try:
            result = await service.add_query(link, stems=False, prepare=False)
        except Exception as error:
            print(f'✗ {link}: {error}')
            continue
        crate = result.get('crate')
        label = f'crate “{crate["name"]}”' if crate else 'library'
        print(f'✓ {len(result["added"])} tracks → {label}')
        ids.update(result['added'])
    if not ids:
        return 1
    if args.metadata_only:
        service.touch()
        print('Imported metadata only; prepare the crates from the booth.')
        return 0

    for track_id in ids:
        track = service.library.tracks[track_id]
        if track['job']['stage'] == 'failed':
            track['job'].update(stage='idle', error=None)
        if track['stems']['status'] == 'failed':
            track['stems']['status'] = 'none'
    await service.start()
    for track_id in ids:
        await service.enqueue(track_id, stems=stems)
    service.touch()

    started = time.monotonic()
    last = None
    while True:
        counts = summary(service, ids)
        line = (f'{counts["ready"]}/{len(ids)} ready ({counts["stems"]} with stems), '
                f'{counts["working"]} in progress, {counts["failed"]} failed')
        if line != last:
            print(f'[{int(time.monotonic() - started):>5}s] {line}', flush=True)
            last = line
        if counts['working'] == 0:
            break
        await asyncio.sleep(3)
    await service.stop()
    service.touch()

    failed = [service.library.tracks[i] for i in ids
              if service.library.tracks[i]['job']['stage'] == 'failed'
              or service.library.tracks[i]['stems']['status'] == 'failed']
    for track in failed:
        error = track['job'].get('error') or track['stems'].get('error')
        print(f'  ✗ {track["title"]} — {track.get("artist") or ""}: {error}')
    print(f'Library: {dj.INDEX_FILE}')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('links', nargs='+', help='Spotify/YouTube links or searches')
    parser.add_argument('--no-stems', action='store_true', help='skip stem separation')
    parser.add_argument('--metadata-only', action='store_true',
                        help='only create crates; download nothing')
    parser.add_argument('--force', action='store_true',
                        help='run even though the bot seems to be running')
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR, format='%(levelname)s %(message)s')
    if bot_is_running() and not args.force:
        print('The bot is running. Use “Prepare all” in the booth instead, or stop the bot '
              '(or pass --force) — both would write the same library file.')
        sys.exit(2)
    sys.exit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
