# Automated HEVC Archival Transcoder

This repository contains a Go-based orchestration service designed for the automated batch transcoding of mixed-format video archives into High-Efficiency Video Coding (HEVC/H.265). The system enforces 10-bit colour depth and high-dynamic-range (HDR) colorimetry across all outputs, while strictly bounding computational constraints via Constant Rate Factor (CRF) encoding.

The application leverages a container-in-container architecture, utilising the Moby Go SDK to spawn ephemeral FFmpeg instances via a heavily restricted Docker Socket Proxy, ensuring cryptographic and operational isolation of the transcoding processes.

## System Architecture
1. **Orchestrator (Go):** A compiled daemon that traverses the input directory, probes container and stream metadata, computes the required transcoding heuristics, and manages a bounded concurrent worker pool.
2. **Socket Proxy:** A least-privilege Docker API proxy (based on HAProxy) that exposes only the specific API endpoints required to provision and destroy ephemeral containers, mitigating the security risks associated with exposing `/var/run/docker.sock`.
3. **Ephemeral Transcoders:** Stateless `lscr.io/linuxserver/ffmpeg` containers spawned on-demand for `ffprobe` metadata extraction and `ffmpeg` encoding tasks.

## Transcoding Heuristics & Colorimetry
The system applies deterministic rulesets based on source stream characteristics:

* **Target Codec:** `libx265` (HEVC) at CRF 18, `medium` preset.
* **Colorimetry Enforcement:** All outputs are strictly encoded at 10-bit depth (`yuv420p10le`) and flagged with BT.2020 primaries, SMPTE ST 2084 (PQ) transfer characteristics, and BT.2020 non-constant luminance matrix, regardless of the source transfer function.
* **Dolby Vision (DV) Handling:** Sources containing DV dynamic metadata (side data) are intercepted and subjected to a deterministic tone-mapping pipeline (via `zscale`) to produce an HDR10-compatible output.
* **Resolution Scaling:** 
  * A native-resolution output is generated for all files.
  * If the source resolution is $\ge$ 4K (3840x2160) or natively HDR, a secondary downsampled 1080p output is additionally generated using the Lanczos resampling algorithm.

### Output Nomenclature
Output files adopt the following suffix conventions:
* `*.h265.crf18.mkv`: Standard native output.
* `*.h265.crf18.1080.mkv`: Downsampled 1080p output.
* `*.hdr.mkv`: Indicates that active colour-mapping was applied (specifically for Dolby Vision sources converted to HDR10).

## Security Considerations
* **Restricted API Access:** The orchestrator communicates exclusively via a proxy where execution, volume manipulation, and unauthenticated build contexts are explicitly dropped.
* **Immutable Inputs:** The source archive is mounted strictly as read-only (`ro`).
* **Privilege Dropping:** Ephemeral containers are spawned with `no-new-privileges:true` and restricted network capabilities (`NetworkMode: "none"`).

## Deployment

### Prerequisites
* Docker

### Configuration
Adjust operational parameters via the `environment` block in `docker-compose.yml`:
* `JOBS`: Maximum number of concurrent transcoding containers (Default: `2`).
* `INPUT_DIR`: Absolute path to the source archive.
* `OUTPUT_DIR`: Absolute path to the destination directory.

### Execution
Initiate the orchestrator using Docker Compose:

```bash
# Build the Go orchestrator image
docker compose build

# Start the proxy and begin batch processing
docker compose up
```

To run the orchestrator as a foreground, one-shot process:
```bash
docker compose run --rm transcoder
```

## Caveats
1. **SDR to HDR Upconversion:** By design, standard dynamic range (SDR) sources are flagged with HDR metadata without complex inverse tone-mapping. This satisfies specific archival uniformity requirements but may result in non-standard visual presentation on highly calibrated displays.
2. **Metadata Attrition:** Dolby Vision dynamic metadata (RPU) is deliberately stripped and approximated via tone-mapping to ensure playback compatibility across standard HDR10 architectures.