# Batch Transcoder (planner/runner)

Dockerised FFmpeg workflow:

- `plan` scans a media tree and writes:
  - `transcode_jobs.jsonl` (source of truth for execution)
  - `transcode_manifest.csv` (human-readable)
  - `ffmpeg_commands.txt` (one command per line)
- `run` executes `transcode_jobs.jsonl` with configurable concurrency and resumable state in `completed_jobs.jsonl`.

## Project layout

- `scripts/` contains the planner, runner and container entrypoint.
- `work/` is a local scratch directory mounted into the container at `/work`.

## Build

```bash
docker compose build
```

## Plan

Plan outputs next to sources (inside the mounted path):

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  transcode \
  plan /media
```

Plan with a separate output root:

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  -v /path/to/encoded:/encoded \
  transcode \
  plan /media --output-root /encoded
```

HDR behaviour:
- Default: HDR/Dolby Vision sources are planned as SDR tonemapped outputs.
- `--preserve-hdr`: keep HDR outputs instead.
- `--add-sdr`: output both HDR and SDR (implies `--preserve-hdr`).

## Run

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  -v /path/to/encoded:/encoded \
  transcode \
  run --concurrency 2
```

## Resume behaviour

A job is considered complete only if:
- ffmpeg exits with code `0`
- output exists and has non-zero size

On restart:
- completed jobs are skipped
- partial jobs are re-run (outputs overwritten)