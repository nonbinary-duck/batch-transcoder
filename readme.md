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

## Run as your user (avoid root-owned outputs)
<<<<<<< HEAD
=======
> [!WARNING]
> This will fail (during the planner) with the error `PermissionError: [Errno 13] Permission denied: 'ffmpeg_commands.txt'`
> If I fix it, I will remove this warning. Feel free to make a 5-min PR.
>>>>>>> 0c98fcd (Fix markdown syntax)

When you bind-mount host directories into a container, files created in the mount will be owned by the container user. To avoid root-owned files on your host, run the container with your current UID:GID.

### Linux/macOS (Docker Engine)

Use `--user "$(id -u):$(id -g)"` in commands below.

> Note: On some macOS setups, ownership mapping behaves differently, but using `--user` is still a safe default.

## Plan

Plan outputs next to sources (inside the mounted path):

```bash
docker compose run --rm \
  --user "$(id -u):$(id -g)" \
  -v /path/to/media:/media \
  transcode \
  plan /media
```

Plan with a separate output root:

```bash
docker compose run --rm \
  --user "$(id -u):$(id -g)" \
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
  --user "$(id -u):$(id -g)" \
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
