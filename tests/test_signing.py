from klepa_core.signing import canonical_json, sign, verify

KEY = b"k" * 32


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": "é"}) == canonical_json({"a": "é", "b": 1})
    assert canonical_json({"a": "é"}) == '{"a":"é"}'.encode()


def test_sign_and_verify_roundtrip():
    assert verify(KEY, {"x": 1}, sign(KEY, {"x": 1}))


def test_verify_rejects_tampering_and_wrong_key():
    signature = sign(KEY, {"x": 1})
    assert not verify(KEY, {"x": 2}, signature)
    assert not verify(b"z" * 32, {"x": 1}, signature)
    assert not verify(KEY, {"x": 1}, "")
