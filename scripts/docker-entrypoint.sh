#!/bin/sh
set -eu

case "${1:-}" in
  plan)
    shift
    exec python3 /app/scripts/plan_transcodes.py "$@"
    ;;
  run)
    shift
    exec python3 /app/scripts/run_transcodes.py "$@"
    ;;
  ""|-h|--help|help)
    cat <<'EOF'
Usage:
  plan [args...]   Run the planner
  run  [args...]   Run the executor

Examples:
  plan /media
  plan --help
  run --help
  run --concurrency 2

Anything after 'plan' or 'run' is passed through to the underlying Python script.
EOF
    ;;
  *)
    echo "ERROR: unknown command: $1" >&2
    echo "Use 'plan' or 'run'." >&2
    exit 2
    ;;
esac