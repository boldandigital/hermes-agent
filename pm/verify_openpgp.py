"""Verify an OpenPGP cleartext-signed file and print the signed text.

Runs in the checkout's test environment (pysequoia is a dev-group dependency),
never in PM's runtime, so it imports nothing from the repo.

    python -I verify_openpgp.py KEYRING.asc SIGNED.asc
"""
import sys

from pysequoia import Cert, verify


def main(keyring: str, signed: str) -> int:
    with open(keyring, "rb") as f:
        certs = Cert.split_bytes(f.read())
    with open(signed, "rb") as f:
        data = f.read()
    try:
        result = verify(data, lambda _ids: certs)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    signer = result.valid_sigs[0].certificate
    print(f"good signature from {signer}", file=sys.stderr)
    sys.stdout.buffer.write(result.bytes)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
