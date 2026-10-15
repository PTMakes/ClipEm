import base64

import ed25519_verify

# RFC 8032 section 7.1, TEST 1 (empty message) and TEST 2 (one byte).
RFC_VECTORS = [
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


def test_rfc8032_vectors_verify():
    for public_key, message, signature in RFC_VECTORS:
        assert ed25519_verify.verify(bytes.fromhex(public_key), bytes.fromhex(message), bytes.fromhex(signature))


def test_rejects_tampered_message_and_signature():
    public_key, message, signature = (bytes.fromhex(x) for x in RFC_VECTORS[1])
    assert not ed25519_verify.verify(public_key, message + b"x", signature)
    flipped = bytearray(signature)
    flipped[10] ^= 1
    assert not ed25519_verify.verify(public_key, message, bytes(flipped))


def test_rejects_wrong_lengths():
    assert not ed25519_verify.verify(b"\x00" * 31, b"", b"\x00" * 64)
    assert not ed25519_verify.verify(b"\x00" * 32, b"", b"\x00" * 63)


def test_cli(tmp_path):
    public_key, message, signature = (bytes.fromhex(x) for x in RFC_VECTORS[1])
    (tmp_path / "pub.txt").write_text(base64.b64encode(public_key).decode())
    (tmp_path / "msg").write_bytes(message)
    (tmp_path / "sig.txt").write_text(base64.b64encode(signature).decode())
    args = ["x", str(tmp_path / "pub.txt"), str(tmp_path / "msg"), str(tmp_path / "sig.txt")]
    assert ed25519_verify.main(args) == 0
    (tmp_path / "msg").write_bytes(message + b"!")
    assert ed25519_verify.main(args) == 1
