"""Verification of the Webhook Relay's `X-Relay-Signature` header.

    X-Relay-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "{t}.{raw_body}">[,v1=<hex>...]

The relay sends one `v1` per active secret while it rotates them; any match is accepted.
Schemes other than `v1` are ignored, so the relay can add a new one without breaking us.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass

HEADER = "X-Relay-Signature"

_TIMESTAMP = re.compile(r"\d{1,12}")
_V1 = re.compile(r"[0-9a-f]{64}")


class SignatureError(Exception):
    """The request can't be trusted. Callers should answer 401 and process nothing."""


class MalformedSignatureError(SignatureError):
    pass


class StaleSignatureError(SignatureError):
    pass


class SignatureMismatchError(SignatureError):
    pass


@dataclass(frozen=True)
class ParsedSignature:
    timestamp: int
    v1: tuple[str, ...]


def sign(body: bytes, secret: str, timestamp: int) -> str:
    """Hex HMAC-SHA256 of `"{timestamp}.{body}"`, matching the relay's PHP signer."""
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def parse(header: str) -> ParsedSignature:
    timestamps: list[str] = []
    v1: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            raise MalformedSignatureError("expected comma-separated key=value pairs")
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            if not _V1.fullmatch(value):
                raise MalformedSignatureError("v1 must be 64 lowercase hex characters")
            v1.append(value)

    if len(timestamps) != 1 or not _TIMESTAMP.fullmatch(timestamps[0]):
        raise MalformedSignatureError("expected exactly one numeric t=")
    if not v1:
        raise MalformedSignatureError("no v1 signature present")
    return ParsedSignature(int(timestamps[0]), tuple(v1))


def verify(header: str, body: bytes, secret: str, *, now: int, tolerance_seconds: int) -> None:
    """Raise a `SignatureError` unless `header` is a fresh, valid signature of `body`."""
    parsed = parse(header)
    if abs(now - parsed.timestamp) > tolerance_seconds:
        raise StaleSignatureError("timestamp outside the tolerance window")

    expected = sign(body, secret, parsed.timestamp)
    # Check every candidate, so timing doesn't reveal which position matched.
    matches = [hmac.compare_digest(expected, candidate) for candidate in parsed.v1]
    if not any(matches):
        raise SignatureMismatchError("no signature matches")
