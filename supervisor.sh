#!/bin/bash
cd "$(dirname "$0")"
PY=./.venv/bin/python3
[ -x "$PY" ] || PY=/usr/bin/env\ python3
nice -n 10 $PY ./supervisor.py "$@"
sleep 10
clear
reset
clear
