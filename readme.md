# Batch Transcoder

Simple Dockerized FFmpeg planner/runner.

## Commands

- `plan` → scan media and generate job files
- `run` → execute jobs from the JSONL jobs file

## Build

```bash
docker compose build
```

## Help

```bash
docker compose run --rm transcode
docker compose run --rm transcode plan --help
docker compose run --rm transcode run --help
```

## Plan

Example:

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  transcode \
  plan /media
```

Writes:

- `ffmpeg_commands.txt`
- `transcode_manifest.csv`
- `transcode_jobs.jsonl`

### Plan with separate output root

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  -v /path/to/encoded:/encoded \
  transcode \
  plan /media --output-root /encoded
```

This preserves the directory structure from `/media` under `/encoded`.

## Run

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  transcode \
  run --concurrency 2
```

## Resume behavior

A job is complete only if:

- ffmpeg exit code is `0`
- output exists
- output size is greater than `0`

On restart:

- completed jobs are skipped
- partial jobs are re-run
- partial outputs are overwritten

## Files

- `transcode_jobs.jsonl` — source of truth for execution
- `transcode_manifest.csv` — human-readable manifest
- `ffmpeg_commands.txt` — one ffmpeg command per line
- `completed_jobs.jsonl` — successful jobs
```

---

# 5) Rebuild

After adding the entrypoint:

```bash
docker compose build --no-cache
```

Then use:

```bash
docker compose run --rm transcode plan --help
docker compose run --rm transcode run --help
```
