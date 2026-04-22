#!/usr/bin/env python3
import argparse
import csv
import json
import shlex
import subprocess
import sys
from pathlib import Path


VIDEO_EXTS_DEFAULT = [".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts"]


def run_ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-print_format", "json",
        str(path),
    ]
    try:
        res = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("ERROR: ffprobe not found in PATH", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        print(f"WARNING: ffprobe failed for {path}: {stderr}", file=sys.stderr)
        return {}

    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        print(f"WARNING: invalid ffprobe JSON for {path}: {e}", file=sys.stderr)
        return {}


def select_video_stream(meta: dict):
    for s in meta.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def parse_duration_seconds(meta: dict) -> float:
    fmt = meta.get("format", {}) or {}
    try:
        d = float(fmt.get("duration", "0") or "0")
    except (TypeError, ValueError):
        d = 0.0
    return d if d > 0 else 1.0


def fmt_hms(total_seconds: float) -> str:
    total = max(0, int(round(total_seconds)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def has_dv(v: dict) -> bool:
    side_data = v.get("side_data_list", []) or []
    for sd in side_data:
        side_type = str(sd.get("side_data_type", "")).lower()
        if "dovi" in side_type or "dolby vision" in side_type:
            return True
        for k in ("dv_profile", "rpu_present_flag", "el_present_flag", "bl_present_flag"):
            try:
                if int(sd.get(k, 0) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass

    profile = str(v.get("profile", "")).lower()
    codec_name = str(v.get("codec_name", "")).lower()
    tags = v.get("tags", {}) or {}

    if "dolby vision" in profile or "dovi" in profile:
        return True
    if "dvhe" in codec_name or "dvh1" in codec_name:
        return True
    if any("dolby vision" in str(val).lower() for val in tags.values()):
        return True
    return False


def is_hdr(v: dict) -> bool:
    cp = str(v.get("color_primaries", "")).lower()
    ct = str(v.get("color_transfer", "")).lower()
    cs = str(v.get("color_space", "")).lower()
    return (
        cp == "bt2020"
        or ct == "smpte2084"
        or ct == "arib-std-b67"
        or cs == "bt2020nc"
        or cs == "bt2020c"
    )


def build_output_name(src: Path, variant: str, hdr_suffix: bool, crf: int, tonemapped: bool) -> str:
    base = src.stem
    name = f"{base}.h265.crf{crf}"
    if variant == "1080":
        name += ".1080"
    if tonemapped:
        name += ".tonemapped"
    if hdr_suffix:
        name += ".hdr"
    name += ".mkv"
    return name


def resolve_output_path(
    src: Path,
    source_root: Path,
    output_root: Path,
    variant: str,
    hdr_suffix: bool,
    crf: int,
    tonemapped: bool,
):
    filename = build_output_name(
        src,
        variant=variant,
        hdr_suffix=hdr_suffix,
        crf=crf,
        tonemapped=tonemapped,
    )

    if output_root is None:
        return src.parent / filename

    rel_parent = src.parent.relative_to(source_root)
    return output_root / rel_parent / filename


def build_filter(is_dv: bool, make_1080: bool, tonemap_to_sdr: bool) -> str:
    # SDR tonemap pipeline based on the user-provided example.
    # Keeps 10-bit output (yuv420p10le) like the example.
    if tonemap_to_sdr:
        chain = [
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p10le",
        ]
        if make_1080:
            chain.append("scale=1920:1080:flags=lanczos")
        return ",".join(chain)

    # Preserve HDR/DV behaviour (existing logic)
    if is_dv:
        if make_1080:
            return (
                "zscale=t=linear:npl=100,"
                "format=gbrpf32le,"
                "zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc,"
                "tonemap=mobius:desat=0,"
                "zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc:range=limited,"
                "format=yuv420p10le,"
                "scale=1920:1080:flags=lanczos"
            )
        return (
            "zscale=t=linear:npl=100,"
            "format=gbrpf32le,"
            "zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc,"
            "tonemap=mobius:desat=0,"
            "zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc:range=limited,"
            "format=yuv420p10le"
        )

    if make_1080:
        return "zscale=1920:1080:filter=lanczos,format=yuv420p10le"

    return "format=yuv420p10le"


def build_x265_params(enable_hdr_signalling: bool) -> str:
    params = ["repeat-headers=1"]
    if enable_hdr_signalling:
        params.extend([
            "hdr-opt=1",
            "colorprim=bt2020",
            "transfer=smpte2084",
            "colormatrix=bt2020nc",
            "range=limited",
        ])
    return ":".join(params)


def shell_join(parts):
    return " ".join(shlex.quote(str(p)) for p in parts)


def build_ffmpeg_command(
    src: Path,
    dst: Path,
    is_dv: bool,
    is_hdr_src: bool,
    make_1080: bool,
    crf: int,
    preset: str,
    tonemap_to_sdr: bool,
):
    vf = build_filter(is_dv=is_dv, make_1080=make_1080, tonemap_to_sdr=tonemap_to_sdr)

    # If tonemapping to SDR, do NOT signal HDR in x265 params.
    enable_hdr_signalling = (not tonemap_to_sdr) and (is_dv or is_hdr_src)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-progress", "pipe:1",
        "-stats",
        "-i", str(src),
        "-map", "0",
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-vf", vf,
        "-c:v", "libx265",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p10le",
        "-profile:v", "main10",
        "-x265-params", build_x265_params(enable_hdr_signalling),
        "-c:a", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        str(dst),
    ]
    return cmd


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Scan a source tree for video files, inspect them with ffprobe, and generate:\n"
            "  1) a plain-text ffmpeg command list\n"
            "  2) a CSV manifest\n"
            "  3) a structured JSONL jobs file for the runner\n\n"
            "By default, outputs are planned next to source files.\n"
            "If --output-root is provided, outputs are written under that directory\n"
            "while preserving source-relative subdirectories.\n\n"
            "HDR handling defaults to planning SDR tonemapped outputs for HDR/DV sources.\n"
            "Use --preserve-hdr to keep HDR outputs instead, and --add-sdr to produce both."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "source_root",
        type=Path,
        help="Root directory to recursively scan for video files.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help=(
            "Optional root directory for transcoded outputs.\n"
            "If omitted, outputs are written next to source files.\n"
            "If provided, source-relative directory structure is preserved under this root."
        ),
    )
    parser.add_argument(
        "--commands-file",
        type=Path,
        default=Path("ffmpeg_commands.txt"),
        help="Path to write one shell-escaped ffmpeg command per line. Default: %(default)s",
    )
    parser.add_argument(
        "--manifest-file",
        type=Path,
        default=Path("transcode_manifest.csv"),
        help="Path to write CSV metadata manifest. Default: %(default)s",
    )
    parser.add_argument(
        "--jobs-file",
        type=Path,
        default=Path("transcode_jobs.jsonl"),
        help="Path to write structured JSONL jobs file. Default: %(default)s",
    )
    parser.add_argument(
        "--video-ext",
        action="append",
        default=[],
        help=(
            "Video extension to include, e.g. --video-ext .mp4\n"
            "May be specified multiple times.\n"
            f"If omitted, defaults to: {', '.join(VIDEO_EXTS_DEFAULT)}"
        ),
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=18,
        help="libx265 CRF value for output encodes. Default: %(default)s",
    )
    parser.add_argument(
        "--preset",
        default="medium",
        help="libx265 preset to use. Default: %(default)s",
    )
    parser.add_argument(
        "--always-1080-for-hdr",
        action="store_true",
        default=False,
        help=(
            "If set, generate 1080p variants for HDR/Dolby Vision sources.\n"
            "Without this flag, 1080p variants are generated only for 4K-or-larger sources."
        ),
    )
    parser.add_argument(
        "--preserve-hdr",
        action="store_true",
        default=False,
        help=(
            "Preserve HDR/Dolby Vision signalling for HDR sources. "
            "By default, HDR sources are planned as SDR tonemapped outputs."
        ),
    )
    parser.add_argument(
        "--add-sdr",
        action="store_true",
        default=False,
        help=(
            "For HDR/Dolby Vision sources, add an additional SDR tonemapped output. "
            "Implies --preserve-hdr."
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # add_sdr implies preserve_hdr
    if args.add_sdr:
        args.preserve_hdr = True

    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve() if args.output_root else None
    commands_file = args.commands_file
    manifest_file = args.manifest_file
    jobs_file = args.jobs_file

    if not source_root.exists():
        print(f"ERROR: source root does not exist: {source_root}", file=sys.stderr)
        sys.exit(1)
    if not source_root.is_dir():
        print(f"ERROR: source root is not a directory: {source_root}", file=sys.stderr)
        sys.exit(1)

    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in (args.video_ext or [])}
    if not exts:
        exts = set(VIDEO_EXTS_DEFAULT)

    files = sorted(
        p for p in source_root.rglob("*")
        if p.is_file() and p.suffix.lower() in exts
    )

    commands = []
    jobs = []
    manifest_rows = []

    for src in files:
        meta = run_ffprobe(src)
        if not meta:
            continue

        v = select_video_stream(meta)
        if not v:
            print(f"WARNING: no video stream: {src}", file=sys.stderr)
            continue

        width = int(v.get("width") or 0)
        height = int(v.get("height") or 0)
        duration_seconds = parse_duration_seconds(meta)
        duration_hms = fmt_hms(duration_seconds)

        dv = has_dv(v)
        hdr_src = is_hdr(v) or dv

        needs_1080 = (width >= 3840 or height >= 2160) or (args.always_1080_for_hdr and hdr_src)

        # Decide outputs:
        # - SDR sources: one output, no tonemap, no HDR suffix.
        # - HDR/DV sources:
        #     default: SDR tonemapped only
        #     --preserve-hdr: HDR only
        #     --add-sdr: HDR + SDR tonemapped (and implies preserve-hdr)
        outputs_to_make = []

        if hdr_src:
            if args.preserve_hdr:
                # HDR output
                outputs_to_make.append(("hdr", False, True))  # label, tonemap_to_sdr, hdr_suffix
            if (not args.preserve_hdr) or args.add_sdr:
                # SDR tonemapped output
                outputs_to_make.append(("sdr", True, False))
        else:
            outputs_to_make.append(("sdr_src", False, False))

        for label, tonemap_to_sdr, hdr_suffix in outputs_to_make:
            tonemapped = bool(tonemap_to_sdr)

            # Native
            native_out = resolve_output_path(
                src=src,
                source_root=source_root,
                output_root=output_root,
                variant="native",
                hdr_suffix=hdr_suffix,
                crf=args.crf,
                tonemapped=tonemapped,
            )
            native_cmd = build_ffmpeg_command(
                src=src,
                dst=native_out,
                is_dv=dv,
                is_hdr_src=hdr_src,
                make_1080=False,
                crf=args.crf,
                preset=args.preset,
                tonemap_to_sdr=tonemap_to_sdr,
            )
            native_job_id = f"{src}::native::{label}"

            commands.append(shell_join(native_cmd))
            jobs.append({
                "job_id": native_job_id,
                "source": str(src),
                "output": str(native_out),
                "variant": "native",
                "width": width,
                "height": height,
                "duration_seconds": duration_seconds,
                "duration_hms": duration_hms,
                "heightxwidthxtime": f"{height}x{width}x{duration_hms}",
                "is_hdr": hdr_src and (not tonemap_to_sdr),
                "is_dv": dv and (not tonemap_to_sdr),
                "needs_1080": needs_1080,
                "tonemapped": tonemap_to_sdr,
                "command": native_cmd,
            })
            manifest_rows.append({
                "job_id": native_job_id,
                "source": str(src),
                "output": str(native_out),
                "variant": "native",
                "width": width,
                "height": height,
                "duration_seconds": f"{duration_seconds:.3f}",
                "duration_hms": duration_hms,
                "heightxwidthxtime": f"{height}x{width}x{duration_hms}",
                "is_hdr": str(hdr_src and (not tonemap_to_sdr)).lower(),
                "is_dv": str(dv and (not tonemap_to_sdr)).lower(),
                "needs_1080": str(needs_1080).lower(),
            })

            # 1080
            if needs_1080:
                out_1080 = resolve_output_path(
                    src=src,
                    source_root=source_root,
                    output_root=output_root,
                    variant="1080",
                    hdr_suffix=hdr_suffix,
                    crf=args.crf,
                    tonemapped=tonemapped,
                )
                cmd_1080 = build_ffmpeg_command(
                    src=src,
                    dst=out_1080,
                    is_dv=dv,
                    is_hdr_src=hdr_src,
                    make_1080=True,
                    crf=args.crf,
                    preset=args.preset,
                    tonemap_to_sdr=tonemap_to_sdr,
                )
                job_1080_id = f"{src}::1080::{label}"

                commands.append(shell_join(cmd_1080))
                jobs.append({
                    "job_id": job_1080_id,
                    "source": str(src),
                    "output": str(out_1080),
                    "variant": "1080",
                    "width": width,
                    "height": height,
                    "duration_seconds": duration_seconds,
                    "duration_hms": duration_hms,
                    "heightxwidthxtime": f"{height}x{width}x{duration_hms}",
                    "is_hdr": hdr_src and (not tonemap_to_sdr),
                    "is_dv": dv and (not tonemap_to_sdr),
                    "needs_1080": needs_1080,
                    "tonemapped": tonemap_to_sdr,
                    "command": cmd_1080,
                })
                manifest_rows.append({
                    "job_id": job_1080_id,
                    "source": str(src),
                    "output": str(out_1080),
                    "variant": "1080",
                    "width": width,
                    "height": height,
                    "duration_seconds": f"{duration_seconds:.3f}",
                    "duration_hms": duration_hms,
                    "heightxwidthxtime": f"{height}x{width}x{duration_hms}",
                    "is_hdr": str(hdr_src and (not tonemap_to_sdr)).lower(),
                    "is_dv": str(dv and (not tonemap_to_sdr)).lower(),
                    "needs_1080": str(needs_1080).lower(),
                })

    commands_file.write_text("\n".join(commands) + ("\n" if commands else ""), encoding="utf-8")

    with manifest_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "job_id",
                "source",
                "output",
                "variant",
                "width",
                "height",
                "duration_seconds",
                "duration_hms",
                "heightxwidthxtime",
                "is_hdr",
                "is_dv",
                "needs_1080",
            ],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)

    with jobs_file.open("w", encoding="utf-8") as f:
        for job in jobs:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")

    print(f"Wrote {len(commands)} command(s) to {commands_file}")
    print(f"Wrote {len(manifest_rows)} manifest row(s) to {manifest_file}")
    print(f"Wrote {len(jobs)} job record(s) to {jobs_file}")


if __name__ == "__main__":
    main()