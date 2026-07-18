FROM postgres:17.5-alpine3.22@sha256:6567bca8d7bc8c82c5922425a0baee57be8402df92bae5eacad5f01ae9544daa

COPY src/careerops/infrastructure/database/bootstrap_roles.sql \
    /docker-entrypoint-initdb.d/10-capability-roles.sql
COPY deploy/postgres/init/20-create-runtime-and-temporal.sh \
    /docker-entrypoint-initdb.d/20-create-runtime-and-temporal.sh

RUN chmod 0444 /docker-entrypoint-initdb.d/10-capability-roles.sql \
    && chmod 0555 /docker-entrypoint-initdb.d/20-create-runtime-and-temporal.sh
