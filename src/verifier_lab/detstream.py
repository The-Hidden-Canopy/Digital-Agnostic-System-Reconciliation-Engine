"""Versioned deterministic byte stream for reproducible test generation (chapter 02).

SHA-256(seed || domain || counter). Not a security-grade RNG. Bounded integers use rejection sampling.
"""
from __future__ import annotations

import hashlib

VERSION = "sha256-counter-1"
DOMAIN = b"verifier-lab/detstream/1"


class DetStream:
    def __init__(self, seed: str):
        self.seed = seed.encode("utf-8")
        self.counter = 0
        self._buf = b""

    def _block(self) -> bytes:
        h = hashlib.sha256(self.seed + b"\x00" + DOMAIN + b"\x00" + self.counter.to_bytes(8, "big")).digest()
        self.counter += 1
        return h

    def take(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf += self._block()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def randbelow(self, n: int) -> int:
        if n <= 0:
            raise ValueError("n must be positive")
        if n == 1:
            return 0
        k = (n - 1).bit_length()
        nbytes = (k + 7) // 8
        limit = (256 ** nbytes // n) * n          # reject the biased tail instead of using modulo
        while True:
            v = int.from_bytes(self.take(nbytes), "big")
            if v < limit:
                return v % n

    def choice(self, seq):
        return seq[self.randbelow(len(seq))]

    def record(self) -> dict:
        return {"version": VERSION, "seed": self.seed.decode(), "blocks_consumed": self.counter}
