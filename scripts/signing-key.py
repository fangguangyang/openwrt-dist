#!/usr/bin/env python3
"""Generate, check and install persistent feed signing keys without logging them."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

SECRET_NAMES = {"apk": "APK_SIGNING_KEY", "opkg": "OPKG_SIGNING_KEY"}


def private_file(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
    path.chmod(0o600)


def signing_material(manager, environment):
    name = SECRET_NAMES[manager]
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"Configure {name} in Actions secrets to keep the fixed feed's signing key stable")
    if manager == "apk":
        result = subprocess.run(["openssl", "pkey", "-check", "-noout"], input=value, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise ValueError("APK_SIGNING_KEY must contain a valid unencrypted PEM private key")
        return {"private_key": value + "\n"}
    try:
        pair = json.loads(value)
        if not all(isinstance(pair[k], str) and pair[k].strip() for k in ("private_key", "public_key")):
            raise ValueError()
        return pair
    except (ValueError, TypeError, KeyError):
        raise ValueError("OPKG_SIGNING_KEY must contain the JSON keypair produced by signing-key.py generate opkg") from None


def install(manager, sdk, environment):
    material = signing_material(manager, environment)
    if manager == "apk":
        secret, public = sdk / "private-key.pem", sdk / "public-key.pem"
        private_file(secret, material["private_key"])
        subprocess.run(["openssl", "pkey", "-in", str(secret), "-pubout", "-out", str(public)], check=True)
    else:
        secret, public = sdk / "key-build", sdk / "key-build.pub"
        private_file(secret, material["private_key"])
        public.write_text(material["public_key"])
        usign = str(sdk / "staging_dir/host/bin/usign")
        with tempfile.TemporaryDirectory() as directory:
            message = Path(directory) / "message"
            message.write_text("OpenWrt feed signing key check\n")
            subprocess.run([usign, "-S", "-s", str(secret), "-m", str(message)], check=True)
            subprocess.run([usign, "-V", "-q", "-p", str(public), "-m", str(message)], check=True)


def generate(manager, directory, usign, public_path=None):
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.glob(manager + "-*")):
        raise ValueError("Signing key files already exist; reuse them or choose another --directory")
    public = public_path or directory / ("apk-public-key.pem" if manager == "apk" else "opkg-public.key")
    if public.exists():
        raise ValueError("Public key already exists; reuse the original private key or choose another --public-key")
    public.parent.mkdir(parents=True, exist_ok=True)
    if manager == "apk":
        output = directory / "apk-signing-key.pem"
        if public.resolve() == output.resolve():
            raise ValueError("Public and private keys need different output paths")
        private_file(output, "")
        subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(output)], check=True)
        subprocess.run(["openssl", "pkey", "-in", str(output), "-pubout", "-out", str(public)], check=True)
    else:
        secret = directory / "opkg-private.key"
        if public.resolve() == secret.resolve():
            raise ValueError("Public and private keys need different output paths")
        subprocess.run([usign, "-G", "-s", str(secret), "-p", str(public), "-c", "OpenWrt feed"], check=True)
        secret.chmod(0o600)
        output = directory / "opkg-signing-key.json"
        private_file(output, json.dumps({"private_key": secret.read_text(), "public_key": public.read_text()}) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("generate")
    create.add_argument("manager", choices=SECRET_NAMES)
    create.add_argument("--directory", type=Path, default=Path(".build/signing"))
    create.add_argument("--public-key", type=Path, help="Public key output (APK defaults to the repository root)")
    create.add_argument("--usign", default="usign")
    put = commands.add_parser("install")
    put.add_argument("manager", choices=SECRET_NAMES)
    put.add_argument("sdk", type=Path)
    check = commands.add_parser("check")
    check.add_argument("matrix", type=Path)
    args = parser.parse_args()
    if args.command == "generate":
        public = args.public_key
        if public is None and args.manager == "apk":
            public = Path(__file__).resolve().parent.parent / "apk-public-key.pem"
        output = generate(args.manager, args.directory.resolve(), args.usign, public.resolve() if public else None)
        print(f"Set {SECRET_NAMES[args.manager]} to the contents of {output}")
        print("Use the same secret value in openwrt-dist and the calling firmware repository.")
    elif args.command == "install":
        install(args.manager, args.sdk.resolve(), os.environ)
    else:
        for manager in {target["package_manager"] for target in json.loads(args.matrix.read_text())["include"]}:
            signing_material(manager, os.environ)
        print("Persistent signing keys are configured")


if __name__ == "__main__":
    main()
