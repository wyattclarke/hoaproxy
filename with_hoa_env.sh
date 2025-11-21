#!/usr/bin/env bash
set -a
source "$(dirname "$0")/settings.env"
set +a

# Forward all arguments to hoaware CLI
python -m hoaware.cli "$@"
