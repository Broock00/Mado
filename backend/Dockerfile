FROM python:3.12-slim

# System deps: libgdal for GeoAlchemy2 spatial types, gcc for native extension wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgdal-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the full backend source.
COPY . .

# Install the package and all runtime dependencies declared in pyproject.toml.
# --no-cache-dir keeps the image lean; editable install is not needed in production.
RUN pip install --no-cache-dir .

# On Render and Cloud Run, PORT is injected at runtime; default 8000 for local use.
ENV PORT=8000

# Linux uses SelectorEventLoop by default so psycopg works without the Windows shim.
# run.py is the Windows dev entry point; production targets app.main:app directly.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
