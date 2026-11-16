# Local Instructions

## Purpose

This folder owns the field-level EDA workflow: `src/` (data loading, static figures, artifact CLI, smoke
test), `report/build_report.py` (the one-time report assembler), `GUIDE.md`, `README.md`, and the
committed `report/`.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly asks for a broader skill
change. Do not change parent `SKILL.md`, sibling EDA workflows, data-pipeline scripts, or root policy
from a field-eda task unless explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first, then `README.md`. For runtime paths and pipeline commands, read
`../../data-pipeline/AGENTS.md`.

## Rules

- Read only data-pipeline runtime outputs under `${DATA_PIPELINE_DATA_ROOT}/data-pipeline`; never read
  grower source exports directly.
- Static output only: PNG, CSV, `findings.json`, and one self-contained HTML page with no scripts. No
  interactive maps, dashboards, or web apps.
- Keep the two steps separate: `src/run_field_eda.py` produces artifacts and must not write the report;
  `report/build_report.py` assembles the page from those artifacts. Do not fold report generation back
  into the reusable runner.
- Soil is out of scope for this workflow.
- Every number or claim in the report must be computed from the tables. Do not hard-code findings,
  distances, or place descriptions.
- Pivot / irrigation status comes only from `manifests/field-attributes.csv`; never infer it from
  boundary shape.
- Keep committed report assets small: each file under 1 MB (the smoke test enforces the HTML limit).
  Never commit tables, rasters, or other runtime data.

## Local validation

Run `src/smoke_field_eda.py` with the runtime venv Python, then `./scripts/validate.sh` from the
repository root.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or root files. Do not
duplicate root-wide asset, vendor, or validation policy here except this pointer to
`../../../AGENTS.md`.
