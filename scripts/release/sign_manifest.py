from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.release.runtime_manifest import PINNED_PUBLIC_KEY_BASE64

SIGNING_KEY_ENV = "BABELDOC_RUNTIME_SIGNING_KEY_BASE64"


def sign_manifest(
    manifest: bytes,
    *,
    encoded_private_key: str,
    expected_public_key_base64: str = PINNED_PUBLIC_KEY_BASE64,
) -> bytes:
    if not encoded_private_key:
        raise ValueError(f"{SIGNING_KEY_ENV} is required")
    try:
        pem = base64.b64decode(encoded_private_key, validate=True)
        private_key = serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:
        raise ValueError("runtime signing key is not valid base64 PKCS8 PEM") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("runtime signing key must be Ed25519")
    expected_public_key = base64.b64decode(
        expected_public_key_base64,
        validate=True,
    )
    actual_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if actual_public_key != expected_public_key:
        raise ValueError("runtime signing key does not match the pinned public key")
    return private_key.sign(manifest)


def verify_manifest(
    manifest: bytes,
    signature: bytes,
    *,
    public_key_base64: str = PINNED_PUBLIC_KEY_BASE64,
) -> None:
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(public_key_base64, validate=True)
    )
    public_key.verify(signature, manifest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    manifest = args.manifest.read_bytes()
    if args.verify:
        verify_manifest(manifest, args.signature.read_bytes())
        return 0
    signature = sign_manifest(
        manifest,
        encoded_private_key=os.environ.get(SIGNING_KEY_ENV, ""),
    )
    args.signature.write_bytes(signature)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
