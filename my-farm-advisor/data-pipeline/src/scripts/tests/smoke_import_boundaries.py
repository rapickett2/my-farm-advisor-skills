#!/usr/bin/env python3
"""Offline smoke checks for ingest/import_boundaries.py.

Run with a Python that has the data-pipeline requirements installed, e.g. the
runtime venv:

    "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \\
        my-farm-advisor/data-pipeline/src/scripts/tests/smoke_import_boundaries.py

The check installs a source-only runtime into a temporary root, writes a tiny
synthetic boundary file, and never touches the network or the real runtime.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
DATA_PIPELINE_DIR = SCRIPT_PATH.parents[3]
INSTALL_SCRIPT = DATA_PIPELINE_DIR / "scripts" / "install.sh"


class SmokeFailure(AssertionError):
    """Raised when a smoke check fails."""


def _square(x: float, y: float, size: float = 0.004) -> list[list[float]]:
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]


def _feature(props: dict, rings: list[list[list[float]]]) -> dict:
    return {"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": rings}}


def _write_source(path: Path) -> None:
    bowtie = [[[-90.80, 35.10], [-90.796, 35.104], [-90.796, 35.10], [-90.80, 35.104], [-90.80, 35.10]]]
    features = [
        # PolygonZ, as many FMIS exports are -> must be written 2D.
        _feature({"CLIENT_NAM": "Smoke Grower", "FARM_NAME": "Smoke Farm", "FIELD_NAME": "Smoke Farm 1",
                  "FIELD_ID": "id-1"}, [[[x, y, 61.5] for x, y in _square(-90.82, 35.10)]]),
        # Same field split across two rows -> one multipolygon.
        _feature({"CLIENT_NAM": "Smoke Grower", "FARM_NAME": "Smoke Farm", "FIELD_NAME": "North",
                  "FIELD_ID": ""}, [_square(-90.81, 35.10)]),
        _feature({"CLIENT_NAM": "Smoke Grower", "FARM_NAME": "Smoke Farm", "FIELD_NAME": "North",
                  "FIELD_ID": ""}, [_square(-90.805, 35.10)]),
        # Self-intersecting polygon -> repaired.
        _feature({"CLIENT_NAM": "Smoke Grower", "FARM_NAME": "Smoke Farm", "FIELD_NAME": "Bowtie",
                  "FIELD_ID": ""}, bowtie),
        _feature({"CLIENT_NAM": "Other Grower", "FARM_NAME": "42", "FIELD_NAME": "A",
                  "FIELD_ID": "other-a"}, [_square(-90.70, 35.20)]),
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")


def _write_kml_multipart(path: Path) -> None:
    """One placemark holding ten disjoint fields, as farm-app KML exports do."""
    rings = []
    for row, y in enumerate((35.000, 34.990)):
        for col in range(5):
            x = -90.900 + col * 0.005
            coords = [
                (x, y), (x + 0.004, y), (x + 0.004, y + 0.004), (x, y + 0.004), (x, y),
            ]
            body = " ".join(f"{cx:.6f},{cy:.6f},0" for cx, cy in coords)
            rings.append(
                "<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing>"
                f"<coordinates>{body}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
            )
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>KML Boundaries</name><MultiGeometry>"
        + "".join(rings)
        + "</MultiGeometry></Placemark></Document></kml>\n",
        encoding="utf-8",
    )


def _run(cmd: list[str], env: dict[str, str], cwd: Path, expect_ok: bool = True) -> str:
    result = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)
    if expect_ok and result.returncode != 0:
        raise SmokeFailure(f"command failed: {cmd}\n{result.stdout}\n{result.stderr}")
    if not expect_ok and result.returncode == 0:
        raise SmokeFailure(f"command unexpectedly succeeded: {cmd}")
    return result.stdout + result.stderr


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mfa-import-smoke-") as tmp:
        root = Path(tmp) / "runtime"
        env = os.environ.copy()
        env.update(
            {
                "DATA_PIPELINE_DATA_ROOT": str(root),
                "DATA_PIPELINE_VENV_DIR": sys.prefix,
                "DATA_PIPELINE_BOOTSTRAP_NO_INSTALL_DEPS": "1",
            }
        )
        _run(
            [str(INSTALL_SCRIPT), "--non-interactive", "--force-refresh", "--no-install-deps",
             "--persist", "none"],
            env,
            DATA_PIPELINE_DIR,
        )
        src = root / "data-pipeline" / "src"
        source = Path(tmp) / "boundaries.geojson"
        _write_source(source)
        importer = [sys.executable, "scripts/ingest/import_boundaries.py", "--source", str(source),
                    "--state", "AR", "--state-fips", "05"]

        out = _run([*importer, "--dry-run"], env, src)
        if "Dry run" not in out or (root / "data-pipeline" / "growers").exists() and any(
            (root / "data-pipeline" / "growers").rglob("field_boundaries.geojson")
        ):
            raise SmokeFailure("dry run wrote files or did not report")
        print("PASS: dry run writes nothing")

        _run(importer, env, src)
        farm = root / "data-pipeline" / "growers" / "smoke-grower" / "farms" / "smoke-farm"
        boundary = farm / "boundary" / "field_boundaries.geojson"
        data = json.loads(boundary.read_text(encoding="utf-8"))
        props = {f["properties"]["field_id"]: f for f in data["features"]}
        if set(props) != {"id-1", "smoke-farm-north", "smoke-farm-bowtie"}:
            raise SmokeFailure(f"unexpected field ids: {sorted(props)}")
        if props["smoke-farm-north"]["geometry"]["type"] != "MultiPolygon":
            raise SmokeFailure("split field rows were not merged")
        if any(f["properties"]["state_fips"] != "05" for f in data["features"]):
            raise SmokeFailure("state_fips not written")
        first_ring = props["id-1"]["geometry"]["coordinates"][0]
        if any(len(coord) != 2 for coord in first_ring):
            raise SmokeFailure("Z coordinates were not dropped")
        with (farm / "manifests" / "field-inventory.csv").open(encoding="utf-8") as handle:
            slugs = {row["field_id"]: row["field_slug"] for row in csv.DictReader(handle)}
        if slugs.get("id-1") != "smoke-farm-1":
            raise SmokeFailure(f"farm prefix should not be doubled: {slugs}")
        if not (root / "data-pipeline" / "growers" / "other-grower" / "farms" / "farm-42").exists():
            raise SmokeFailure("numeric farm name was not prefixed")
        print("PASS: canonical boundaries, inventory, derived ids, merge, repair")

        before = boundary.read_bytes()
        _run(importer, env, src)
        if boundary.read_bytes() != before:
            raise SmokeFailure("re-import is not byte-identical")
        print("PASS: re-import is deterministic")

        boundary.write_text(before.decode("utf-8").replace("Smoke Farm 1", "Edited"), encoding="utf-8")
        _run(importer, env, src, expect_ok=False)
        _run([*importer, "--force"], env, src)
        if boundary.read_bytes() != before:
            raise SmokeFailure("--force did not restore boundaries")
        print("PASS: differing boundaries require --force")

        # KML exports pack a farm into one multi-part placemark.
        kml = Path(tmp) / "kml_boundaries.kml"
        _write_kml_multipart(kml)
        _run(
            [sys.executable, "scripts/ingest/import_boundaries.py", "--source", str(kml),
             "--field-name-column", "Name", "--grower-name", "KML Grower",
             "--farm-name", "KML Farm", "--explode-multipart", "--multipart-prefix", "KF",
             "--state", "AR", "--state-fips", "05"],
            env,
            src,
        )
        kml_farm = root / "data-pipeline" / "growers" / "kml-grower" / "farms" / "kml-farm"
        kml_data = json.loads(
            (kml_farm / "boundary" / "field_boundaries.geojson").read_text(encoding="utf-8")
        )
        names = [f["properties"]["field_name"] for f in kml_data["features"]]
        if len(names) != 10:
            raise SmokeFailure(f"multipart KML did not explode: {len(names)} fields")
        if names != [f"KF {i:02d}" for i in range(1, 11)]:
            raise SmokeFailure(f"unexpected exploded names: {names}")
        north = kml_data["features"][0]["geometry"]["coordinates"][0]
        south = kml_data["features"][5]["geometry"]["coordinates"][0]
        if not min(c[1] for c in north) > max(c[1] for c in south):
            raise SmokeFailure("exploded parts are not ordered north-to-south")
        if any(len(coord) != 2 for f in kml_data["features"]
               for coord in f["geometry"]["coordinates"][0]):
            raise SmokeFailure("KML Z coordinates were not dropped")
        print("PASS: multipart KML explodes to 10 ordered 2D fields")
    return 0


if __name__ == "__main__":
    sys.exit(main())
