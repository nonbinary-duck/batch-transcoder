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
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
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


def build_output_name(
    src: Path,
    variant: str,
    suffix: str | None,
    crf: int,
    tonemapped: bool,
) -> str:
    base = src.stem
    name = f"{base}.h265.crf{crf}"
    if variant == "1080":
        name += ".1080"
    if tonemapped:
        name += ".tonemapped"
    if suffix:
        name += f".{suffix}"
    name += ".mkv"
    return name


def resolve_output_path(
    src: Path,
    source_root: Path,
    output_root: Path | None,
    variant: str,
    suffix: str | None,
    crf: int,
    tonemapped: bool,
) -> Path:
    filename = build_output_name(
        src,
        variant=variant,
        suffix=suffix,
        crf=crf,
        tonemapped=tonemapped,
    )

    if output_root is None:
        return src.parent / filename

    try:
        rel_parent = src.parent.relative_to(source_root)
    except ValueError:
        rel_parent = Path()

    return output_root / rel_parent / filename


def build_filter(*, is_dv: bool, is_hdr_src: bool, make_1080: bool, tonemap_to_sdr: bool, npl: int, tonemap_operator: str) -> str:
    # Standard SDR pass-through (no tonemapping, just scale if needed and output 10-bit)
    if not is_hdr_src:
        chain = []
        if make_1080:
            chain.append("scale=1920:1080:flags=lanczos")
        chain.append("format=yuv420p10le")
        return ",".join(chain)

    # Tonemap HDR to SDR: use npl configurable
    if tonemap_to_sdr:
        chain = [
            f"zscale=t=linear:npl={npl}",
            "format=gbrpf32le",
            "zscale=p=bt709",
            f"tonemap={tonemap_operator}:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p10le",
        ]
        if make_1080:
            chain.append("scale=1920:1080:flags=lanczos")
        return ",".join(chain)

    # Dolby Vision: strictly pass-through (no tone-mapping/zscale), but enforce 12-bit
    if is_dv:
        chain = []
        if make_1080:
            chain.append("scale=1920:1080:flags=lanczos")
        chain.append("format=yuv420p12le")
        return ",".join(chain)

    # Standard HDR10 (not DV, not tonemapping)
    chain = [
        f"zscale=t=linear:npl={npl}",
        "format=gbrpf32le",
        "zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc",
        f"tonemap={tonemap_operator}:desat=0",
        "zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc:range=limited",
        "format=yuv420p10le",
    ]
    if make_1080:
        chain.append("scale=1920:1080:flags=lanczos")
    return ",".join(chain)


def build_x265_params(
    *, 
    enable_hdr_signalling: bool, 
    enable_dolbyvision: bool, 
    vbv_maxrate: int | None, 
    vbv_bufsize: int | None
) -> str:
    params = ["repeat-headers=1"]
    
    if enable_dolbyvision:
        params.extend([
            "hdr-opt=1",
            "dolby-vision-profile=8.1"
        ])
    elif enable_hdr_signalling:
        params.extend([
            "hdr-opt=1",
            "colorprim=bt2020",
            "transfer=smpte2084",
            "colormatrix=bt2020nc",
            "range=limited",
        ])
        
    if vbv_maxrate is not None:
        params.append(f"vbv-maxrate={int(vbv_maxrate)}")
    if vbv_bufsize is not None:
        params.append(f"vbv-bufsize={int(vbv_bufsize)}")
    return ":".join(params)


def shell_join(parts):
    return " ".join(shlex.quote(str(p)) for p in parts)


def build_ffmpeg_command(
    *,
    src: Path,
    dst: Path,
    is_dv: bool,
    is_hdr_src: bool,
    make_1080: bool,
    crf: int,
    preset: str,
    tonemap_to_sdr: bool,
    npl: int,
    tonemap_operator: str,
    dv_vbv_maxrate: int,
    dv_vbv_bufsize: int,
) -> list[str]:
    vf = build_filter(
        is_dv=is_dv,
        is_hdr_src=is_hdr_src,
        make_1080=make_1080,
        tonemap_to_sdr=tonemap_to_sdr,
        npl=npl,
        tonemap_operator=tonemap_operator,
    )

    # Standard HDR signalling (only if HDR, NOT tonemapping, and NOT DV)
    enable_hdr_signalling = (not tonemap_to_sdr) and is_hdr_src and (not is_dv)
    # DV Signalling (only if DV and NOT tonemapping)
    enable_dolbyvision = is_dv and (not tonemap_to_sdr)

    vbv_maxrate = args_dv_vbv_maxrate = dv_vbv_maxrate if enable_dolbyvision else None
    vbv_bufsize = args_dv_vbv_bufsize = dv_vbv_bufsize if enable_dolbyvision else None

    # Determine 10-bit vs 12-bit output requirements
    pix_fmt = "yuv420p12le" if enable_dolbyvision else "yuv420p10le"
    profile = "main12" if enable_dolbyvision else "main10"

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
        "-pix_fmt", pix_fmt,
        "-profile:v", profile,
    ]

    cmd += [
        "-x265-params", build_x265_params(
            enable_hdr_signalling=enable_hdr_signalling,
            enable_dolbyvision=enable_dolbyvision,
            vbv_maxrate=vbv_maxrate,
            vbv_bufsize=vbv_bufsize,
        ),
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
            "Default Output Logic:\n"
            "  - HDR/DV sources: Produces one Native resolution HDR output, AND one 1080p SDR tonemapped output.\n"
            "  - SDR sources: Produces one Native resolution SDR output.\n\n"
            "MKV output only."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("source_root", type=Path, help="Root directory to recursively scan for video files.")
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
    parser.add_argument("--commands-file", type=Path, default=Path("ffmpeg_commands.txt"))
    parser.add_argument("--manifest-file", type=Path, default=Path("transcode_manifest.csv"))
    parser.add_argument("--jobs-file", type=Path, default=Path("transcode_jobs.jsonl"))

    parser.add_argument(
        "--video-ext",
        action="append",
        default=[],
        help=("Video extension to include, e.g. --video-ext .mp4 (repeatable)."),
    )

    parser.add_argument("--crf", type=int, default=18, help="libx265 CRF value. Default: %(default)s")
    parser.add_argument("--preset", default="medium", help="libx265 preset. Default: %(default)s")

    out_group = parser.add_argument_group("Output Resolutions")
    out_group.add_argument(
        "--hdr-out",
        choices=["none", "native", "1080", "both"],
        default="native",
        help="Target resolutions for HDR/DV outputs from HDR sources.\nDefault: %(default)s",
    )
    out_group.add_argument(
        "--sdr-tonemap-out",
        choices=["none", "native", "1080", "both"],
        default="1080",
        help="Target resolutions for SDR tonemapped outputs from HDR sources.\nDefault: %(default)s",
    )
    out_group.add_argument(
        "--sdr-out",
        choices=["none", "native", "1080", "both"],
        default="native",
        help="Target resolutions for standard SDR outputs from SDR sources.\nDefault: %(default)s",
    )

    filters = parser.add_argument_group("Filter & Codec Settings")
    filters.add_argument(
        "--npl",
        type=int,
        default=83,
        help="zscale npl value used in HDR pipelines. Default: %(default)s",
    )
    filters.add_argument(
        "--dv-vbv-maxrate",
        type=int,
        default=40000,
        help="Dolby Vision (libx265 dolby-vision-profile) vbv-maxrate value. Default: %(default)s",
    )
    filters.add_argument(
        "--dv-vbv-bufsize",
        type=int,
        default=40000,
        help="Dolby Vision (libx265 dolby-vision-profile) vbv-bufsize value. Default: %(default)s",
    )
    filters.add_argument(
        "--tonemap-operator",
        default="reinhard",
        choices=["reinhard", "hable", "mobius", "luma"],
        help="Tonemap operator used when producing SDR-tonemapped outputs. Default: %(default)s",
    )

    return parser.parse_args()


def get_resolutions_from_choice(choice: str) -> list[str]:
    if choice == "none":
        return []
    if choice == "native":
        return ["native"]
    if choice == "1080":
        return ["1080"]
    return ["native", "1080"]


def main():
    args = parse_args()

    if args.npl < 1:
        print("ERROR: --npl must be >= 1", file=sys.stderr)
        sys.exit(2)

    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve() if args.output_root else None

    if not source_root.exists():
        print(f"ERROR: source root does not exist: {source_root}", file=sys.stderr)
        sys.exit(1)
    if not source_root.is_dir():
        print(f"ERROR: source root is not a directory: {source_root}", file=sys.stderr)
        sys.exit(1)

    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in (args.video_ext or [])}
    if not exts:
        exts = set(VIDEO_EXTS_DEFAULT)

    files = sorted(p for p in source_root.rglob("*") if p.is_file() and p.suffix.lower() in exts)

    commands: list[str] = []
    jobs: list[dict] = []
    manifest_rows: list[dict] = []

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
        hdr10ish = is_hdr(v)
        hdr_src = hdr10ish or dv

        def hdr_suffix_for_stream() -> str:
            return "dv-hdr" if dv else "hdr"

        # List of (label, tonemap_to_sdr, suffix, target_resolutions)
        outputs_to_make: list[tuple[str, bool, str | None, list[str]]] = []

        if hdr_src:
            hdr_resolutions = get_resolutions_from_choice(args.hdr_out)
            if hdr_resolutions:
                outputs_to_make.append(("hdr", False, hdr_suffix_for_stream(), hdr_resolutions))

            sdr_tonemap_resolutions = get_resolutions_from_choice(args.sdr_tonemap_out)
            if sdr_tonemap_resolutions:
                outputs_to_make.append(("sdr", True, None, sdr_tonemap_resolutions))
        else:
            sdr_resolutions = get_resolutions_from_choice(args.sdr_out)
            if sdr_resolutions:
                outputs_to_make.append(("sdr_src", False, None, sdr_resolutions))

        for label, tonemap_to_sdr, suffix, resolutions in outputs_to_make:
            tonemapped = bool(tonemap_to_sdr)

            for variant in resolutions:
                make_1080 = (variant == "1080")

                out_path = resolve_output_path(
                    src=src,
                    source_root=source_root,
                    output_root=output_root,
                    variant=variant,
                    suffix=suffix,
                    crf=args.crf,
                    tonemapped=tonemapped,
                )
                cmd = build_ffmpeg_command(
                    src=src,
                    dst=out_path,
                    is_dv=dv,
                    is_hdr_src=hdr_src,
                    make_1080=make_1080,
                    crf=args.crf,
                    preset=args.preset,
                    tonemap_to_sdr=tonemap_to_sdr,
                    npl=args.npl,
                    tonemap_operator=args.tonemap_operator,
                    dv_vbv_maxrate=args.dv_vbv_maxrate,
                    dv_vbv_bufsize=args.dv_vbv_bufsize,
                )
                job_id = f"{src}::{variant}::{label}"
                commands.append(shell_join(cmd))
                jobs.append({
                    "job_id": job_id,
                    "source": str(src),
                    "output": str(out_path),
                    "variant": variant,
                    "width": width,
                    "height": height,
                    "duration_seconds": duration_seconds,
                    "duration_hms": duration_hms,
                    "heightxwidthxtime": f"{height}x{width}x{duration_hms}",
                    "is_hdr": hdr_src and (not tonemap_to_sdr),
                    "is_dv": dv and (not tonemap_to_sdr),
                    "tonemapped": tonemap_to_sdr,
                    "command": cmd,
                })
                manifest_rows.append({
                    "job_id": job_id,
                    "source": str(src),
                    "output": str(out_path),
                    "variant": variant,
                    "width": width,
                    "height": height,
                    "duration_seconds": f"{duration_seconds:.3f}",
                    "duration_hms": duration_hms,
                    "heightxwidthxtime": f"{height}x{width}x{duration_hms}",
                    "is_hdr": str(hdr_src and (not tonemap_to_sdr)).lower(),
                    "is_dv": str(dv and (not tonemap_to_sdr)).lower(),
                })


    args.commands_file.write_text("\n".join(commands) + ("\n" if commands else ""), encoding="utf-8")

    with args.manifest_file.open("w", newline="", encoding="utf-8") as f:
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
            ],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)

    with args.jobs_file.open("w", encoding="utf-8") as f:
        for job in jobs:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")

    print(f"Wrote {len(commands)} command(s) to {args.commands_file}")
    print(f"Wrote {len(manifest_rows)} manifest row(s) to {args.manifest_file}")
    print(f"Wrote {len(jobs)} job record(s) to {args.jobs_file}")


if __name__ == "__main__":
    main()