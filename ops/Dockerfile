# transcriber — a container with nothing in it that can rot.
#
# There is no `pip install` anywhere below, and that is the point rather than an oversight:
# every import in src/transcriber is from Python's own standard library, so the image is the
# base Python plus this source tree. Nothing to pin, nothing to audit, nothing that stops
# resolving in three years.
#
# ffmpeg is the one optional extra. Without it the service still works — audio.py falls back
# to a pure-Python container walk and splitting falls back to cutting on frame edges — but
# with it, silence detection puts the cuts between sentences instead of through them. It is
# a couple of hundred megabytes; if the image size matters more than that, drop the apt-get
# block and the service will say in each transcript's metadata how it cut.
#
#   docker build -f ops/Dockerfile -t transcriber .
#   docker run --rm --env-file .env -v transcriber-state:/var/lib/transcriber transcriber
#
# The ledger MUST be on a volume. It is the only proof a recording ever existed, and a
# container that loses it re-processes everything and can no longer tell you what happened.

FROM python:3.11-slim-bookworm

# Fail fast and log immediately: an unflushed buffer in a service you are watching through
# `docker logs` is indistinguishable from a service that has hung.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/transcriber/src

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Not root. This process downloads files from the internet and runs ffmpeg over them; there
# is no reason for it to be able to write anything outside its own two directories.
RUN set -eux; \
    groupadd --system --gid 10001 transcriber; \
    useradd --system --uid 10001 --gid transcriber --home-dir /var/lib/transcriber \
            --shell /usr/sbin/nologin transcriber; \
    mkdir -p /var/lib/transcriber /var/cache/transcriber; \
    chown -R transcriber:transcriber /var/lib/transcriber /var/cache/transcriber; \
    chmod 700 /var/lib/transcriber /var/cache/transcriber

WORKDIR /opt/transcriber
COPY --chown=root:root src/ ./src/
COPY --chown=root:root ARCHITECTURE.md README.md pyproject.toml ./

# The source is read-only to the process that runs it.
RUN chmod -R a-w /opt/transcriber/src

USER transcriber

# The ledger and the scratch space are the only writable paths, and both are volumes.
# WORK_DIR holds the raw audio of confidential conversations while a recording is in flight;
# it is created 0700 by the service and belongs on a real disk, never in a shared /tmp.
ENV LEDGER_PATH=/var/lib/transcriber/ledger.sqlite \
    WORK_DIR=/var/cache/transcriber
VOLUME ["/var/lib/transcriber", "/var/cache/transcriber"]

# Proves parsing, the state machine, quote verification and the markdown contract offline,
# with no credential and no network. It runs at build time so a broken image never ships,
# and it is the same command the health check uses at run time.
RUN python3 -m transcriber selftest

# A container that is "up" but not processing is exactly the failure this service exists to
# remove, so the health check asks the ledger a real question rather than checking a port.
HEALTHCHECK --interval=5m --timeout=30s --start-period=1m --retries=3 \
    CMD python3 -m transcriber status >/dev/null || exit 1

STOPSIGNAL SIGTERM

ENTRYPOINT ["python3", "-m", "transcriber"]
CMD ["run"]
