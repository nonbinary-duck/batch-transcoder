# batch-transcoder

Batch transcoder orchestrator (Go + Moby) that spawns an `ffprobe`/`ffmpeg` Docker container per command via a Docker socket proxy.

## Deployment

### Build
Build the orchestrator image locally:

```bash
docker compose build
```

### Execution
The proxy is automatically started due to `depends_on: dockerproxy` in our `transcoder` spesification.

Run the transcoder as a one-shot process using `docker compose run`, passing volumes and environment overrides there:

```bash
docker compose run --rm \
  -v /path/to/source:/input:ro \
  -v /path/to/output:/output:rw \
  -e JOBS=2 \
  transcoder
```

Environment variables you may wish to override:
- `JOBS` (default: `2`): Maximum number of concurrent ffmpeg transcodes.
- `FFMPEG_IMAGE` (default: `lscr.io/linuxserver/ffmpeg:latest`): ffmpeg container image to run.
- `PULL_MISSING` (default: `true`): Pull the ffmpeg image if it is not present locally.

Notes:
- `/input` and `/output` are container paths. The app automatically inspects its own container mounts to discover the corresponding host paths and uses those when creating ffprobe/ffmpeg containers.

## Output naming
- Native output: `name.h265.crf18.mkv`
- Extra 1080p output (HDR or 4K sources): `name.h265.crf18.1080.mkv`
- If Dolby Vision is detected (colour-mapped to HDR10-ish): add `.hdr` before `.mkv`
  - `name.h265.crf18.hdr.mkv`
  - `name.h265.crf18.1080.hdr.mkv`

## Caveats
1. **SDR to HDR signalling**: SDR sources are encoded and signalled as HDR (BT.2020/PQ) to satisfy your uniformity requirement. This is not a proper artistic SDR→HDR grade and may look non-standard on some displays.
2. **Dolby Vision metadata**: Dolby Vision dynamic metadata (RPU) is not preserved. DV sources are tone-mapped to an HDR10-style output for compatibility.
