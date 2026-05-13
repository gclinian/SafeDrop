"""X25519 + Fernet session crypto for SafeDrop.

Each peer holds a long-lived (per-process) X25519 keypair. The public key is
embedded in the discovery HELLO and in the TCP handshake. After the TCP
handshake both sides compute the same shared secret via X25519 ECDH, run it
through HKDF-SHA256 to derive a 32-byte Fernet key, and use that key to
encrypt every JSON frame on the connection.

A 4-digit pairing code is derived from the same shared secret so the two
users can visually confirm they are talking to each other and not a MITM.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.fernet import Fernet


_HKDF_INFO_KEY = b"SafeDrop v1 fernet key"
_HKDF_INFO_PAIR = b"SafeDrop v1 pair code"


@dataclass
class Identity:
    """A per-process X25519 keypair."""

    private_key: X25519PrivateKey

    @classmethod
    def generate(cls) -> "Identity":
        return cls(X25519PrivateKey.generate())

    @property
    def public_key(self) -> X25519PublicKey:
        return self.private_key.public_key()

    def public_key_b64(self) -> str:
        raw = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")


def load_peer_pubkey(pubkey_b64: str) -> X25519PublicKey:
    raw = base64.b64decode(pubkey_b64.encode("ascii"))
    return X25519PublicKey.from_public_bytes(raw)


@dataclass
class Session:
    """A symmetric encryption session derived from an ECDH exchange."""

    fernet: Fernet
    pair_code: str

    def encrypt(self, plaintext: bytes) -> bytes:
        return self.fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self.fernet.decrypt(ciphertext)


def derive_session(identity: Identity, peer_pubkey_b64: str) -> Session:
    peer = load_peer_pubkey(peer_pubkey_b64)
    shared = identity.private_key.exchange(peer)

    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO_KEY,
    ).derive(shared)
    fernet_key = base64.urlsafe_b64encode(key_material)
    fernet = Fernet(fernet_key)

    pair_material = HKDF(
        algorithm=hashes.SHA256(),
        length=4,
        salt=None,
        info=_HKDF_INFO_PAIR,
    ).derive(shared)
    (pair_int,) = struct.unpack(">I", pair_material)
    pair_code = f"{pair_int % 10000:04d}"

    return Session(fernet=fernet, pair_code=pair_code)
