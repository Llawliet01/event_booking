"""HMAC signing/verification shared by the webhook receiver
(app/webhooks/routes.py) and the fake provider that signs outgoing
callbacks (app/payments/provider_simulator.py), so both sides agree on
exactly one signing scheme.
"""
import hashlib
import hmac


def sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify(payload: bytes, signature: str, secret: str) -> bool:
    expected = sign(payload, secret)
    return hmac.compare_digest(expected, signature)
