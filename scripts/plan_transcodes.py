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
    # Your sample has:
    # side_data_type: "DOVI configuration record"
    # dv_profile + rpu_present_flag etc.
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

    # Safety: only preserve structure if src is under source_root
    try:
        rel_parent = src.parent.relative_to(source_root)
    except ValueError:
        # Shouldn't happen because we rglob under source_root, but be defensive.
        rel_parent = Path()

    return output_root / rel_parent / filename


def build_filter(*, is_dv: bool, make_1080: bool, tonemap_to_sdr: bool, npl: int, tonemap_operator: str) -> str:
    # Tonemap to SDR: use npl configurable (default 83)
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

    # HDR/DV (not tonemapping): keep your existing behaviour but npl=83 (configurable)
    if is_dv:
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

    if make_1080:
        return "zscale=1920:1080:filter=lanczos,format=yuv420p10le"

    return "format=yuv420p10le"


def build_x265_params(*, enable_hdr_signalling: bool, vbv_maxrate: int | None, vbv_bufsize: int | None) -> str:
    params = ["repeat-headers=1"]
    if enable_hdr_signalling:
        params.extend([
            "hdr-opt=1",
            "colorprim=bt2020",
            "transfer=smpte2084",
            "colormatrix=bt2020nc",
            "range=limited",
        ])
    # DV guidance: include VBV constraints when doing DV RPU coding (user asked; make configurable)
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
        make_1080=make_1080,
        tonemap_to_sdr=tonemap_to_sdr,
        npl=npl,
        tonemap_operator=tonemap_operator,
    )

    # If tonemapping to SDR, do NOT signal HDR in x265 params.
    enable_hdr_signalling = (not tonemap_to_sdr) and (is_hdr_src or is_dv)

    # Only apply DV features when actually DV and not tonemapping.
    enable_dolbyvision = is_dv and (not tonemap_to_sdr)

    vbv_maxrate = dv_vbv_maxrate if enable_dolbyvision else None
    vbv_bufsize = dv_vbv_bufsize if enable_dolbyvision else None

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
    ]

    if enable_dolbyvision:
        # FFmpeg 8.0.1 + libx265 supports this in your container.
        cmd += ["-dolbyvision", "1"]

    cmd += [
        "-x265-params", build_x265_params(
            enable_hdr_signalling=enable_hdr_signalling,
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
            "Defaults:\n"
            "- SDR sources: native + 1080 (only when source is >=2160)\n"
            "- HDR/DV sources:\n"
            "    - if source is >=2160: native HDR + 1080 HDR + native SDR-tonemapped + 1080 SDR-tonemapped\n"
            "    - if source is <2160: native HDR + native SDR-tonemapped\n\n"
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

    # HDR/SDR selection toggles (only affect HDR/DV sources)
    g_hdr = parser.add_mutually_exclusive_group()
    g_hdr.add_argument(
        "--only-hdr-from-hdr",
        action="store_true",
        help="For HDR/DV sources, output only HDR/DV. SDR sources still encoded as SDR.",
    )
    g_hdr.add_argument(
        "--only-sdr-from-hdr",
        action="store_true",
        help="For HDR/DV sources, output only SDR-tonemapped. SDR sources still encoded as SDR.",
    )

    # Resolution selection toggles (only affect >=2160 sources)
    g_res = parser.add_mutually_exclusive_group()
    g_res.add_argument(
        "--only-native",
        action="store_true",
        help="Do not produce 1080 variants (native only).",
    )
    g_res.add_argument(
        "--only-1080-from-2160",
        action="store_true",
        help="For sources >=2160, output only the 1080 variant (no native). For <2160, keep native.",
    )

    # Tonemap / DV knobs
    parser.add_argument(
        "--npl",
        type=int,
        default=83,
        help="zscale npl value used in HDR pipelines. Default: %(default)s",
    )

    parser.add_argument(
        "--dv-vbv-maxrate",
        type=int,
        default=40000,
        help="Dolby Vision (libx265 -dolbyvision 1) vbv-maxrate value. Default: %(default)s",
    )
    parser.add_argument(
        "--dv-vbv-bufsize",
        type=int,
        default=40000,
        help="Dolby Vision (libx265 -dolbyvision 1) vbv-bufsize value. Default: %(default)s",
    )

    parser.add_argument(
        "--tonemap-operator",
        default="reinhard",
        choices=["reinhard", "hable", "mobius", "luma"],
        help="Tonemap operator used when producing SDR-tonemapped outputs. Default: %(default)s",
    )

    return parser.parse_args()


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

        is_2160_or_more = (width >= 3840 or height >= 2160)
        would_make_1080 = is_2160_or_more and (not args.only_native)

        # Apply resolution selector:
        # - default for >=2160: native + 1080
        # - --only-native: native only
        # - --only-1080-from-2160: 1080 only (but only when >=2160)
        make_native_variant = True
        make_1080_variant = would_make_1080

        if args.only_1080_from_2160 and is_2160_or_more:
            make_native_variant = False
            make_1080_variant = True

        # Decide which "colour outputs" to make for HDR sources
        # Defaults for HDR sources: both HDR and SDR-tonemapped
        want_hdr_output = hdr_src
        want_sdr_tonemap_output = hdr_src

        if hdr_src:
            if args.only_hdr_from_hdr:
                want_sdr_tonemap_output = False
            if args.only_sdr_from_hdr:
                want_hdr_output = False

        # Suffix rules:
        # - DV HDR outputs: ".dv-hdr"
        # - HDR10 outputs: ".hdr"
        # - SDR outputs: no suffix
        def hdr_suffix_for_stream() -> str | None:
            if not hdr_src:
                return None
            if dv:
                return "dv-hdr"
            return "hdr"

        outputs_to_make: list[tuple[str, bool, str | None]] = []
        # tuple: (label, tonemap_to_sdr, suffix)

        if hdr_src:
            if want_hdr_output:
                outputs_to_make.append(("hdr", False, hdr_suffix_for_stream()))
            if want_sdr_tonemap_output:
                outputs_to_make.append(("sdr", True, None))
        else:
            outputs_to_make.append(("sdr_src", False, None))

        for label, tonemap_to_sdr, suffix in outputs_to_make:
            tonemapped = bool(tonemap_to_sdr)

            def add_job(variant: str, make_1080: bool):
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

            if make_native_variant:
                add_job("native", make_1080=False)
            if make_1080_variant:
                add_job("1080", make_1080=True)

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