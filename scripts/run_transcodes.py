#!/usr/bin/env python3
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


def load_jobs(path: Path):
    jobs = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            jobs.append(json.loads(line))
    return jobs


def load_completed(path: Path):
    completed = set()
    if not path.exists():
        return completed

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                job_id = rec.get("job_id")
                if job_id:
                    completed.add(job_id)
            except json.JSONDecodeError:
                continue
    return completed


def append_completed(path: Path, record: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def is_output_complete(output_path: str) -> bool:
    p = Path(output_path)
    return p.exists() and p.is_file() and p.stat().st_size > 0


def fmt_hms(total_seconds: float) -> str:
    if total_seconds is None or total_seconds < 0:
        total_seconds = 0
    total = int(round(total_seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


class ActiveState:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}
        self.completed_duration = 0.0
        self.total_duration = 0.0
        self.done_count = 0
        self.total_count = 0
        self.started = time.time()
        self.last_status_print = 0.0

    def set_totals(self, jobs):
        self.total_duration = sum(float(j.get("duration_seconds", 0.0) or 0.0) for j in jobs)
        self.total_count = len(jobs)

    def start_job(self, job):
        is_dv = job.get("is_dv", False)
        is_hdr = job.get("is_hdr", False)
        tonemapped = job.get("tonemapped", False)
        
        if is_dv:
            fmt_str = "DV HDR"
        elif is_hdr:
            fmt_str = "HDR10"
        elif tonemapped:
            fmt_str = "SDR (Tonemapped)"
        else:
            fmt_str = "SDR"

        with self.lock:
            self.active[job["job_id"]] = {
                "job_id": job["job_id"],
                "source": job["source"],
                "output": job["output"],
                "variant": job["variant"],
                "display_format": fmt_str,
                "duration": float(job.get("duration_seconds", 0.0) or 0.0),
                "progress_seconds": 0.0,
                "speed": "",
                "started": time.time(),
            }

    def update_job(self, job_id, progress_seconds=None, speed=None):
        with self.lock:
            if job_id not in self.active:
                return
            if progress_seconds is not None:
                self.active[job_id]["progress_seconds"] = max(0.0, progress_seconds)
            if speed is not None:
                self.active[job_id]["speed"] = speed

    def finish_job(self, job_id, duration, ok=True):
        with self.lock:
            self.active.pop(job_id, None)
            self.done_count += 1
            if ok:
                self.completed_duration += duration

    def snapshot(self):
        with self.lock:
            return {
                "active": dict(self.active),
                "completed_duration": self.completed_duration,
                "total_duration": self.total_duration,
                "done_count": self.done_count,
                "total_count": self.total_count,
                "started": self.started,
            }


def print_status(state: ActiveState):
    snap = state.snapshot()
    active = snap["active"]

    active_sum = sum(min(v["progress_seconds"], v["duration"]) for v in active.values())
    done = snap["completed_duration"] + active_sum
    total = snap["total_duration"]
    pct = (done / total * 100.0) if total > 0 else 0.0

    elapsed = time.time() - snap["started"]
    eta = 0.0
    if done > 0:
        est_total = elapsed * (total / done)
        eta = max(0.0, est_total - elapsed)

    print(
        f"STATUS | overall {pct:6.2f}% | done {snap['done_count']}/{snap['total_count']} "
        f"| elapsed {fmt_hms(elapsed)} | ETA {fmt_hms(eta)} | active {len(active)}"
    )

    for job_id in sorted(active.keys()):
        j = active[job_id]
        prog = min(j["progress_seconds"], j["duration"])
        jpct = (prog / j["duration"] * 100.0) if j["duration"] > 0 else 0.0
        
        variant_label = "1080p" if j['variant'] == "1080" else j['variant']
        
        print(
            f"  RUNNING | {Path(j['source']).name} [{variant_label} {j['display_format']}] "
            f"{jpct:6.2f}% {fmt_hms(prog)}/{fmt_hms(j['duration'])} "
            f"speed {j['speed'] or '?'}"
        )


def parse_progress_stream(stdout, job, state: ActiveState):
    out_time_us = 0.0
    speed = ""
    for raw in iter(stdout.readline, ""):
        line = raw.strip()
        if not line:
            continue

        if line.startswith("out_time_ms="):
            try:
                out_time_us = float(line.split("=", 1)[1].strip())
                state.update_job(
                    job["job_id"],
                    progress_seconds=(out_time_us / 1_000_000.0),
                    speed=speed,
                )
            except ValueError:
                pass
        elif line.startswith("speed="):
            speed = line.split("=", 1)[1].strip()
            state.update_job(job["job_id"], speed=speed)


def stderr_drain(stderr):
    for _ in iter(stderr.readline, ""):
        pass


def worker(job_queue: queue.Queue, state: ActiveState, completed_set: set, completed_lock: threading.Lock, completed_file: Path):
    while True:
        job = job_queue.get()
        if job is None:
            job_queue.task_done()
            return

        job_id = job["job_id"]
        output = job["output"]
        duration = float(job.get("duration_seconds", 0.0) or 0.0)

        with completed_lock:
            already_complete = job_id in completed_set

        if already_complete and is_output_complete(output):
            print(f"DONE (skip): {job_id} -> {output}")
            state.finish_job(job_id, duration, ok=True)
            job_queue.task_done()
            continue

        Path(output).parent.mkdir(parents=True, exist_ok=True)
        state.start_job(job)
        cmd = job["command"]

        print(f"START: {job_id} -> {output}")

        src_path = Path(job["source"])
        if not src_path.exists() or not src_path.is_file():
            print(f"ERROR: input missing, skipping without touching output: {src_path}", file=sys.stderr)
            state.finish_job(job_id, duration, ok=False)
            job_queue.task_done()
            continue

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print("ERROR: ffmpeg not found in PATH", file=sys.stderr)
            state.finish_job(job_id, duration, ok=False)
            job_queue.task_done()
            continue
        except Exception as e:
            print(f"ERROR starting {job_id}: {e}", file=sys.stderr)
            state.finish_job(job_id, duration, ok=False)
            job_queue.task_done()
            continue

        t_out = threading.Thread(target=parse_progress_stream, args=(proc.stdout, job, state), daemon=True)
        t_err = threading.Thread(target=stderr_drain, args=(proc.stderr,), daemon=True)
        t_out.start()
        t_err.start()

        rc = proc.wait()

        t_out.join(timeout=2)
        t_err.join(timeout=2)

        ok = (rc == 0 and is_output_complete(output))
        if ok:
            rec = {
                "job_id": job_id,
                "source": job["source"],
                "output": output,
                "variant": job["variant"],
                "duration_seconds": duration,
                "completed_at_unix": time.time(),
            }
            with completed_lock:
                append_completed(completed_file, rec)
                completed_set.add(job_id)
            print(f"DONE: {job_id} -> {output}")
            state.finish_job(job_id, duration, ok=True)
        else:
            print(f"ERROR: {job_id} failed with exit code {rc}", file=sys.stderr)
            state.finish_job(job_id, duration, ok=False)

        job_queue.task_done()


def status_loop(state: ActiveState, stop_event: threading.Event, interval: int):
    while not stop_event.wait(interval):
        print_status(state)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Run ffmpeg transcode jobs from a JSONL jobs file.\n\n"
            "Logging is simple:\n"
            "  - immediate START/DONE/ERROR lines\n"
            "  - status summary printed every N seconds\n"
            "  - no console clearing"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "--jobs-file",
        type=Path,
        default=Path("transcode_jobs.jsonl"),
        help="Structured JSONL jobs file produced by the planner. Default: %(default)s",
    )
    parser.add_argument(
        "--manifest-file",
        type=Path,
        default=Path("transcode_manifest.csv"),
        help="CSV manifest file to verify exists. Default: %(default)s",
    )
    parser.add_argument(
        "--completed-file",
        type=Path,
        default=Path("completed_jobs.jsonl"),
        help="JSONL file used to persist successfully completed jobs. Default: %(default)s",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Number of ffmpeg processes to run simultaneously. Default: %(default)s",
    )
    parser.add_argument(
        "--longest-first",
        action="store_true",
        help="Sort pending jobs by descending duration before execution.",
    )
    parser.add_argument(
        "--status-interval",
        type=int,
        default=10,
        help="Seconds between status updates. Default: %(default)s",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.concurrency < 1:
        print("ERROR: --concurrency must be >= 1", file=sys.stderr)
        sys.exit(1)

    if args.status_interval < 1:
        print("ERROR: --status-interval must be >= 1", file=sys.stderr)
        sys.exit(1)

    if not args.jobs_file.exists():
        print(f"ERROR: missing jobs file: {args.jobs_file}", file=sys.stderr)
        sys.exit(1)

    if not args.manifest_file.exists():
        print(f"ERROR: missing manifest file: {args.manifest_file}", file=sys.stderr)
        sys.exit(1)

    jobs = load_jobs(args.jobs_file)
    if not jobs:
        print("No jobs found.")
        return

    completed_set = load_completed(args.completed_file)
    completed_lock = threading.Lock()

    pending_jobs = []
    for j in jobs:
        if j["job_id"] in completed_set and is_output_complete(j["output"]):
            continue
        pending_jobs.append(j)

    if args.longest_first:
        pending_jobs.sort(key=lambda j: float(j.get("duration_seconds", 0.0) or 0.0), reverse=True)

    if not pending_jobs:
        print("All jobs already completed.")
        return

    state = ActiveState()
    state.set_totals(pending_jobs)

    print(f"Starting {len(pending_jobs)} job(s) with concurrency={args.concurrency}")
    print(f"Status updates every {args.status_interval} seconds")
    print_status(state)

    job_queue = queue.Queue()
    workers = []

    for _ in range(args.concurrency):
        t = threading.Thread(
            target=worker,
            args=(job_queue, state, completed_set, completed_lock, args.completed_file),
            daemon=True,
        )
        t.start()
        workers.append(t)

    for job in pending_jobs:
        job_queue.put(job)

    for _ in workers:
        job_queue.put(None)

    stop_event = threading.Event()
    status_thread = threading.Thread(
        target=status_loop,
        args=(state, stop_event, args.status_interval),
        daemon=True,
    )
    status_thread.start()

    try:
        job_queue.join()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        stop_event.set()
        status_thread.join(timeout=2)
        sys.exit(130)

    stop_event.set()
    status_thread.join(timeout=2)

    print_status(state)
    snap = state.snapshot()
    print(f"Finished. Outputs done: {snap['done_count']}/{snap['total_count']}")


if __name__ == "__main__":
    main()