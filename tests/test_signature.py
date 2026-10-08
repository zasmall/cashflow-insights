import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from cashflow.core.signature import (
    MalformedSignatureError,
    SignatureError,
    SignatureMismatchError,
    StaleSignatureError,
    sign,
    verify,
)
from tests.helpers import GOLDEN_BODY, GOLDEN_HEADER, GOLDEN_TIMESTAMP

TOLERANCE = 300

bodies = st.binary(max_size=2048)
secrets = st.text(min_size=1, max_size=64)
timestamps = st.integers(min_value=1_000_000_000, max_value=4_000_000_000)


def header(body: bytes, secret: str, t: int) -> str:
    return f"t={t},v1={sign(body, secret, t)}"


@pytest.mark.parametrize("secret", ["whsec_golden_primary", "whsec_golden_rotated"])
def test_golden_vector_from_php_signer(secret: str) -> None:
    verify(GOLDEN_HEADER, GOLDEN_BODY, secret, now=GOLDEN_TIMESTAMP, tolerance_seconds=TOLERANCE)


def test_golden_vector_rejects_other_secret() -> None:
    with pytest.raises(SignatureMismatchError):
        verify(
            GOLDEN_HEADER, GOLDEN_BODY, "nope", now=GOLDEN_TIMESTAMP, tolerance_seconds=TOLERANCE
        )


@given(bodies, secrets, timestamps, st.integers(-TOLERANCE, TOLERANCE))
def test_fresh_signature_verifies(body: bytes, secret: str, t: int, skew: int) -> None:
    verify(header(body, secret, t), body, secret, now=t + skew, tolerance_seconds=TOLERANCE)


@given(bodies, secrets, timestamps, st.data())
def test_any_body_change_is_rejected(body: bytes, secret: str, t: int, data: st.DataObject) -> None:
    tampered = data.draw(st.binary(max_size=2048))
    assume(tampered != body)

    with pytest.raises(SignatureMismatchError):
        verify(header(body, secret, t), tampered, secret, now=t, tolerance_seconds=TOLERANCE)


@given(bodies, secrets, timestamps, st.integers(TOLERANCE + 1, 10**6), st.sampled_from([-1, 1]))
def test_timestamp_outside_window_is_stale(
    body: bytes, secret: str, t: int, distance: int, direction: int
) -> None:
    with pytest.raises(StaleSignatureError):
        verify(
            header(body, secret, t),
            body,
            secret,
            now=t + direction * distance,
            tolerance_seconds=TOLERANCE,
        )


@given(bodies, secrets, timestamps, st.lists(secrets, max_size=3), st.data())
def test_any_matching_v1_is_accepted(
    body: bytes, secret: str, t: int, others: list[str], data: st.DataObject
) -> None:
    candidates = [sign(body, other, t) for other in others]
    position = data.draw(st.integers(0, len(candidates)))
    candidates.insert(position, sign(body, secret, t))
    rotated = f"t={t}," + ",".join(f"v1={c}" for c in candidates)

    verify(rotated, body, secret, now=t, tolerance_seconds=TOLERANCE)


@given(st.text(max_size=300), bodies)
def test_arbitrary_headers_only_raise_signature_errors(raw_header: str, body: bytes) -> None:
    with pytest.raises(SignatureError):
        verify(raw_header, body, "secret", now=GOLDEN_TIMESTAMP, tolerance_seconds=TOLERANCE)


def test_unknown_schemes_are_ignored() -> None:
    body = b"{}"
    signed = f"t=100,v0=legacy,v2=future,v1={sign(body, 's', 100)}"

    verify(signed, body, "s", now=100, tolerance_seconds=TOLERANCE)


VALID_V1 = "a" * 64


@pytest.mark.parametrize(
    "raw_header",
    [
        "",
        "garbage",
        f"v1={VALID_V1}",
        "t=100",
        f"t=abc,v1={VALID_V1}",
        f"t=100,t=200,v1={VALID_V1}",
        "t=100,v1=XYZ",
        f"t=100,v1={VALID_V1.upper()}",
        f"t=1234567890123,v1={VALID_V1}",
    ],
)
def test_malformed_headers(raw_header: str) -> None:
    with pytest.raises(MalformedSignatureError):
        verify(raw_header, b"{}", "s", now=100, tolerance_seconds=TOLERANCE)
