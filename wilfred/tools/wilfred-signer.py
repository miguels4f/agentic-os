"""Wilfred signer: the human side of Wilfred's protected gates.

  init  [--kind approval|review]                 create YOUR Ed25519 key pair
  sign  CHALLENGE.json [--kind approval|review]  show what is being approved, ask for the
                                                 passphrase, write a signed token

Two key pairs exist, each in ~/Library/Application Support/Wilfred/:
  approval  approval-{public,private}-key.pem           run gates (ship, merge, deploy,
                                                         dependencies, migrations) and `promote`
  review    review-attestation-{public,private}-key.pem  test-evolution reviews and
                                                         independent-review attestations

The private key is encrypted with a passphrase that is read only from an interactive
terminal (never from arguments, environment or stdin), so an agent session without a
terminal cannot sign on your behalf. No key ships with this kit: every user creates their own.

Run it with Wilfred's environment, from the `wilfred/` folder:
  uv run python tools/wilfred-signer.py init --kind approval
  uv run python tools/wilfred-signer.py sign <repo>/.hw/<run-id>/gates/<gate>.challenge.json
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core.approvals import build_decision_payload, canonical_bytes

HOME = Path.home() / "Library" / "Application Support" / "Wilfred"
SIGNED = HOME / "signed"  # never inside .hw/<run>/: Wilfred treats *.json there as its own records
KINDS = {
    "approval": ("approval-public-key.pem", "approval-private-key.pem", ".token.json"),
    "review": (
        "review-attestation-public-key.pem",
        "review-attestation-private-key.pem",
        ".review-token.json",
    ),
}


def _paths(kind: str) -> tuple[Path, Path, str]:
    public, private, suffix = KINDS[kind]
    return HOME / public, HOME / private, suffix


def _passphrase(prompt: str) -> bytes:
    if not sys.stdin.isatty():
        sys.exit("An interactive terminal is required: the passphrase is typed by hand only.")
    return getpass.getpass(prompt).encode()


def init(kind: str) -> None:
    public, private, _ = _paths(kind)
    if public.exists() or private.exists():
        sys.exit(f"A {kind} key already exists in {HOME}. Not overwriting it.")
    first = _passphrase(f"New {kind} passphrase (12+ characters): ")
    if len(first) < 12:
        sys.exit("Passphrase too short.")
    if _passphrase("Repeat the passphrase: ") != first:
        sys.exit("Passphrases do not match.")
    key = Ed25519PrivateKey.generate()
    HOME.mkdir(parents=True, exist_ok=True)
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(first)))
    public.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    print(f"Key created.\n  public: {public}\n  private (encrypted): {private}")
    print("Store the passphrase in your password manager: without it nothing can be signed.")


def sign(challenge_path: Path, kind: str, decision: str, name: str, note: str) -> None:
    public, private, suffix = _paths(kind)
    challenge = json.loads(challenge_path.read_text(encoding="utf-8"))
    expires = datetime.fromisoformat(challenge["expires_at"])
    left = (expires - datetime.now(UTC)).total_seconds()
    if left <= 0:
        sys.exit(f"This challenge expired at {expires.astimezone():%H:%M}. Ask for it to be reopened.")
    print(f"Expires at {expires.astimezone():%H:%M} ({int(left // 60)} min left).")
    print("You are about to sign:")
    for field in ("run_id", "gate_id", "purpose", "action", "expires_at"):
        if field in challenge:
            print(f"  {field}: {challenge[field]}")
    print(f"  decision: {decision}")
    if input("Confirm? type 'yes': ").strip().lower() != "yes":
        sys.exit("Cancelled.")
    key = serialization.load_pem_private_key(private.read_bytes(), _passphrase("Passphrase: "))
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit("The private key is not Ed25519.")
    payload = build_decision_payload(challenge, decision=decision, decided_by=name, note=note,
                                     public_key_path=public)
    token = {"payload": payload,
             "signature": base64.b64encode(key.sign(canonical_bytes(payload))).decode()}
    SIGNED.mkdir(parents=True, exist_ok=True)
    out = SIGNED / f"{challenge['run_id']}--{challenge['gate_id']}{suffix}"
    out.write_text(json.dumps(token, indent=2) + "\n", encoding="utf-8")
    print(f"Signed token written to {out} (valid for 5 minutes).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--kind", choices=sorted(KINDS), default="approval")
    s = sub.add_parser("sign")
    s.add_argument("challenge", type=Path)
    s.add_argument("--kind", choices=sorted(KINDS), default="approval")
    s.add_argument("--decision", choices=["approve", "reject", "modify_scope"], default="approve")
    s.add_argument("--by", default=getpass.getuser())
    s.add_argument("--note", default="")
    args = parser.parse_args()
    if args.cmd == "init":
        init(args.kind)
    else:
        sign(args.challenge, args.kind, args.decision, args.by, args.note)


if __name__ == "__main__":
    main()
