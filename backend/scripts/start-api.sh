#!/bin/sh
# API entrypoint: migrate, then serve.
#
# This exists because a platform start command is not a shell command. Render (and
# several others) hand `dockerCommand` to the container without a shell to parse it, so
# quotes, `&&` and `$PORT` arrive as literal characters in a command *name* and the
# container dies with `not found`. Keeping the platform-side command to plain
# whitespace-separated tokens -- `sh ./scripts/start-api.sh` -- makes it immune to
# however any given platform chooses to split it, and puts the actual logic somewhere
# it can be read, reviewed and run locally.
set -e

# Migrations run here rather than as a pre-deploy hook because Render's preDeployCommand
# requires a paid instance type. This is safe at ONE instance and only one: Alembic takes
# no lock that would make N replicas racing `upgrade head` on boot safe. The free plan
# gives exactly one, so this holds for as long as the plan does. Move to preDeployCommand
# before scaling past a single instance.
echo "running migrations..."
alembic upgrade head

# --workers 1, deliberately, and not WEB_CONCURRENCY.
#
# LEDGERLOOP_EMBED_WORKER hosts the matcher, relay and sweeper on this process's event
# loop. With N uvicorn workers each one would start its own copy of all three. Nothing
# breaks -- the consumer group distributes stream entries and the partial unique indexes
# reject a duplicate result -- but it multiplies the polling and the connection count for
# no gain. Render defaults WEB_CONCURRENCY to 1; this ignores it rather than trusting it,
# because the coupling is a correctness-adjacent decision, not a performance knob.
#
# exec so uvicorn replaces this shell as PID 1 and receives SIGTERM directly. Without it
# the shell holds PID 1, swallows the signal, and the embedded matcher never gets the
# clean drain that lets it finish and ack the message in hand.
echo "starting api on port ${PORT:-8000}"
exec uvicorn ledgerloop.api.app:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1
