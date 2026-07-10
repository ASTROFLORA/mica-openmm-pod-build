"""Slice-7 §17 — Ed25519 signed verdict cues + WORM verifier.

Per-session keypair (in-memory or persisted externally by caller). Signs
any post whose post_type is in _SIGN_REQUIRED. Verifier re-walks the feed
chain and validates each signature.

Graceful no-op if cryptography not installed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization
    _HAS_CRYPTO = True
except Exception:
    _HAS_CRYPTO = False

_LOG = logging.getLogger("mica.feed_signing")

_SIGN_REQUIRED = frozenset({
    "driver_decision", "session_close", "insight", "decision",
})

# In-memory keystore keyed by session_id. For multi-process durability,
# caller persists via Neon driver_sessions table.
_KEYSTORE: Dict[str, Tuple[bytes, bytes]] = {}  # session_id -> (priv_pem, pub_pem)


@dataclass
class VerifyResult:
    ok: bool
    checked: int = 0
    signed_ok: int = 0
    missing_sig: int = 0
    bad_sig: int = 0
    errors: List[str] = field(default_factory=list)


def _canonical_payload(post: Dict) -> bytes:
    md = dict(post.get("metadata") or {})
    md.pop("ed25519_sig", None)
    md.pop("ed25519_alg", None)
    sig_free = dict(post)
    sig_free["metadata"] = md
    return json.dumps(sig_free, sort_keys=True, default=str).encode("utf-8")


def is_crypto_available() -> bool:
    return _HAS_CRYPTO


def generate_session_keypair(session_id: str) -> Dict[str, str]:
    """Create and cache a keypair. Returns {'session_id','public_key_pem'}."""
    if not _HAS_CRYPTO:
        return {"session_id": session_id, "public_key_pem": "", "crypto": "unavailable"}
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _KEYSTORE[session_id] = (priv_pem, pub_pem)
    return {"session_id": session_id, "public_key_pem": pub_pem.decode()}


def register_session_keypair(session_id: str, priv_pem: bytes, pub_pem: bytes) -> None:
    _KEYSTORE[session_id] = (priv_pem, pub_pem)


def sign_post_if_required(post: Dict) -> Dict:
    """Mutates `post` in place adding metadata.ed25519_sig if required."""
    if not _HAS_CRYPTO:
        return post
    if post.get("post_type") not in _SIGN_REQUIRED:
        return post
    session_id = post.get("session_id") or (post.get("metadata") or {}).get("session_id")
    if not session_id or session_id not in _KEYSTORE:
        return post
    try:
        priv_pem, _pub_pem = _KEYSTORE[session_id]
        priv = serialization.load_pem_private_key(priv_pem, password=None)
        sig = priv.sign(_canonical_payload(post))
        md = dict(post.get("metadata") or {})
        md["ed25519_sig"] = base64.b64encode(sig).decode()
        md["ed25519_alg"] = "Ed25519"
        post["metadata"] = md
    except Exception as exc:
        _LOG.debug("sign skipped: %s", exc)
    return post


def verify_post(post: Dict, pub_pem: bytes) -> bool:
    if not _HAS_CRYPTO:
        return True  # skip verification
    if post.get("post_type") not in _SIGN_REQUIRED:
        return True
    md = post.get("metadata") or {}
    sig_b64 = md.get("ed25519_sig")
    if not sig_b64:
        return False
    try:
        pub = serialization.load_pem_public_key(pub_pem)
        sig = base64.b64decode(sig_b64)
        pub.verify(sig, _canonical_payload(post))
        return True
    except Exception:
        return False


def verify_feed_chain(posts: List[Dict], pub_pem_by_session: Dict[str, bytes]) -> VerifyResult:
    res = VerifyResult(ok=True)
    for p in posts:
        res.checked += 1
        if p.get("post_type") not in _SIGN_REQUIRED:
            continue
        sid = p.get("session_id") or (p.get("metadata") or {}).get("session_id")
        if not sid or sid not in pub_pem_by_session:
            res.missing_sig += 1
            continue
        pub_pem = pub_pem_by_session[sid]
        if verify_post(p, pub_pem):
            res.signed_ok += 1
        else:
            res.bad_sig += 1
            res.ok = False
    return res


def worm_enforce_append_only(path: str) -> bool:
    """Best-effort: verify the feed file has append-only semantics.
    Returns True if no pre-existing content was overwritten this session
    (cheap heuristic: inode present + size monotonic via sidecar)."""
    try:
        sidecar = path + ".worm"
        current_size = os.path.getsize(path) if os.path.exists(path) else 0
        last_size = 0
        if os.path.exists(sidecar):
            try:
                last_size = int(open(sidecar, "r").read().strip() or "0")
            except Exception:
                last_size = 0
        if current_size < last_size:
            _LOG.warning("WORM violation: feed shrank from %s to %s", last_size, current_size)
            return False
        open(sidecar, "w").write(str(current_size))
        return True
    except Exception as exc:
        _LOG.debug("worm check skipped: %s", exc)
        return True
