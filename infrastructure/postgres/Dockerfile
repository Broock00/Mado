# Mado needs PostGIS (venue geography, radius search) and pgvector (embeddings,
# AI memory) in the same database - spec 54.09 puts both in the primary store.
# No official image ships both, so PostGIS is the base and pgvector is added from
# the PGDG apt repository that image already configures.
FROM postgis/postgis:16-3.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
