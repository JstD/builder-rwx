"""Download sqlglot at the pinned commit from its own GitHub repository and unpack it.

Usage: python fetch_sqlglot.py <dest_dir>
"""

import io
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"


def main() -> None:
    dest = sys.argv[1]
    last = None
    for _ in range(5):
        try:
            data = urllib.request.urlopen(URL, timeout=120).read()
            break
        except Exception as error:  # retry transient network errors
            last = error
    else:
        raise SystemExit(f"could not download sqlglot: {last}")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        prefix = f"sqlglot-{COMMIT}/"
        members = []
        for member in archive.getmembers():
            if not member.name.startswith(prefix):
                continue
            member.name = member.name[len(prefix):]
            if member.name:
                members.append(member)
        archive.extractall(dest, members=members, filter="data")
    print(f"unpacked {len(members)} entries into {dest}")


if __name__ == "__main__":
    main()
