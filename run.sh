#!/usr/bin/env bash
# venv の Python で dashboard.py を起動するラッパー
cd "$(dirname "$0")"
export LANG="${LANG:-C.UTF-8}"
exec ./.venv/bin/python dashboard.py "$@"
