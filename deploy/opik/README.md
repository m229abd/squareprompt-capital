# Opik trace infrastructure

Adapted from the official [Opik 1.11.14 Docker Compose deployment](https://github.com/comet-ml/opik/tree/1.11.14/deployment/docker-compose) and its nginx and ClickHouse configuration.

This subset runs trace storage and the web UI under the `opik` profile. It removes the optional privileged Python evaluator, generated demo data, and OpenTelemetry collectors. Service names/volumes are prefixed, the UI binds to loopback port 5174, and images are pinned. It shares the main Compose project's default network so the research worker can address `opik-backend:8080`.

The durable REST trace exporter uses `OPIK_URL_OVERRIDE=http://opik-backend:8080` and workspace `default`. This is a local development deployment. Its local infrastructure passwords are not suitable for publishing database services to the internet.
