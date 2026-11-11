"""Place DuckDB extensions in the current user's extension directory, where INSTALL finds them and LOAD reads them.

Run in the Duck image build (see the Dockerfile). The files are fetched directly rather than with DuckDB's own
INSTALL, because DuckDB didn't finish importing under the emulation the other architecture's build runs in.
Usage: python duckdb_extensions.py httpfs ducklake
"""

import gzip
import importlib.metadata
import pathlib
import platform
import sys
import urllib.request

version = "v" + importlib.metadata.version("duckdb")
arch = {"x86_64": "linux_amd64", "aarch64": "linux_arm64"}[platform.machine()]
target = pathlib.Path.home() / ".duckdb" / "extensions" / version / arch
target.mkdir(parents=True, exist_ok=True)
for name in sys.argv[1:]:
    url = f"https://extensions.duckdb.org/{version}/{arch}/{name}.duckdb_extension.gz"
    # The CDN refuses urllib's default User-Agent (403); DuckDB's own and curl's are let through.
    request = urllib.request.Request(url, headers={"User-Agent": "duckstring-image-build"})
    with urllib.request.urlopen(request, timeout=120) as resp:
        (target / f"{name}.duckdb_extension").write_bytes(gzip.decompress(resp.read()))
    print(f"{name}: {target / name}.duckdb_extension")
