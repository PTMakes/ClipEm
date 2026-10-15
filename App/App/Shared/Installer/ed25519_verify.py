"""
Pure-Python Ed25519 signature verification (RFC 8032, section 5.1.7).

Used to check the release signature on clipem.app's install manifest before
anything it lists is downloaded - see updater.fetch_manifest(). Standard
library only, so it runs on every interpreter ClipEm uses: the bundled
embeddable Windows Python (which bootstrap.ps1 also runs this with, before
anything else is installed), the portable Linux Python, and system Pythons.

Verification only - nothing secret is ever handled here, so the usual
constant-time concerns don't apply. Signing happens at release time with
openssl (see Dev/release_common.py).

CLI (used by bootstrap.ps1):
    python ed25519_verify.py <public-key-file> <message-file> <signature-file>
exits 0 if the signature is valid, 1 otherwise.
"""

import base64
import hashlib
import sys

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 % _P == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _point_add(a, b):
    # Extended twisted Edwards coordinates, a = -1.
    pa = (a[1] - a[0]) * (b[1] - b[0]) % _P
    pb = (a[1] + a[0]) * (b[1] + b[0]) % _P
    pc = 2 * a[3] * b[3] * _D % _P
    pd = 2 * a[2] * b[2] % _P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _decompress(encoded):
    if len(encoded) != 32:
        return None
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def verify(public_key, message, signature):
    """True if `signature` (64 raw bytes) is a valid Ed25519 signature of
    `message` (bytes) under `public_key` (32 raw bytes)."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _decompress(public_key)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
    return _point_equal(_point_mul(s, _G), _point_add(r, _point_mul(h, a)))


def decode_key_or_signature(text):
    """Keys and signatures are stored base64-encoded (one line) - see
    signing_public_key.txt and the *.sig files next to each manifest."""
    return base64.b64decode(text.strip(), validate=True)


def main(argv):
    if len(argv) != 4:
        print("usage: ed25519_verify.py <public-key-file> <message-file> <signature-file>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "r", encoding="ascii") as f:
            public_key = decode_key_or_signature(f.read())
        with open(argv[2], "rb") as f:
            message = f.read()
        with open(argv[3], "r", encoding="ascii") as f:
            signature = decode_key_or_signature(f.read())
    except (OSError, ValueError) as exc:
        print(f"Could not read signature inputs: {exc}", file=sys.stderr)
        return 1
    if verify(public_key, message, signature):
        print("Signature OK")
        return 0
    print("Signature INVALID", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
