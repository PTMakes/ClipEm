import base64
import hashlib

import ed25519_verify
import pytest

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


# RFC 8032 TEST 3 (two bytes) and TEST SHA(abc) (a 64-byte message).
MORE_VECTORS = [
    (
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        bytes.fromhex("af82"),
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
    (
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        hashlib.sha512(b"abc").digest(),
        "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
        "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704",
    ),
]
P, L = ed25519_verify._P, ed25519_verify._L
IDENTITY = (1).to_bytes(32, "little")  # the neutral point, encoded


def _valid():
    public_key, message, signature = RFC_VECTORS[1]
    return bytes.fromhex(public_key), bytes.fromhex(message), bytes.fromhex(signature)


@pytest.mark.parametrize("public_key, message, signature", MORE_VECTORS)
def test_more_rfc8032_vectors_verify(public_key, message, signature):
    assert ed25519_verify.verify(bytes.fromhex(public_key), message, bytes.fromhex(signature)) is True


def test_a_signature_from_another_key_is_refused():
    other_key = bytes.fromhex(RFC_VECTORS[0][0])
    _key, message, signature = _valid()
    assert ed25519_verify.verify(other_key, message, signature) is False


def test_the_curve_constants_are_ed25519s():
    assert ed25519_verify._G[0] == 15112221349535400772501151409588531511454012693041857206046113283949847762202
    assert ed25519_verify._G[1] == 46316835694926478169428394003475163141307993866256225615783033603165251855960
    assert ed25519_verify._SQRT_M1 ** 2 % P == P - 1


def test_a_second_valid_signature_cant_be_made_from_the_first():
    """S + L signs the same thing mathematically (malleability): RFC 8032 refuses any S >= L."""
    public_key, message, signature = _valid()
    s = int.from_bytes(signature[32:], "little")
    assert s + L < 2 ** 256
    assert ed25519_verify.verify(public_key, message, signature[:32] + (s + L).to_bytes(32, "little")) is False


def test_s_of_exactly_l_is_refused():
    # With the neutral point as key and R, s = 0 passes the equation - so does
    # L, which only the range check can stop.
    assert ed25519_verify.verify(IDENTITY, b"m", IDENTITY + bytes(32)) is True
    assert ed25519_verify.verify(IDENTITY, b"m", IDENTITY + L.to_bytes(32, "little")) is False


@pytest.mark.parametrize("y", [P, P + 1])
def test_a_y_that_isnt_reduced_is_refused(y):
    assert ed25519_verify._recover_x(y, 0) is None
    encoded = y.to_bytes(32, "little")
    assert ed25519_verify.verify(encoded, b"m", encoded + bytes(32)) is False


def test_x_zero_has_no_negative():
    assert ed25519_verify._recover_x(1, 0) == 0
    assert ed25519_verify._recover_x(1, 1) is None
    negative_zero = (1 | 1 << 255).to_bytes(32, "little")
    assert ed25519_verify._decompress(negative_zero) is None
    assert ed25519_verify.verify(negative_zero, b"m", negative_zero + bytes(32)) is False


def test_a_y_off_the_curve_is_refused():
    assert ed25519_verify._recover_x(2, 0) is None
    assert ed25519_verify.verify((2).to_bytes(32, "little"), b"m", bytes(64)) is False


def test_points_survive_compression_both_square_root_branches():
    """Half of all y need the sqrt(-1) step; each sign bit must pick its own x."""
    point, used_sqrt_m1 = ed25519_verify._G, set()
    for _ in range(40):
        z = pow(point[2], P - 2, P)
        x, y = point[0] * z % P, point[1] * z % P
        decoded = ed25519_verify._decompress((y | (x & 1) << 255).to_bytes(32, "little"))
        assert ed25519_verify._point_equal(decoded, point) and decoded[0] == x
        x2 = (y * y - 1) * pow(ed25519_verify._D * y * y + 1, P - 2, P) % P
        used_sqrt_m1.add(pow(x2, (P + 3) // 8, P) ** 2 % P != x2)
        point = ed25519_verify._point_add(point, ed25519_verify._G)
    assert used_sqrt_m1 == {True, False}


def test_points_are_equal_only_if_x_and_y_both_are():
    g = ed25519_verify._G
    negated = ((P - g[0]) % P, g[1], 1, (P - g[3]) % P)  # same y
    assert ed25519_verify._point_equal(g, g)
    assert not ed25519_verify._point_equal(g, negated)
    assert not ed25519_verify._point_equal(g, (g[0], (g[1] + 1) % P, 1, 0))  # same x


def test_extra_bytes_after_a_valid_signature_are_refused():
    public_key, message, signature = _valid()
    assert ed25519_verify.verify(public_key, message, signature + b"\0") is False
    assert ed25519_verify.verify(public_key + b"\0", message, signature) is False


def test_keys_and_signatures_must_be_clean_base64():
    assert ed25519_verify.decode_key_or_signature(" QUJD\n") == b"ABC"
    with pytest.raises(ValueError):
        ed25519_verify.decode_key_or_signature("QU!JD")


def test_cli_usage_and_unreadable_inputs(tmp_path, capsys):
    assert ed25519_verify.main(["x"]) == 2
    public_key, message, signature = _valid()
    key, msg, sig = tmp_path / "pub.txt", tmp_path / "msg", tmp_path / "sig.txt"
    key.write_text(base64.b64encode(public_key).decode())
    msg.write_bytes(message)
    assert ed25519_verify.main(["x", str(key), str(msg), str(sig)]) == 1  # no signature file
    sig.write_text("not base64!!")
    assert ed25519_verify.main(["x", str(key), str(msg), str(sig)]) == 1
    sig.write_bytes(b"\xff\xfe")  # not even text
    assert ed25519_verify.main(["x", str(key), str(msg), str(sig)]) == 1
    assert "Could not read signature inputs" in capsys.readouterr().err
