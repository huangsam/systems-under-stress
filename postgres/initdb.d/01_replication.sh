#!/bin/sh
set -e

# Create replication user if not exists
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'replicator') THEN
            CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator_pw';
        END IF;
    END
    \$\$;
EOSQL

# Enable replication connections in pg_hba.conf for container network
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
