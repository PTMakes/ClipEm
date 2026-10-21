"""Lines the app tracks the tray recorded up with a clip, in time.

The tray and OBS both capture the same sound, but each stamps it with its own
clock, a few dozen milliseconds apart and not the same each time. Every app
track is part of the clip's Desktop track, though, so comparing how loud they
both are from one millisecond to the next finds where in the recording the
clip starts - to a millisecond or so.

Pure Python (the tray's Python has no numpy): loudness is summed to 1 kHz,
searched coarsely (10 ms steps) and then finely (1 ms) around the best
coarse match.
"""

from array import array
from itertools import accumulate
from math import sqrt
from operator import mul

ENV_RATE = 1000  # envelope values per second
COARSE = 10  # envelope values per coarse step
MIN_OVERLAP = 500  # envelope values two stretches must share to be compared

# Mean absolute level (of 32768) below which a stretch counts as silent: -70 dBFS.
SILENCE_LEVEL = 10


def _samples(pcm):
    samples = array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    return samples


def envelope(samples, block):
    """How loud each `block` samples are: the sum of their absolute values."""
    return [sum(map(abs, samples[i:i + block])) for i in range(0, len(samples) - block + 1, block)]


def mono_envelope(pcm, rate):
    """Envelope of mono 16-bit PCM at `rate` Hz, ENV_RATE values per second."""
    return envelope(_samples(pcm), rate // ENV_RATE)


def stereo_envelope(pcm, rate):
    """The same for 16-bit stereo PCM (the left channel stands for both)."""
    return envelope(_samples(pcm)[0::2], rate // ENV_RATE)


def add_envelopes(envelopes):
    """Element-wise sum, as long as the longest."""
    envelopes = [e for e in envelopes if e]
    if not envelopes:
        return []
    length = max(len(e) for e in envelopes)
    total = [0] * length
    for env in envelopes:
        for i, value in enumerate(env):
            total[i] += value
    return total


def is_silent(env, block):
    """Whether an envelope (of `block` samples per value) never gets louder
    than SILENCE_LEVEL on average."""
    return not env or max(env) / block < SILENCE_LEVEL


def _decimate(env, factor):
    return [sum(env[i:i + factor]) for i in range(0, len(env) - factor + 1, factor)]


def _centered(env):
    if not env:
        return []
    mean = sum(env) / len(env)
    return [value - mean for value in env]


class Correlator:
    """Normalized correlation between a reference envelope and a longer one
    it's somewhere inside: score(lag) compares ref[i] with sig[i + lag]."""

    def __init__(self, ref, sig):
        self.ref = ref
        self.sig = sig
        self._ref_energy = [0.0, *accumulate(x * x for x in ref)]
        self._sig_energy = [0.0, *accumulate(x * x for x in sig)]

    def score(self, lag):
        lo = max(0, -lag)
        hi = min(len(self.ref), len(self.sig) - lag)
        if hi - lo < MIN_OVERLAP:
            return 0.0
        energy = (self._ref_energy[hi] - self._ref_energy[lo]) * \
                 (self._sig_energy[hi + lag] - self._sig_energy[lo + lag])
        if energy <= 0:
            return 0.0
        return sum(map(mul, self.ref[lo:hi], self.sig[lo + lag:hi + lag])) / sqrt(energy)

    def best(self, center, radius):
        """(lag, score) of the best lag within `radius` of `center`."""
        return max(((lag, self.score(lag)) for lag in range(center - radius, center + radius + 1)),
                   key=lambda pair: pair[1])


def locate(ref, sig, nominal, search):
    """Where the reference (the clip's Desktop envelope) is inside sig (the
    tracks' envelope): (lag, score), lag in envelope values, searching
    `search` either side of `nominal`. A score near 0 means no match."""
    coarse = Correlator(_centered(_decimate(ref, COARSE)), _centered(_decimate(sig, COARSE)))
    lag, _score = coarse.best(round(nominal / COARSE), max(1, search // COARSE))
    best, score, _at_start = refine(ref, sig, lag * COARSE, COARSE + 2)
    return best, score


def refine(ref, sig, lag, span):
    """(best lag, its score, the score at `lag` itself) within `span` of
    `lag`, at full resolution."""
    correlator = Correlator(_centered(ref), _centered(sig))
    best, score = correlator.best(lag, span)
    return best, score, correlator.score(lag)
