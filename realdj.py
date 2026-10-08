"""Real DJ: a mode of the booth that performs a set instead of playing a queue.

Auto DJ plays the room's queue in order, each song start to finish, and only
the moment between two songs is clever. Real DJ works the way a club DJ does:

  * It reads each song's structure from its waveform and stems: 8-bar phrases
    with their energy, drums and vocals, which phrases are the big ones
    (chorus / drop), where the intro and breakdowns are.
  * It plays the good part of each song: in a phrase or two before the first
    big section, out after the second one (roughly one to three minutes).
  * It picks the next song from the whole crate (the room's queue), not the
    next in line: tempo the decks can beat-match, a key that fits, and the
    energy the set needs now (warm up, peak, a breather, peak again).
  * It has a repertoire and doesn't repeat itself: long EQ blends, stem
    swaps, mashups (one song's vocal over the other's instrumental), drum
    bridges for clashing keys, loop-roll drops, echo throws, and a tempo ride
    that walks the tempo toward the next song before mixing.
  * It plays the decks while a song runs: an echo throw on the last word of
    a vocal phrase, a short loop roll or filter sweep into a big section, a
    chorus hook stuttered two or three times with the drums cut.
  * With stems on both songs it plans by opportunity rather than by chance:
    it reads where each vocal sings and where the hooks are (half-beat vocal
    map), tries every move at every phrase, and plays the one the songs suit
    best. That includes routines that go back and forth: the new beat under
    the old song, one song's vocal over the other's music, the old song
    coming back, its hook looping into the new song's chorus.

Everything is done by moving the booth's own controls, so the DJ booth page
shows it happening, and a person can grab any control at any time (that
cancels the current transition, like in Auto DJ).
"""

import asyncio
import base64
import logging
import math
import random
import time

import numpy as np

import mixer
import stems

SR = 48_000                    # dj.SR (dj imports this module, so not from there)

logger = logging.getLogger("MusicBot.RealDJ")

PHRASE = 32                    # beats: 8 bars of 4/4
MIN_PLAY = 60.0                # seconds of each song, at least ...
MAX_PLAY = 170.0               # ... and at most
TEMPO_MATCH = 0.06             # beat-matchable without a tempo ride
TEMPO_RIDE = 0.15              # beyond this, no blend: a drop or echo out
RIDE_MAX = 6.0                 # percent the outgoing song may be pushed
SET_MINUTES = 75.0             # one energy arc (warm-up, peak, breather, peak)

# How much the DJ plays the decks. chill: long blends, few touches. club: a
# touch most phrases, loop rolls and drops with a riser / 808. hype: tricks
# all the time, drops over blends, the odd air horn.
VIBES = {
    "chill": {"flourish": 0.15, "drop_weight": 0.0, "cut": 0.0, "samples": False, "horn": False},
    "club": {"flourish": 0.5, "drop_weight": 1.5, "cut": 0.5, "samples": True, "horn": False},
    "hype": {"flourish": 0.8, "drop_weight": 3.5, "cut": 1.5, "samples": True, "horn": True},
}


# --------------------------------------------------------------------------
# Song structure
# --------------------------------------------------------------------------

def _wave_bands(deck):
    wave = deck.wave or {}
    try:
        return {name: np.frombuffer(base64.b64decode(wave[name]), dtype=np.uint8).astype(np.float32)
                for name in ("low", "mid", "high")}, float(wave.get("rate") or 20)
    except (KeyError, ValueError, TypeError):
        return None, 20.0


def structure(deck):
    """Phrases of a loaded deck and where to play it from and to.

    Cached on the deck; recomputed when its stems arrive (vocals and drums
    then come from the stems instead of the waveform's bands).
    """
    key = (deck.wave_id, deck.stems_status, deck.bpm)
    cached = getattr(deck, "_structure", None)
    if cached and cached[0] == key:
        return cached[1]
    result = _structure(deck)
    deck._structure = (key, result)
    return result


def _structure(deck):
    if not deck.loaded or not deck.bpm:
        return None
    bands, rate = _wave_bands(deck)
    beat = deck.beat_seconds
    phrase_s = PHRASE * beat
    count = int((deck.music_end() - deck.first_beat) // phrase_s)
    if count < 2 or bands is None:
        return None
    phrases = []
    for k in range(count):
        t0 = deck.time_of_beat(k * PHRASE)
        t1 = t0 + phrase_s
        i0, i1 = max(0, int(t0 * rate)), max(1, int(t1 * rate))
        low = float(bands["low"][i0:i1].mean()) if i1 > i0 else 0.0
        mid = float(bands["mid"][i0:i1].mean()) if i1 > i0 else 0.0
        high = float(bands["high"][i0:i1].mean()) if i1 > i0 else 0.0
        vocals = deck.stem_presence("vocals", t0, t1) if deck.has_stems else None
        drums = deck.stem_presence("drums", t0, t1) if deck.has_stems else None
        phrases.append({"beat": k * PHRASE, "start": t0, "energy": low + mid + high,
                        "low": low, "vocals": vocals, "drums": drums})
    peak = max(p["energy"] for p in phrases) or 1.0
    low_peak = max(p["low"] for p in phrases) or 1.0
    for p in phrases:
        p["energy"] /= peak
        p["low"] /= low_peak
        # A beat to mix over: drums from the stems, else the low band.
        p["beat_strength"] = p["drums"] if p["drums"] is not None else p["low"]
    energies = sorted(p["energy"] for p in phrases)
    # The loudest ~30% of phrases, and anything within 12% of the peak.
    cut = max(0.72, min(0.88, energies[int(len(energies) * 0.7)] - 1e-6))
    for p in phrases:
        p["big"] = p["energy"] >= cut
    big = [i for i, p in enumerate(phrases) if p["big"]] or [int(np.argmax([p["energy"] for p in phrases]))]
    first_big = big[0]
    # In: one phrase before the first big one, so the transition builds into it.
    entry = max(0, first_big - 1)
    if phrases[entry]["start"] > deck.duration * 0.5:
        entry = 0
    # Out: after the second big section (a run of big phrases is one section).
    sections, run = [], []
    for i in big:
        if run and i != run[-1] + 1:
            sections.append(run)
            run = []
        run.append(i)
    sections.append(run)
    after = [s for s in sections if s[-1] >= entry]
    exit_phrase = (after[1][-1] if len(after) > 1 else after[0][-1]) + 1
    exit_phrase = min(exit_phrase, count)
    start_t = phrases[entry]["start"]
    end_t = deck.time_of_beat(exit_phrase * PHRASE)
    # Keep each song between MIN_PLAY and MAX_PLAY seconds where it can.
    while end_t - start_t < MIN_PLAY and exit_phrase < count:
        exit_phrase += 1
        end_t = deck.time_of_beat(exit_phrase * PHRASE)
    while end_t - start_t > MAX_PLAY and exit_phrase - 1 > entry + 1:
        exit_phrase -= 1
        end_t = deck.time_of_beat(exit_phrase * PHRASE)
    return {"phrases": phrases, "big": big, "entry": entry, "exit": exit_phrase,
            "entry_beat": entry * PHRASE, "exit_beat": exit_phrase * PHRASE,
            "first_big_beat": first_big * PHRASE}


# --------------------------------------------------------------------------
# Choosing the next song
# --------------------------------------------------------------------------

def energy_target(minutes):
    """Where the set's energy should be: warm up, peak, a breather, peak again."""
    t = (minutes % SET_MINUTES) / SET_MINUTES
    return 0.5 + 0.32 * math.sin(math.pi * min(1.0, t * 1.6)) * (0.75 if 0.62 < t < 0.75 else 1.0)


def _artist(item):
    artist = (item.get("artist") or "").strip().lower()
    if artist:
        return artist
    title = (item.get("title") or "").lower()
    return title.split(" - ", 1)[0].strip() if " - " in title else ""


def score_candidate(meta, out_meta, target, recent_artists):
    """Lower is better. meta / out_meta: analysis dicts (bpm, camelot, energy)."""
    if not meta or not meta.get("bpm"):
        return 4.0
    ratio = mixer.tempo_ratio(out_meta.get("live_bpm") or out_meta.get("bpm"), meta.get("bpm"))
    gap = abs(math.log(ratio)) if ratio else 0.3
    tempo = gap * 6 if gap <= TEMPO_MATCH else 0.6 + gap * 8 if gap <= TEMPO_RIDE else 2.5
    steps = mixer.key_distance(out_meta.get("camelot"), meta.get("camelot"))
    key = (0.0, 0.15, 0.7)[steps] if steps < 3 else 1.1
    mood = abs((meta.get("energy") or 0.5) - target) * 2.2
    return tempo + key + mood


def pick_next(items, out_deck, minutes, history, rng=random):
    """Index of the crate item to play next (or None for an empty crate)."""
    if not items:
        return None
    out_meta = dict(out_deck.meta or {}, live_bpm=out_deck.live_bpm()) if out_deck else {}
    target = energy_target(minutes)
    played = {(h.get("title") or "").lower() for h in history[:40]}
    recent = [(h.get("artist") or "").lower() for h in history[:2]]
    best = None
    for index, item in enumerate(items[:100]):
        if (item.get("title") or "").lower() in played:
            continue
        meta = mixer.cache.get(*[k for k in item.get("keys") or [] if k])
        score = score_candidate(meta, out_meta, target, recent) if out_meta.get("bpm") else (
            abs(((meta or {}).get("energy") or 0.5) - target))
        if _artist(item) and _artist(item) in recent:
            score += 0.8
        if stems.enabled() and stems.lookup(item.get("keys") or []):
            score -= 0.25           # stems ready: the fancy transitions are possible
        score += index * 0.004 + rng.random() * 0.15   # a hint of the queue's order, a hint of chance
        if best is None or score < best[0]:
            best = (score, index)
    if best is None:                # everything was played already
        return 0
    return best[1]


# --------------------------------------------------------------------------
# Planning a transition
# --------------------------------------------------------------------------

class Plan:
    def __init__(self, style, beats, start_beat, in_cue, ride=0.0, why=""):
        self.script = None             # keyframes, for a routine
        self.label = None              # what the booth shows, for a routine
        self.move = style              # the move's name (routines: trade / tease)
        self.hooks = []                # (beat, length, repeats, word start s) on the old deck, to loop
        self.style = style
        self.beats = beats
        self.start_beat = start_beat   # on the outgoing deck
        self.in_cue = in_cue           # seconds into the incoming song
        self.ride = ride               # outgoing pitch (%) to walk to before mixing
        self.why = why

    def json(self):
        return {"style": self.style, "beats": self.beats, "start_beat": self.start_beat,
                "in_cue": round(self.in_cue, 2), "ride": round(self.ride, 2), "why": self.why,
                "label": self.label, "move": self.move,
                "hooks": [{"beat": b, "length": l, "repeats": r, "time": t} for b, l, r, t in self.hooks]}


DROP_STYLES = ("loop_roll", "echo", "cut")


def choose_style(out, inn, so, si, last_styles, rng=random, vibe="club"):
    """A style from the repertoire, from compatibility, energy, stems and vibe."""
    v = VIBES.get(vibe, VIBES["club"])
    ratio = mixer.tempo_ratio(out.live_bpm(), inn.bpm)
    gap = abs(math.log(ratio)) if ratio else 1.0
    keys_ok = mixer.key_distance(out.meta.get("camelot"), inn.meta.get("camelot")) <= 1
    both_stems = out.has_stems and inn.has_stems
    e_out = out.meta.get("energy") or 0.5
    e_in = inn.meta.get("energy") or 0.5
    options = []
    if gap > TEMPO_RIDE:
        options = [("echo", 3), ("loop_roll", 2), ("spinback", 1), ("brake", 1)]
        if e_in > e_out + 0.15:
            options = [("loop_roll", 4), ("echo", 2), ("cut", 1)]
    elif not keys_ok:
        options = [("drum_bridge", 5)] if both_stems else []
        options += [("filter", 3), ("echo", 2), ("loop_roll", 2 if e_in > e_out else 1)]
    else:
        if both_stems:
            vocal_out = None
            if so:
                tail = [p for p in so["phrases"] if so["exit_beat"] - 2 * PHRASE <= p["beat"] < so["exit_beat"]]
                vocal_out = max((p["vocals"] or 0) for p in tail) if tail else None
            options += [("stem_swap", 4), ("mashup", 3 if vocal_out and vocal_out > 0.25 else 1),
                        ("acapella", 2 if vocal_out and vocal_out > 0.3 else 0)]
        options += [("blend", 3), ("fade", 1 if vibe == "chill" else 0.5)]
        if e_in > e_out + 0.2:
            options += [("loop_roll", 2), ("filter", 2)]
        # Club and hype: drop the next song's big moment in, not just blend.
        if e_in >= 0.45:
            options += [("loop_roll", v["drop_weight"]), ("cut", v["cut"]),
                        ("echo", v["drop_weight"] * 0.4)]
    if vibe == "chill":
        options = [(s, w * (0.3 if s in ("loop_roll", "cut", "spinback", "brake") else 1.0))
                   for s, w in options]
    options = [(s, w) for s, w in options if w > 0]
    # Variety: what the last two transitions were weighs less.
    weighted = [(s, w * (0.25 if s in last_styles[-2:] else 1.0)) for s, w in options]
    total = sum(w for _, w in weighted)
    pick = rng.random() * total
    for style, weight in weighted:
        pick -= weight
        if pick <= 0:
            return style, gap
    return weighted[-1][0], gap


def plan_transition(out, inn, current_beat, last_styles, rng=random, vibe="club"):
    if out.has_stems and inn.has_stems:
        plan = plan_with_stems(out, inn, current_beat, last_styles, rng, vibe)
        if plan:
            return plan
    from dj import STYLES
    so, si = structure(out), structure(inn)
    style, gap = choose_style(out, inn, so, si, last_styles, rng, vibe)
    beats = STYLES[style][1]
    if style in ("echo", "cut", "brake", "spinback"):
        beats = 8 if style == "echo" else STYLES[style][1]
    # Where the new song comes in: blends build into its first big section;
    # drops land right on it.
    if si:
        target = si["first_big_beat"] if style in DROP_STYLES + ("brake", "spinback") else max(
            0, si["first_big_beat"] - beats)
        target = (target // 4) * 4
        in_cue = inn.time_of_beat(target)
        if in_cue > inn.duration * 0.5 or in_cue < 0:
            in_cue = inn.cue
    else:
        in_cue = inn.cue
    # Where the old song goes out: ending at its planned exit phrase.
    if so:
        exit_beat = so["exit_beat"]
        tail = beats if style not in ("echo", "cut", "brake", "spinback") else 0
        start = exit_beat - tail
        start = math.floor(start / 8) * 8
    else:
        end_beat = out.beat_at(out.music_end())
        start = math.floor((end_beat - beats) / 8) * 8
    if start <= current_beat + 4:
        start = math.ceil((current_beat + 8) / 8) * 8
    ride = 0.0
    if TEMPO_MATCH < gap <= TEMPO_RIDE and style not in DROP_STYLES + ("brake", "spinback"):
        # Walk the outgoing song part of the way so sync covers the rest.
        want = (inn.bpm / (out.bpm or inn.bpm))
        factor = min((0.5, 1.0, 2.0), key=lambda f: abs(math.log(want * f)))
        needed = (want * factor - 1) * 100
        ride = max(-RIDE_MAX, min(RIDE_MAX, needed * 0.6))
    why = f"{style}: tempo gap {gap * 100:.0f}%, keys {out.meta.get('camelot')}→{inn.meta.get('camelot')}"
    return Plan(style, beats, start, in_cue, ride, why)


# --------------------------------------------------------------------------
# Where the words are: a half-beat map from the stems, and the hooks
# --------------------------------------------------------------------------

def beat_profile(deck):
    """Vocal, drum and mix loudness every half beat, from the stems.

    {"vocals", "drums", "mix"}: float arrays, index i = beat i / 2, each scaled
    so its loud end (95th percentile) is 1, and "raw": the unscaled RMS of
    every part (drums, bass, vocals, other, mix) for predicting how loud a
    mix of the two songs will be. None without stems or a beat grid. Cached
    on the deck.
    """
    if not deck.has_stems or not deck.bpm:
        return None
    key = (deck.wave_id, deck.bpm, deck.first_beat, id(deck.stems))
    cached = getattr(deck, "_beat_profile", None)
    if cached and cached[0] == key:
        return cached[1]
    half = deck.beat_seconds / 2
    count = int((deck.music_end() - deck.first_beat) / half)
    if count < 32:
        return None
    vi, di, bi = (stems.NAMES.index(n) for n in ("vocals", "drums", "bass"))
    step = 24                          # 2 kHz is plenty for loudness
    total = min(len(deck.audio), deck.stems.shape[1])
    vocals = np.zeros(count, np.float32)
    drums = np.zeros(count, np.float32)
    bass = np.zeros(count, np.float32)
    mix = np.zeros(count, np.float32)
    for i in range(count):
        a = int(max(0.0, deck.time_of_beat(i / 2)) * SR)
        b = min(total, a + int(half * SR))
        if b - a < step * 4:
            continue
        for out, source in ((vocals, deck.stems[vi, a:b:step]), (drums, deck.stems[di, a:b:step]),
                            (bass, deck.stems[bi, a:b:step]), (mix, deck.audio[a:b:step])):
            x = source.astype(np.float32)
            out[i] = float(np.sqrt((x * x).mean()))
    # "Other" isn't stored: what's left of the mix (parts taken as unrelated).
    other = np.sqrt(np.maximum(0.0, mix ** 2 - drums ** 2 - bass ** 2 - vocals ** 2))
    result = {"raw": {"drums": drums, "bass": bass, "vocals": vocals, "other": other, "mix": mix}}
    for name, values in (("vocals", vocals), ("drums", drums), ("mix", mix)):
        loud = float(np.percentile(values, 95)) if values.any() else 0.0
        result[name] = np.clip(values / loud, 0.0, 1.5) if loud > 0 else values
    deck._beat_profile = (key, result)
    return result


def find_hooks(deck, s=None):
    """The words worth repeating: strong vocal entries after a breath.

    A hook starts on a beat (an even half-beat) where the vocal comes in loud
    right after a dip, scored up in big sections, on the bar's one, and in
    louder phrases. "time" is where the word itself starts (from the stem, not
    the grid) and length is 1 beat for a single word, 2 for a line. Best
    first, at least 8 beats apart: [{"beat", "time", "length", "score"}].
    """
    s = s or structure(deck)
    prof = beat_profile(deck)
    if not s or prof is None:
        return []
    key = (deck.wave_id, deck.bpm, deck.first_beat, id(deck.stems))
    cached = getattr(deck, "_hooks", None)
    if cached and cached[0] == key:
        return [dict(h) for h in cached[1]]
    v = prof["vocals"]
    phrases = s["phrases"]
    found = []
    for i in range(4, len(v) - 4, 2):
        beat = i // 2
        level = float(v[i])
        before = float(min(v[i - 1], v[i - 2]))
        onset = level - before
        if level < 0.55 or onset < 0.25:
            continue
        phrase = phrases[beat // PHRASE] if beat // PHRASE < len(phrases) else None
        if phrase is None:
            continue
        weight = 1.0 + (0.6 if phrase["big"] else 0.0) + 0.3 * phrase["energy"] + (0.25 if beat % 4 == 0 else 0.0)
        found.append({"beat": beat, "score": round(level * onset * weight, 4)})
    found.sort(key=lambda h: -h["score"])
    picked = []
    for hook in found:
        if all(abs(hook["beat"] - other["beat"]) >= 8 for other in picked):
            # Where the word really starts, and whether it's one word or a line.
            start, sung = _word(deck, hook["beat"])
            hook["time"] = round(start, 3)
            hook["length"] = 1 if sung < 0.85 * deck.beat_seconds else 2
            picked.append(hook)
        if len(picked) >= 16:
            break
    deck._hooks = (key, picked)
    return [dict(h) for h in picked]


def _word(deck, beat):
    """(start in seconds, sung length in seconds) of the word nearest `beat`,
    from the vocal stem at 10 ms: the first rise through 30% of the local peak
    within half a beat of the grid beat, and how long it holds above that.
    The analysed beat grid can be a little off; the word's own start isn't."""
    vi = stems.NAMES.index("vocals")
    bs = deck.beat_seconds
    t0 = max(0.0, deck.time_of_beat(beat - 0.5))
    a = int(t0 * SR)
    b = min(deck.stems.shape[1], int(deck.time_of_beat(beat + 2.5) * SR))
    hop = SR // 100
    x = deck.stems[vi, a:b, 0].astype(np.float32)
    frames = len(x) // hop
    if frames < 8:
        return deck.time_of_beat(beat), bs
    env = np.sqrt((x[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
    near = env[: int(2 * bs / 0.01)]
    threshold = 0.3 * float(near.max()) if near.size else 0.0
    if threshold <= 0:
        return deck.time_of_beat(beat), bs
    # The onset is the sharpest rise within half a beat of the grid beat (the
    # biggest jump over the 50 ms before it), whether the word comes out of
    # silence or out of a hum; it starts where that rise crosses 30% of the way up.
    last = min(frames - 1, int(bs / 0.01) + 1)
    if last <= 6:
        return deck.time_of_beat(beat), bs
    rises = [(float(env[k] - env[k - 5:k].min()), k) for k in range(5, last)]
    rise, peak_at = max(rises)
    floor = float(env[peak_at - 5:peak_at].min())
    if rise < 0.5 * (float(near.max()) - floor) or rise <= 0:
        return deck.time_of_beat(beat), bs
    onset = next(k for k in range(peak_at - 5, peak_at + 1) if env[k] >= floor + 0.3 * rise)
    threshold = max(threshold, floor + 0.3 * rise)
    end, quiet = onset + 3, 0
    while end < frames:
        quiet = quiet + 1 if env[end] < threshold else 0
        if quiet >= 5:
            end -= 4
            break
        end += 1
    return t0 + onset * 0.01, (end - onset) * 0.01


def _hook_in(hooks, lo, hi):
    """The best hook starting in [lo, hi) deck beats, or None."""
    inside = [h for h in hooks if lo <= h["beat"] < hi]
    return max(inside, key=lambda h: h["score"]) if inside else None


def vocal_coverage(prof, beat0, beat1, threshold=0.35):
    """How much of [beat0, beat1) someone is singing (0..1), from the half-beat map."""
    if prof is None:
        return 0.0
    v = prof["vocals"][max(0, int(beat0 * 2)):max(0, int(beat1 * 2))]
    return float((v > threshold).mean()) if len(v) else 0.0


def predicted_drop(script, beats, prof_out, prof_in, start_beat, cue_beat):
    """How far (dB, ≤ 0) a routine's quietest beat falls below what the old
    song was playing just before it, predicted from both songs' stem levels,
    the script's stem gains and the crossfader. Hook loops are left out (they
    repeat a sung word, so they only ever fill)."""
    raw_o, raw_i = prof_out["raw"], prof_in["raw"]
    before = raw_o["mix"][max(0, (start_beat - PHRASE) * 2):start_beat * 2]
    reference = float(np.median(before)) if len(before) else float(np.median(raw_o["mix"]))
    if reference <= 0:
        return 0.0
    levels = []
    for j in range(int(beats * 2)):
        values = dj_script_at(script, j / 2)
        q = values["xf"]
        g_out, g_in = math.cos(q * math.pi / 2), math.sin(q * math.pi / 2)
        io, ii = start_beat * 2 + j, cue_beat * 2 + j
        power = 0.0
        for raw, index, gain, side in ((raw_o, io, g_out, "out."), (raw_i, ii, g_in, "in.")):
            if 0 <= index < len(raw["mix"]):
                for stem in ("drums", "bass", "vocals", "other"):
                    power += (gain * values[side + stem] * float(raw[stem][index])) ** 2
        levels.append(math.sqrt(power))
    if len(levels) < 2:
        return 0.0
    # A beat at a time: a single quiet half-beat (a breath) is fine.
    beat_levels = np.convolve(np.array(levels), np.ones(2) / 2, mode="valid")
    worst = float(beat_levels.min())
    return min(0.0, 20 * math.log10(max(worst, 1e-6) / reference))


def loudness_score(drop):
    """Score for a predicted dip: nothing down to -6 dB (a build is meant to thin
    out), then a little per dB; real holes are rejected before this."""
    return 0.08 * min(0.0, drop + 6.0)


def dj_script_at(script, beat):
    from dj import script_at
    return script_at(script, beat)


# --------------------------------------------------------------------------
# Routines: scripted back and forth between the two songs (dj.ROUTINE)
# --------------------------------------------------------------------------

def _frame(at, ramp, **values):
    """A keyframe; out_drums=0 sets "out.drums"."""
    return {"at": at, "ramp": ramp, "set": {k.replace("_", ".", 1): v for k, v in values.items()}}


ALL_IN_OFF = dict(in_drums=0.0, in_bass=0.0, in_other=0.0, in_vocals=0.0)
ALL_OUT_ON = dict(out_drums=1.0, out_bass=1.0, out_other=1.0, out_vocals=1.0)


def trade_script(variant, acappella_at=None):
    """Back and forth over three phrases, landing the new song on the fourth.

      phrase 1  the new song's drums take over under the old song
      phrase 2  "out": the old vocal rides the new song's music
                "in":  the new vocal rides the old song's music
      phrase 3  the old song comes back on its own, then builds into the new
                song: from `acappella_at` (the bar of its hook, which the
                plan loops) it goes a cappella over the new drums and bass
                while the new music rises. Without a hook to sing, the two
                songs' music layer instead (drums and bass handed over first).
      phrase 4  the new song lands whole, the old one is gone

    Beats are from the start of the routine. While the old song plays alone the
    crossfader is all the way over to it, so it doesn't lose 3 dB.
    """
    P = PHRASE
    frames = [_frame(0, 2, xf=0.5, in_drums=1.0, out_drums=0.0)]
    if variant == "out":
        frames.append(_frame(P - 1, 1, in_bass=1.0, in_other=1.0, out_bass=0.0, out_other=0.0))
    else:
        frames.append(_frame(P - 1, 1, in_vocals=1.0, out_vocals=0.0))
    frames.append(_frame(2 * P - 1, 1, xf=0.0, **ALL_OUT_ON, **ALL_IN_OFF))
    if acappella_at is not None:
        build = int(acappella_at)
        frames += [
            _frame(build - 1, 1, xf=0.5, out_drums=0.0, out_bass=0.0, out_other=0.0,
                   in_drums=1.0, in_bass=1.0, in_other=0.35),
            _frame(build, max(2, 3 * P - 2 - build), in_other=0.8),
        ]
    else:
        build = 2 * P + 16
        frames += [
            _frame(build - 1, 1, xf=0.5, out_drums=0.0, out_bass=0.0, in_drums=1.0, in_bass=1.0),
            _frame(build, 12, in_other=0.6, out_other=0.5),
        ]
    frames += [
        _frame(3 * P - 1, 1, in_other=1.0, in_vocals=1.0, out_vocals=0.0, out_other=0.0),
        _frame(3 * P, 2, xf=1.0),
    ]
    return frames, 3 * P + 2


def tease_script(clash, acappella_at=16):
    """A hook tease over one phrase: the new drums come in, then from the bar of
    the old song's hook (which the plan loops) it goes a cappella over them,
    and the new song drops whole on the next phrase. With clashing keys only
    the new drums play under the vocal."""
    P = PHRASE
    at = int(acappella_at)
    frames = [
        _frame(0, 2, xf=0.5, in_drums=1.0, out_drums=0.0),
        _frame(at - 1, 1, out_bass=0.0, out_other=0.0, in_bass=0.0 if clash else 1.0,
               in_other=0.0 if clash else 0.35),
    ]
    if not clash:
        frames.append(_frame(at, max(2, P - 2 - at), in_other=0.8))
    frames += [
        _frame(P - 1, 1, in_bass=1.0, in_other=1.0, in_vocals=1.0, out_vocals=0.0),
        _frame(P, 2, xf=1.0),
    ]
    return frames, P + 2


ROUTINE_LABELS = {"trade": "Back and forth", "tease": "Hook tease"}

# How much each vibe likes each move (multiplies its score).
TASTE = {
    "chill": {"trade": 0.85, "tease": 0.5, "mashup": 1.2, "stem_swap": 1.3, "acapella": 1.0,
              "drum_bridge": 1.0, "drop": 0.12, "blend": 1.0},
    "club": {"trade": 1.15, "tease": 1.1, "mashup": 1.0, "stem_swap": 0.95, "acapella": 0.9,
             "drum_bridge": 1.0, "drop": 0.75, "blend": 0.7},
    "hype": {"trade": 1.0, "tease": 1.45, "mashup": 0.9, "stem_swap": 0.8, "acapella": 0.85,
             "drum_bridge": 1.0, "drop": 1.25, "blend": 0.5},
}


def _vocal(s, phrase):
    phrases = s["phrases"]
    return float(phrases[phrase]["vocals"] or 0) if 0 <= phrase < len(phrases) else 0.0


def _landing(si, offset):
    """(cue phrase, lands on a big phrase) for the new song, so that `offset`
    phrases after its cue it is in a big section (its first one it can reach)."""
    phrases = si["phrases"]
    half = len(phrases) // 2 + 1
    for index, phrase in enumerate(phrases):
        if phrase["big"] and index - offset >= 0 and index - offset <= half:
            return index - offset, True
    return max(0, min(si["entry"], half)), False


def plan_with_stems(out, inn, current, last_styles, rng=random, vibe="club"):
    """The best move for two songs that both have stems, by opportunity.

    Every move is tried at every phrase the old song could start it on, and
    scored on what the songs actually do there: whose vocal is singing, where
    the new song's big section lands, where the hooks are, the keys and
    tempos, the energy and the vibe; recent moves count a little against.
    Returns a Plan, or None when the songs give it nothing to work with.
    """
    so, si = structure(out), structure(inn)
    if not so or not si or not out.bpm or not inn.bpm:
        return None
    from dj import ROUTINE, STYLES
    taste = TASTE.get(vibe, TASTE["club"])
    ratio = mixer.tempo_ratio(out.live_bpm(), inn.bpm)
    gap = abs(math.log(ratio)) if ratio else 1.0
    if gap > TEMPO_RIDE:
        return None                      # the classic drops handle a tempo cliff
    keys = mixer.key_distance(out.meta.get("camelot"), inn.meta.get("camelot"))
    clash = keys > 1
    lift = (inn.meta.get("energy") or 0.5) - (out.meta.get("energy") or 0.5)
    hooks = find_hooks(out, so)
    top_hook = hooks[0]["score"] if hooks else 1.0
    prof_out, prof_in = beat_profile(out), beat_profile(inn)
    P = PHRASE
    count = len(so["phrases"])
    first_big = so["big"][0] if so["big"] else so["entry"]
    earliest = max(int(math.ceil((current + 6) / P)), first_big + 1)
    ideal_end = so["exit"]
    candidates = []

    def consider(move, start, overlap, landing, base, why, **extra):
        if start < earliest or start + overlap > count:
            return
        cue, on_big = _landing(si, landing)
        score = base * taste.get(move if move in taste else "drop", 1.0)
        score += 0.35 if on_big else -0.3
        score -= 0.12 * abs(start + overlap - ideal_end)
        if move in last_styles[-2:]:
            score *= 0.6
        score += rng.random() * 0.2
        candidates.append((score, move, start, cue, why, extra))

    for start in range(earliest, count):
        b0 = start * P
        # How much the old song actually sings: in the phrase it starts on, and the next.
        sing0 = vocal_coverage(prof_out, b0, b0 + P)
        sing1 = vocal_coverage(prof_out, b0 + P, b0 + 2 * P)
        if not clash:
            # Back and forth. The build into the landing goes a cappella on a hook.
            build_hook = _hook_in(hooks, b0 + 2 * P + 8, b0 + 3 * P - 6)
            hook_bonus = 0.45 * (build_hook["score"] / top_hook) if build_hook else -0.1
            cue, _ = _landing(si, 3)
            music_in = si["phrases"][cue + 1]["energy"] if cue + 1 < len(si["phrases"]) else 0.0
            trade = None
            if sing1 >= 0.45 and music_in >= 0.35:
                trade = ("out", 1.4 + 0.7 * sing1 + 0.4 * music_in,
                         "back and forth: the old vocal rides the new music, the old song comes back, "
                         "then builds into the new one's chorus")
            else:
                sing_in = vocal_coverage(prof_in, (cue + 1) * P, (cue + 2) * P)
                if sing_in >= 0.45:
                    trade = ("in", 1.3 + 0.7 * sing_in,
                             "back and forth: the new vocal rides the old song's music, the old song "
                             "comes back, then builds into the new one's chorus")
            if trade:
                variant, base, why = trade
                acappella_at = (build_hook["beat"] - b0) // 4 * 4 if build_hook else None
                script = trade_script(variant, acappella_at)
                drop = predicted_drop(*script, prof_out, prof_in, b0, cue * P)
                if drop > -14:
                    consider("trade", start, 3, 3, base + hook_bonus + loudness_score(drop), why,
                             variant=variant, hook=build_hook, script=script)
            consider("mashup", start, 2, 2, 1.0 + 0.9 * (sing0 + sing1) / 2,
                     "mashup: the old vocal over the new instrumental")
            consider("acapella", start, 1, 1, 0.8 + 0.9 * sing0, "the old vocal a cappella over the new song")
            consider("stem_swap", start, 1, 1, 1.0 + 0.6 * (1 - sing0),
                     "stem swap: the parts change hands one by one")
        else:
            consider("drum_bridge", start, 1, 1, 1.4, "keys clash: only the drums overlap")
        tease_hook = _hook_in(hooks, b0 + 8, b0 + P - 6)
        if tease_hook:
            cue, _ = _landing(si, 1)
            script = tease_script(clash, (tease_hook["beat"] - b0) // 4 * 4)
            drop = predicted_drop(*script, prof_out, prof_in, b0, cue * P)
            if drop > -14:
                consider("tease", start, 1, 1,
                         1.2 + 0.9 * tease_hook["score"] / top_hook + (0.2 if clash else 0.0) + loudness_score(drop),
                         "hook tease: the old song's hook loops over the new drums, then the drop",
                         hook=tease_hook, script=script)
        if lift > 0.15 or vibe == "hype":
            consider("loop_roll", start, 0, 0, 0.5 + 1.5 * max(0.0, lift), "loop roll into the new song's big moment")
    if not candidates:
        return None
    score, move, start, cue, why, extra = max(candidates, key=lambda c: c[0])
    start_beat = start * P
    hook = extra.get("hook")
    if move in ("trade", "tease"):
        script, beats = extra["script"]
        hooks_to_loop = []
        if hook:
            reps = 3 if hook["length"] == 1 else 2
            last = start_beat + (3 * P - 2 if move == "trade" else P - 2)
            # Loop until the bar before the landing at the latest.
            while reps > 1 and hook["beat"] + reps * hook["length"] > last:
                reps -= 1
            if reps > 1:
                hooks_to_loop.append((hook["beat"], hook["length"], reps, hook["time"]))
        plan = Plan(ROUTINE, beats, start_beat, inn.time_of_beat(cue * P), why=why)
        plan.script, plan.label, plan.move, plan.hooks = script, ROUTINE_LABELS[move], move, hooks_to_loop
    elif move == "loop_roll":
        cue_beat = si["first_big_beat"]
        plan = Plan("loop_roll", STYLES["loop_roll"][1], start_beat - STYLES["loop_roll"][1],
                    inn.time_of_beat(cue_beat), why=why)
        plan.move = move
    else:
        beats = STYLES[move][1]
        plan = Plan(move, beats, start_beat, inn.time_of_beat(cue * P), why=why)
        plan.move = move
    if TEMPO_MATCH < gap <= TEMPO_RIDE and plan.style != "loop_roll":
        want = inn.bpm / (out.bpm or inn.bpm)
        factor = min((0.5, 1.0, 2.0), key=lambda f: abs(math.log(want * f)))
        plan.ride = max(-RIDE_MAX, min(RIDE_MAX, (want * factor - 1) * 100 * 0.6))
    plan.why = f"{why} (keys {out.meta.get('camelot')}→{inn.meta.get('camelot')}, score {score:.2f})"
    return plan


# --------------------------------------------------------------------------
# The performer
# --------------------------------------------------------------------------

class RealDJ:
    """Drives a dj.DJSession like a DJ would. tick() runs every 0.25 s."""

    def __init__(self, session, rng=None, vibe="club"):
        self.session = session
        self.rng = rng or random.Random()
        self.vibe = vibe if vibe in VIBES else "club"
        self.horn_at = -9                # transitions since the last air horn
        self.touches = []                # flourishes played (for the booth / tests)
        self.started_at = time.monotonic()
        self.plan = None
        self.plan_for = None           # (deck name, wave id) the plan is for
        self.styles = []               # recent transition styles
        self.flourish_at = {}          # deck name -> last phrase given a flourish
        self.next_title = None
        self.ride_from = None

    def minutes(self):
        return (time.monotonic() - self.started_at) / 60

    def state(self):
        return {"plan": self.plan.json() if self.plan else None, "next": self.next_title,
                "vibe": self.vibe,
                "energy_target": round(energy_target(self.minutes()), 2)}

    async def tick(self):
        from dj import Transition, _xfader_toward, DECKS
        session = self.session
        engine = session.engine
        decks = engine.decks
        with engine.lock:
            playing = [d for d in decks.values() if d.playing]
            tr = engine.transition
        if not playing:
            await self._start_cold()
            return
        with engine.lock:
            m = engine.master() or playing[0]
            other = decks["B" if m.name == "A" else "A"]
        if tr:
            return
        if other.playing:
            return
        if (not other.loaded or other.ended) and not session._loading(other.name):
            if m.seconds > 3:
                await self._load_next(other, m)
            return
        if not other.loaded or session._loading(other.name):
            return
        plan_key = (other.name, other.wave_id, other.has_stems, m.has_stems)
        fresh = None
        if self.plan_for != plan_key or self.plan is None:
            # Reading two songs' stems takes a moment; plan off the audio
            # thread's lock and apply the result under it.
            current = m.beat_at(m.seconds) if m.bpm else 0
            fresh = await asyncio.get_running_loop().run_in_executor(
                None, plan_transition, m, other, current, list(self.styles), self.rng, self.vibe)
        with engine.lock:
            if fresh is not None and (other.wave_id, other.has_stems, m.has_stems) != plan_key[1:]:
                fresh = None             # the decks changed while planning: next tick plans again
            if fresh is not None:
                self.plan = fresh
                self.plan_for = plan_key
                self.ride_from = None
                other.cue = other.snap(self.plan.in_cue, force=True) if other.bpm else self.plan.in_cue
                other.seek(other.cue)
                ratio = mixer.tempo_ratio(m.live_bpm(), other.bpm) if (m.bpm and other.bpm) else None
                other.sync = bool(ratio and abs(ratio - 1) <= 0.08 + RIDE_MAX / 100)
                logger.info("Real DJ %s: next %r via %s at beat %s (cue %.1fs)%s", session.key,
                            other.info.get("title"), self.plan.style, self.plan.start_beat,
                            other.cue, f", tempo ride {self.plan.ride:+.1f}%" if self.plan.ride else "")
            if self.plan is None or self.plan_for != plan_key:
                return                   # still planning for these decks
            plan = self.plan
            if not m.bpm:
                left = (m.music_end() - 12 - m.seconds) / max(0.05, m.base_rate())
                if left <= 0:
                    engine.transition = Transition(m, other, "fade", 16, None)
                    self._started(plan, "fade")
                return
            current = m.beat_at(m.seconds)
            if m.loop_on:
                return
            self._ride(m, plan, current)
            self._flourish(m, plan, current)
            seconds_left = (plan.start_beat - current) * m.beat_seconds / max(0.05, m.base_rate())
            if seconds_left < 1.2:
                engine.transition = Transition(m, other, plan.style, plan.beats, plan.start_beat)
                engine.transition.samples = self._transition_samples(plan.style)
                if plan.script:
                    engine.transition.script = plan.script
                    engine.transition.label = plan.label
                for beat, length, repeats, at in plan.hooks:
                    self._hook_loop(m, beat, length, repeats, at=at)
                    self.touches = (self.touches + ["hook_repeat"])[-20:]
                self._started(plan, plan.move)
                self._schedule_horn(other)

    def _transition_samples(self, style):
        """Sampler hits for a transition: a riser into a drop, an 808 on it."""
        if not VIBES[self.vibe]["samples"]:
            return {}
        if style == "loop_roll":
            return {"start": "riser", "drop": "boom"}
        if style == "cut":
            return {"drop": "boom"}
        if style == "spinback":
            return {"start": "rewind"}
        if style == "echo" and self.vibe == "hype":
            return {"drop": "clap"}
        return {}

    def _schedule_horn(self, inn):
        """Hype: now and then an air horn as the new song's big section hits."""
        self.horn_at += 1
        if not VIBES[self.vibe]["horn"] or self.horn_at < 3 or self.rng.random() > 0.5:
            return
        s = structure(inn)
        if not s:
            return
        self.horn_at = 0
        engine = self.session.engine
        beat = s["first_big_beat"]
        if inn.bpm and inn.beat_at(inn.cue) > beat - 1:
            later = [p["beat"] for p in s["phrases"] if p["big"] and p["beat"] > inn.beat_at(inn.cue) + 8]
            if not later:
                return
            beat = later[0]
        engine.schedule(inn.name, beat, beat + 0.5, lambda: self._sample("horn"), lambda: None)

    def _sample(self, name):
        self.session.engine.play_sample(name)

    def _started(self, plan, style):
        self.styles = (self.styles + [style])[-6:]
        logger.info("Real DJ %s: %s", self.session.key, plan.why)
        self.plan = None
        self.next_title = None

    async def _start_cold(self):
        """Nothing on air: play a loaded deck from its entry, else load one."""
        from dj import _xfader_toward, DECKS
        session = self.session
        engine = session.engine
        ready = next((d for d in engine.decks.values() if d.loaded and not d.ended), None)
        if ready:
            with engine.lock:
                s = structure(ready)
                # Still parked where loading cued it (its first downbeat): start
                # it where a DJ would.
                if s and ready.seconds <= ready.cue + 0.5:
                    # Open the set with something to hear: past a near-silent
                    # intro, but still before the first big section.
                    entry = s["entry"]
                    while entry < s["big"][0] and s["phrases"][entry]["energy"] < 0.35:
                        entry += 1
                    ready.seek(ready.time_of_beat(entry * PHRASE))
                ready.playing = True
                ready.fader = 1.0
                engine.xfader = _xfader_toward(ready.xf if ready.xf in ("A", "B") else ready.name, 1.0)
                engine.master_name = ready.name
            return
        if any(session._loading(d) for d in DECKS):
            return
        items = session.crate.items()
        index = pick_next(items, None, self.minutes(), session.history, self.rng)
        if index is None:
            return
        item = await session.crate.take(index)
        if item:
            session.load_in_background("A", item, play=False)

    async def _load_next(self, deck, out):
        session = self.session
        items = session.crate.items()
        index = pick_next(items, out, self.minutes(), session.history, self.rng)
        if index is None:
            item = await session._next_item()       # Discord: Smart Autoplay
        else:
            item = await session.crate.take(index)
        if item:
            self.next_title = item.get("title")
            session.load_in_background(deck.name, item)

    def _ride(self, m, plan, current):
        """Walk the outgoing tempo toward the next song over the last 32 beats."""
        if not plan.ride:
            return
        begin = plan.start_beat - PHRASE
        if current < begin:
            return
        if self.ride_from is None:
            self.ride_from = m.pitch
            m.sync = False
        q = min(1.0, (current - begin) / PHRASE)
        m.pitch = self.ride_from + (plan.ride - self.ride_from) * q

    def _flourish(self, m, plan, current):
        """A touch on the playing song at the end of a phrase (how often: vibe)."""
        s = structure(m)
        if not s or current < PHRASE:
            return
        phrase = int(current // PHRASE)
        if self.flourish_at.get((m.name, m.wave_id), -9) >= phrase:
            return
        boundary = (phrase + 1) * PHRASE
        if boundary >= plan.start_beat - 4 or boundary - current > 6 or boundary - current < 2.5:
            return
        self.flourish_at[(m.name, m.wave_id)] = phrase
        v = VIBES[self.vibe]
        if self.rng.random() > v["flourish"]:
            return
        phrases = s["phrases"]
        here = phrases[phrase] if phrase < len(phrases) else None
        nxt = phrases[phrase + 1] if phrase + 1 < len(phrases) else None
        engine = self.session.engine
        if engine.fx.on or engine.transition or m.loop_on:
            return
        into_big = bool(nxt and nxt["big"] and not (here and here["big"]))
        sung = bool(here and (here["vocals"] or 0) > 0.25)
        calm = bool(nxt and not nxt["big"] and (nxt["beat_strength"] or 0) < 0.45)
        options = []
        hook = None
        if nxt and nxt["big"] and m.has_stems and self.vibe != "chill":
            hook = _hook_in(find_hooks(m, s), boundary + 1, min(boundary + PHRASE - 4, plan.start_beat - 8))
        if hook:
            options += [("hook_repeat", 4 if self.vibe == "hype" else 3)]
        if into_big:
            options += [("roll", 3), ("slip_loop", 3), ("sweep", 1)]
            if v["samples"]:
                options += [("riser", 2)]
        if sung:
            options += [("echo", 3)]
        if calm and self.vibe != "chill":
            options += [("flanger", 2)]
        options += [("sweep", 1.5), ("slip_loop", 1 if self.vibe != "chill" else 0)]
        options = [(k, w) for k, w in options if w > 0]
        total = sum(w for _, w in options)
        pick = self.rng.random() * total
        kind = options[-1][0]
        for name, weight in options:
            pick -= weight
            if pick <= 0:
                kind = name
                break
        if kind == "hook_repeat":
            self._hook_loop(m, hook["beat"], hook["length"], 3 if hook["length"] == 1 else 2,
                            drop_drums=True, at=hook.get("time"))
        else:
            getattr(self, "_fl_" + kind)(m, boundary, nxt)
        self.touches = (self.touches + [kind])[-20:]
        logger.info("Real DJ: %s at the end of a phrase in %r", kind.replace("_", " "), m.info.get("title"))

    def _fx(self, m, kind, beat, level):
        engine = self.session.engine
        fx = engine.fx

        def on():
            fx.set_on(False, engine.clock()[0])
            fx.type, fx.beat, fx.level, fx.target = kind, beat, level, m.name
            fx.set_on(True, engine.clock()[0])

        def off():
            fx.set_on(False, engine.clock()[0])
        return on, off

    def _fl_roll(self, m, boundary, nxt):
        on, off = self._fx(m, "roll", 2, 1.0)            # half-beat roll on the last beat
        self.session.engine.schedule(m.name, boundary - 1, boundary, on, off)

    def _fl_echo(self, m, boundary, nxt):
        on, off = self._fx(m, "echo", 3, 0.7)            # throw the last word
        self.session.engine.schedule(m.name, boundary - 1, boundary - 0.25, on, off)

    def _fl_flanger(self, m, boundary, nxt):
        on, off = self._fx(m, "flanger", 6, 0.55)        # into a calm phrase
        self.session.engine.schedule(m.name, boundary, boundary + 8, on, off)

    def _fl_sweep(self, m, boundary, nxt):
        def on():
            m.color_fx = "filter"
            m.color = 0.45

        def off():
            m.color = 0.0
        self.session.engine.schedule(m.name, boundary - 2, boundary, on, off)

    def _fl_riser(self, m, boundary, nxt):
        engine = self.session.engine
        engine.schedule(m.name, boundary - 8, boundary - 7.5, lambda: self._sample("riser"), lambda: None)
        engine.schedule(m.name, boundary, boundary + 0.5, lambda: self._sample("boom"), lambda: None)

    def _hook_loop(self, deck, beat, length, repeats, drop_drums=False, at=None):
        """Loop `length` beats from the word at `at` seconds (else from grid
        beat `beat`) `repeats` times, then carry on where the song would have
        been (slip), so it stays on the phrase. With drop_drums the drums cut
        out while the word repeats. Armed half a beat early, so a grid that's
        a little late still catches the word from its start."""
        engine = self.session.engine
        state = {}

        def off():
            deck.loop_on = False
            deck.end_slip()
            deck.slip = state.get("slip", False)
            if "drums" in state:
                deck.stem_gain["drums"] = state["drums"]

        def on():
            state["slip"] = deck.slip
            deck.slip = True
            deck.shadow = deck.pos
            deck.loop_in = at if at is not None else deck.time_of_beat(beat)
            deck.loop_out = deck.loop_in + length * deck.beat_seconds
            deck.loop_on = True
            rate = max(0.05, deck.base_rate())
            lead_in = max(0.0, deck.loop_in - deck.seconds)
            if drop_drums and deck.has_stems and engine.transition is None:
                state["drums"] = deck.stem_gain["drums"]
                engine.schedule_after(lead_in / rate, lambda: deck.stem_gain.update(drums=0.0))
            # Inside the loop the deck's beat stands still, so the end is timed:
            # the way in to the word, then the repeats.
            engine.schedule_after((lead_in + repeats * length * deck.beat_seconds) / rate, off)
        engine.schedule(deck.name, beat - 0.5, beat + 0.5, on, lambda: None)

    def _fl_slip_loop(self, m, boundary, nxt):
        """A stutter: the last two beats loop on half a beat, then the song
        carries on where it would have been (slip), right on the one."""
        state = {}

        engine = self.session.engine

        def off():
            m.loop_on = False
            m.end_slip()
            m.slip = state.get("slip", False)

        def on():
            state["slip"] = m.slip
            m.slip = True
            m.shadow = m.pos
            m.loop_in = m.time_of_beat(boundary - 2)
            m.loop_out = m.loop_in + 0.5 * m.beat_seconds
            m.loop_on = True
            # Inside the loop the deck's beat stands still, so the end is
            # timed: two beats of real time from here.
            engine.schedule_after(2 * m.beat_seconds / max(0.05, m.base_rate()), off)
        engine.schedule(m.name, boundary - 2, boundary - 1.9, on, lambda: None)
