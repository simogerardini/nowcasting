"""VAPID key management: generate once, persist on the data volume, reuse everywhere."""
import base64
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger("vapid")
KEY_FILE = Path(os.environ.get("VAPID_KEY_FILE", "/data/vapid.json"))


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def generate_vapid_keypair() -> tuple[str, str]:
    """Return (public_key, private_key) both base64url (raw formats, as used by browsers / pywebpush)."""
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64url(pub), _b64url(priv)


def ensure_vapid_keys(settings) -> None:
    """Populate settings.vapid_* from env, else from KEY_FILE, else generate + persist."""
    if settings.vapid_public_key and settings.vapid_private_key:
        return
    if KEY_FILE.exists():
        data = json.loads(KEY_FILE.read_text())
    else:
        pub, priv = generate_vapid_keypair()
        data = {"public": pub, "private": priv}
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = KEY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, KEY_FILE)  # atomic create-if-absent (api & worker race)
        except FileExistsError:
            data = json.loads(KEY_FILE.read_text())
        finally:
            tmp.unlink(missing_ok=True)
        log.warning("Generated new VAPID key pair at %s", KEY_FILE)
    settings.vapid_public_key = data["public"]
    settings.vapid_private_key = data["private"]


if __name__ == "__main__":
    pub, priv = generate_vapid_keypair()
    print(f"VAPID_PUBLIC_KEY={pub}\nVAPID_PRIVATE_KEY={priv}")
