# Batch Transcoder (Planner & Runner)

A Dockerised FFmpeg workflow for bulk processing video files with correct HDR, Dolby Vision, and SDR handling.

The system uses a two-phase architecture:
- `plan` scans a media tree and generates structured task files.
  - `transcode_jobs.jsonl` (source of truth for the runner)
  - `transcode_manifest.csv` (human-readable spreadsheet of jobs)
  - `ffmpeg_commands.txt` (a plain-text list of commands)
- `run` executes the jobs in `transcode_jobs.jsonl` with configurable concurrency, maintaining state in `completed_jobs.jsonl` to allow seamless resuming.

## Build

```bash
docker compose build
```

## Phase 1: Plan Jobs

The planner analyses the media and decides what FFmpeg commands need to be executed. 

**Default Behaviour:**
- **HDR / Dolby Vision sources:** Plans one `native` resolution HDR output AND one `1080p` SDR tonemapped output.
- **SDR sources:** Plans one `native` resolution SDR output.

To plan outputs next to your source files:

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  transcode \
  plan /media
```

To plan outputs to a completely separate directory (retaining folder structure):

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  -v /path/to/encoded:/encoded \
  transcode \
  plan /media --output-root /encoded
```

### Overriding Planned Resolutions
You can explicitly define which resolutions are created using the planner flags (`none`, `native`, `1080`, `both`).

```bash
# Example: Only produce native HDR (no SDR tonemapping) from HDR sources,
# and produce both native and 1080p outputs for SDR sources.
docker compose run --rm \
  -v /path/to/media:/media \
  transcode plan /media \
  --hdr-out native \
  --sdr-tonemap-out none \
  --sdr-out both
```

## Phase 2: Run Executions

Once the `transcode_jobs.jsonl` file is written, you can begin the encoding process.

```bash
docker compose run --rm \
  -v /path/to/media:/media \
  -v /path/to/encoded:/encoded \
  transcode \
  run --concurrency 2
```

*(Note: the `/encoded` volume mount is only required during execution if you passed `--output-root` during the planning phase).*

### Resume Behaviour

The runner is fully stateless apart from `completed_jobs.jsonl`. A job is considered complete only if:
1. `ffmpeg` exits with code `0`.
2. The output file exists and has a non-zero size.

If you cancel the run (`Ctrl+C`) or a container restarts:
- Completely encoded jobs are immediately skipped.
- Partially encoded jobs are overwritten and restarted from scratch.