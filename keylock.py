"""Key lock for the DJ decks: change a song's tempo without changing its pitch.

A deck's pitch fader is varispeed, like vinyl: 4% faster is also most of a
semitone sharper, which is what makes two songs in matching keys sound sour
while they are beat-matched. With key lock on, a deck that is not at its own
tempo is played through Rubber Band's real-time stretcher instead (the
library ffmpeg's rubberband filter uses, loaded here directly), so the tempo
moves and the key stays.

The deck keeps counting its playhead the way it always did. The stretcher has
a lead: it is fed the song a little ahead of what is heard (DeckStretch.cur),
and is primed with the audio just before the playhead so its first block is
the playhead's.

Nothing here is needed to run the booth: without the library available() is
False and decks stay varispeed.
"""

import ctypes
import ctypes.util
import logging

import numpy as np

logger = logging.getLogger("MusicBot.KeyLock")

SR = 48_000
# RubberBandOptions (rubberband-c.h)
REALTIME = 0x00000001
THREADING_NEVER = 0x00010000
CHANNELS_TOGETHER = 0x10000000
ENGINE_FINER = 0x20000000
# The finer engine (Rubber Band 3): the faster one bends sustained notes (a 55 Hz bass note
# came out 3.5% sharp at 3.5% faster, as if there were no key lock), this one holds them
# exactly and keeps the beat within a millisecond, for about a tenth of a core per deck.
OPTIONS = REALTIME | THREADING_NEVER | CHANNELS_TOGETHER | ENGINE_FINER
#: Rates the stretcher is asked for; outside it the deck is varispeed (a brake, a spinback, a scratch).
MIN_RATE, MAX_RATE = 0.5, 2.0
#: A deck this close to its own tempo plays plainly: 0.3% is 5 cents, and sync's phase nudges live here.
ENGAGE = 0.003
RELEASE = 0.001
RELEASE_BLOCKS = 25              # this many blocks back at its own tempo before the stretcher lets go
FEED = 512                       # samples fed at a time

_lib = None
_tried = False


def _load():
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    for name in (ctypes.util.find_library("rubberband"), "librubberband.so.2", "librubberband.so.3",
                 "librubberband.dylib"):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        try:
            lib.rubberband_new.restype = ctypes.c_void_p
            lib.rubberband_new.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_double, ctypes.c_double]
            lib.rubberband_delete.argtypes = [ctypes.c_void_p]
            lib.rubberband_reset.argtypes = [ctypes.c_void_p]
            lib.rubberband_set_time_ratio.argtypes = [ctypes.c_void_p, ctypes.c_double]
            lib.rubberband_get_latency.restype = ctypes.c_uint
            lib.rubberband_get_latency.argtypes = [ctypes.c_void_p]
            lib.rubberband_set_max_process_size.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            lib.rubberband_get_samples_required.restype = ctypes.c_uint
            lib.rubberband_get_samples_required.argtypes = [ctypes.c_void_p]
            lib.rubberband_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
            lib.rubberband_available.restype = ctypes.c_int
            lib.rubberband_available.argtypes = [ctypes.c_void_p]
            lib.rubberband_retrieve.restype = ctypes.c_uint
            lib.rubberband_retrieve.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
            for optional in ("rubberband_get_preferred_start_pad", "rubberband_get_start_delay"):
                fn = getattr(lib, optional, None)
                if fn is not None:
                    fn.restype = ctypes.c_uint
                    fn.argtypes = [ctypes.c_void_p]
        except AttributeError:
            continue
        _lib = lib
        break
    if _lib is None:
        logger.info("Rubber Band is not installed: decks change pitch with tempo (no key lock)")
    return _lib


def available():
    return _load() is not None


class Stretcher:
    """One real-time Rubber Band stretcher, stereo, 48 kHz."""

    def __init__(self, rate=1.0):
        lib = _load()
        if lib is None:
            raise RuntimeError("Rubber Band is not installed.")
        self.lib = lib
        self.state = lib.rubberband_new(SR, 2, OPTIONS, 1.0 / rate, 1.0)
        if not self.state:
            raise RuntimeError("Rubber Band did not start.")
        lib.rubberband_set_max_process_size(self.state, FEED)
        self.rate = rate
        self._pointers = (ctypes.c_void_p * 2)()

    def close(self):
        if self.state:
            self.lib.rubberband_delete(self.state)
            self.state = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def reset(self):
        self.lib.rubberband_reset(self.state)

    def set_rate(self, rate):
        if rate != self.rate:
            self.rate = rate
            self.lib.rubberband_set_time_ratio(self.state, 1.0 / rate)

    def start_pad(self):
        fn = getattr(self.lib, "rubberband_get_preferred_start_pad", None)
        return int(fn(self.state)) if fn is not None else int(self.lib.rubberband_get_latency(self.state))

    def start_delay(self):
        fn = getattr(self.lib, "rubberband_get_start_delay", None)
        return int(fn(self.state)) if fn is not None else int(self.lib.rubberband_get_latency(self.state))

    def required(self):
        return int(self.lib.rubberband_get_samples_required(self.state))

    def available(self):
        return int(self.lib.rubberband_available(self.state))

    def process(self, block):
        """Feed (n, 2) float32."""
        planar = np.ascontiguousarray(block.T, dtype=np.float32)
        self._pointers[0] = planar[0].ctypes.data
        self._pointers[1] = planar[1].ctypes.data
        self.lib.rubberband_process(self.state, self._pointers, len(block), 0)

    def retrieve(self, n):
        """Up to n samples as (n, 2) float32."""
        planar = np.empty((2, n), np.float32)
        self._pointers[0] = planar[0].ctypes.data
        self._pointers[1] = planar[1].ctypes.data
        got = int(self.lib.rubberband_retrieve(self.state, self._pointers, n))
        return planar[:, :got].T


class DeckStretch:
    """A deck's key lock: decides when the stretcher plays, primes it, feeds it.

    read() is given the deck's playhead, the rate for this block and a source,
    `source(index, count) -> (count, 2) float32` (the deck's audio from an
    integer sample, with its stems mixed), and returns the block, or None when
    the deck should play plainly this block.
    """

    def __init__(self):
        self.stretcher = None
        self.active = False
        self.cur = 0                  # next source sample to feed
        self.expected = None          # where the playhead should be at the next block, if nothing jumped
        self.calm = 0                 # blocks in a row at (nearly) the deck's own tempo
        self.fresh = False            # the block just returned is the first after priming
        self.tail = None              # the stretcher's last block when it lets go, to fade into the plain deck

    def warm(self):
        """Build the stretcher ahead of time (it takes a few milliseconds the audio thread should not spend)."""
        if self.stretcher is None and available():
            try:
                self.stretcher = Stretcher()
            except Exception:
                logger.debug("Key lock could not start", exc_info=True)

    def stop(self):
        """Let go of the deck (it plays plainly, or has jumped: the next block primes afresh)."""
        self.active = False
        self.expected = None
        self.calm = 0

    def wanted(self, rate):
        """Whether this block is the stretcher's (engages past ENGAGE, lets go after a while back at RELEASE)."""
        if not MIN_RATE <= rate <= MAX_RATE:
            return False
        off = abs(rate - 1.0)
        if not self.active:
            return off > ENGAGE
        self.calm = self.calm + 1 if off < RELEASE else 0
        return self.calm < RELEASE_BLOCKS

    def _prime(self, pos, rate, source):
        if self.stretcher is None:
            self.stretcher = Stretcher(rate)
        else:
            self.stretcher.reset()
            self.stretcher.set_rate(rate)
        st = self.stretcher
        pad, delay = st.start_pad(), st.start_delay()
        start = int(round(pos))
        self.cur = start - pad
        guard = 0
        while st.available() < delay and guard < 400:
            self._feed(source, max(1, min(FEED, st.required() or FEED)))
            guard += 1
        dropped = 0
        while dropped < delay:
            got = len(st.retrieve(delay - dropped))
            if not got:
                break
            dropped += got
        self.active = True
        self.calm = 0
        self.fresh = True

    def _feed(self, source, count):
        self.stretcher.process(source(self.cur, count))
        self.cur += count

    def _pull(self, n, rate, source, wrap):
        st = self.stretcher
        st.set_rate(rate)
        guard = 0
        while st.available() < n and guard < 200:
            if wrap and self.cur >= wrap[1] and wrap[1] - wrap[0] > 64:
                self.cur = int(wrap[0] + (self.cur - wrap[0]) % (wrap[1] - wrap[0]))
            self._feed(source, max(1, min(FEED, st.required() or FEED)))
            guard += 1
        out = st.retrieve(n)
        if len(out) < n:
            out = np.concatenate([out, np.zeros((n - len(out), 2), np.float32)])
        return out

    def read(self, pos, n, rate, source, wrap=None):
        """n samples for a deck whose playhead is at `pos` (samples) and moves `rate` source samples per sample.

        `wrap` is the deck's (loop in, loop out) in samples while it loops, so the feed stays inside it.
        """
        self.fresh = False
        self.tail = None
        if not self.wanted(rate):
            if self.active and self.expected is not None and abs(pos - self.expected) <= 2.0:
                try:
                    self.tail = self._pull(n, rate, source, wrap)
                except Exception:
                    self.tail = None
            self.stop()
            return None
        try:
            jumped = self.expected is None or abs(pos - self.expected) > 2.0
            if not self.active or jumped:
                self._prime(pos, rate, source)
            out = self._pull(n, rate, source, wrap)
        except Exception:
            logger.exception("Key lock failed; the deck plays without it")
            self.stop()
            return None
        self.expected = pos + rate * n
        if wrap and self.expected >= wrap[1] and wrap[1] - wrap[0] > 64:
            self.expected = wrap[0] + (self.expected - wrap[0]) % (wrap[1] - wrap[0])
        return out
