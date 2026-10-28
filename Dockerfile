# syntax=docker/dockerfile:1
#
# iris-signal-harvester runtime image.
#
# Layout note (this matters): sink.migrate.default_migrations_dir() resolves
# <package>/../../../migrations, i.e. it assumes a source checkout. Rather than
# patch that, the image keeps a source-checkout shape: the code lives in
# /app/src, is installed *editable* (so the package path really is
# /app/src/iris_signal_harvester/...) and /app/migrations sits where the resolver
# looks for it. The guard in the builder stage fails the build if that stops
# being true.

# ---- builder: resolve and install dependencies into a venv -------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
# README.md is named in pyproject.toml (readme = ...), so the build backend needs it.
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --editable .

# Build-time guard: the migration runner must find the SQL files from inside the image.
RUN /opt/venv/bin/python -c "\
from iris_signal_harvester.sink.migrate import default_migrations_dir as d; \
p = d(); \
assert (p / '001_initial.sql').is_file(), f'migrations not found at {p}'; \
print('migrations resolved at', p)"

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="iris-signal-harvester" \
      org.opencontainers.image.description="Harvests infrastructure-trigger signals from German council information systems into PostGIS."

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

# Unprivileged user. docker-compose overrides the uid/gid with the host user's so
# that files written to the bind-mounted ./reports belong to the person running
# `make`; nothing here assumes uid 10001.
RUN groupadd --system --gid 10001 iris \
    && useradd --system --uid 10001 --gid iris --no-create-home --shell /usr/sbin/nologin iris

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/pyproject.toml /app/README.md /app/
COPY --from=builder /app/src /app/src
COPY --from=builder /app/migrations /app/migrations

# Runtime data the default settings point at, as relative paths from /app:
#   config/                  taxonomy, gazetteer, bodies, thresholds
#   sql/                     validation queries (`iris-harvest validate`)
#   tests/fixtures/cassettes committed replay fixtures (IRIS_SOURCE=fixture, the demo path)
COPY config /app/config
COPY sql /app/sql
COPY tests/fixtures/cassettes /app/tests/fixtures/cassettes

# Code is root-owned and read-only to the app user. Only reports/ is writable.
RUN mkdir -p /app/reports && chown iris:iris /app/reports

WORKDIR /app
USER 10001:10001

ENTRYPOINT ["iris-harvest"]
CMD ["--help"]
