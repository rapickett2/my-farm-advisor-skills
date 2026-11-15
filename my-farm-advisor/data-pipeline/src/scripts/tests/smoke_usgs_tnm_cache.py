#!/usr/bin/env python3
"""Smoke test: USGS TNM adapter reuses one tile across fields and streams to disk.

A 3DEP 1 m project tile is several hundred MB and covers many fields. The
runtime cache is per field, so without reuse every field downloaded the same
bytes again (~100 min/field for the Mississippi tile). This checks:

1. a tile already cached for a sibling field is linked, not re-fetched;
2. the first download streams to disk and leaves no ``.part`` behind;
3. a short transfer against Content-Length fails instead of caching a
   truncated raster.

Runs offline: the "server" is a file:// URL.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = Path(os.environ.get("MY_FARM_ADVISOR_SKILL_ROOT", HERE.parents[3]))
SKILL_SRC = SKILL_ROOT / "terrain" / "dem-terrain" / "src"
sys.path.insert(0, str(SKILL_SRC))

from dem_terrain.usgs_tnm import (  # noqa: E402
    USGSTNMAdapter,
    _http_stream_to_file,
    download_with_retries,
)
from dem_terrain.source_resolver import SourceCandidate  # noqa: E402


class SmokeFailure(AssertionError):
    pass


def _candidate(url: str) -> SourceCandidate:
    return SourceCandidate(
        adapter_id="usgs_tnm_3dep",
        adapter_name="USGS TNM 3DEP",
        source_id="smoke",
        source_name="Smoke 1 m tile",
        resolution_m=1.0,
        coverage_score=1.0,
        source_urls=(url,),
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tnm-cache-smoke-") as tmp:
        root = Path(tmp)
        origin = root / "origin"
        origin.mkdir()
        tile = origin / "USGS_1M_15_x72y381_SMOKE_2018_D18.tif"
        payload = bytes(range(256)) * 8192  # 2 MB
        tile.write_bytes(payload)
        url = tile.as_uri()

        cache_root = root / "shared" / "usgs_tnm_3dep"
        first = cache_root / "field-a"
        second = cache_root / "field-b"

        adapter = USGSTNMAdapter(network_enabled=False)
        path_a = adapter.download(_candidate(url), first)
        if path_a.read_bytes() != payload:
            raise SmokeFailure("first download does not match the source tile")
        if any(p.name.endswith(".part") for p in first.iterdir()):
            raise SmokeFailure("temporary .part file left behind after a clean download")
        print("PASS: first field streams the tile to disk and cleans up its .part")

        path_b = adapter.download(_candidate(url), second)
        if path_b.read_bytes() != payload:
            raise SmokeFailure("reused tile does not match the source tile")
        if path_a.stat().st_ino != path_b.stat().st_ino:
            raise SmokeFailure("sibling field should link the cached tile, not copy or re-fetch")
        print("PASS: second field links the sibling's tile instead of downloading it again")

        # Streaming writes exactly the bytes the "server" sent.
        streamed = root / "stream" / "tile.tif"
        streamed.parent.mkdir()
        _http_stream_to_file(tile.as_uri(), streamed, 30.0)
        if streamed.read_bytes() != payload:
            raise SmokeFailure("streamed file does not match the source bytes")

        try:
            download_with_retries("file:///nonexistent/tile.tif", root / "missing", retries=0)
        except Exception:
            pass
        else:
            raise SmokeFailure("a missing source must raise, not return a record")
        if (root / "missing").exists() and any((root / "missing").iterdir()):
            raise SmokeFailure("failed download must not leave a cached file")
        print("PASS: failed transfers raise and leave no cached raster")
    return 0


if __name__ == "__main__":
    sys.exit(main())
