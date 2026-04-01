# Playto Folio Harvester - Architecture and Progress (Phases 1-2)

## Scope and objective
- The system ingests candidate portfolio URLs, harvests media assets, sanitizes outputs, and prepares files for client-safe delivery.
- Current completed scope is Python harvesting plus Go API orchestration with PostgreSQL-backed job tracking.

## Whole code architecture (current)

### Components
- Frontend UI (planned for integration): Next.js admin panel to create and monitor jobs.
- API backend (implemented): Go HTTP service.
- Worker (implemented): Python media harvester.
- Database (implemented): PostgreSQL jobs table for state and error tracking.
- Local storage (implemented): temp_harvest/<job_id> for harvested output.
- Drive integration (next phase): service account credentials prepared in env, upload wiring pending.

### Backend folder architecture
- cmd/server: application bootstrap, env loading, server startup, graceful shutdown.
- internal/api: HTTP handlers and route registration.
- internal/db: PostgreSQL connection setup.
- internal/repository: data access methods for jobs.
- internal/worker: asynchronous execution of the Python harvester.
- internal/models: shared job model and status types.
- worker/harvester.py: Python harvesting engine.

## Python harvester status (Phase 1 deliverables)

### Implemented capabilities
- Browser-driven extraction using Playwright for dynamic sites.
- Asset discovery and download for images, PDFs, and video candidates.
- Video fallback behavior with screenshot + README when video download fails.
- Image sanitization and generic output renaming.
- Structured worker logs for each major stage.

### Diagnostics improvements completed
- Logs parsed args, runtime context, browser lifecycle events, and harvest stage outputs.
- Logs exception type/message in more places.
- Logs traceback on top-level and timeout failure paths.

## Go API and DB status (Phase 2 deliverables)

### Database
- schema.sql created with jobs table and created_at index.
- Status lifecycle stored in DB: queued, in_progress, completed, failed.
- error_logs and drive_link fields are persisted for job history.

### Endpoints implemented
- POST /api/jobs: create a job and return id.
- GET /api/jobs: list jobs ordered by created_at DESC.
- GET /api/jobs/{id}: fetch job details and current status.

### Async processing implemented
- POST /api/jobs starts a goroutine for worker processing.
- Go updates status to in_progress, runs Python worker, then updates to completed or failed.
- On failure, captured stderr/stdout details are stored in error_logs.

### Logging improvements completed
- Request-level logs with request_id for each endpoint.
- Startup logs include masked DB URL and runtime config.
- DB connection start/success logs.
- Worker lifecycle logs include command start, exit code, duration, and status updates.

## Is harvester.py connected to the Go server?

Yes. It is connected and currently used in production flow.

- Integration mode: direct process invocation from Go using os/exec.
- Trigger point: POST /api/jobs.
- Command pattern used by Go:

    PYTHON_BIN worker/harvester.py --url <portfolio_url> --output ./temp_harvest/<job_id>

- Current architecture does not use a separate queue broker yet; execution is async via goroutine inside the Go process.

## Environment configuration in use

Required variables currently used:
- PORT
- DATABASE_URL
- PYTHON_BIN
- WORKER_SCRIPT_PATH
- HARVEST_OUTPUT_BASE
- GOOGLE_APPLICATION_CREDENTIALS
- DRIVE_TARGET_FOLDER_ID

Note:
- Go now auto-loads .env at startup when run from project root.

## How to test the system

### 1) Prerequisites
1. Ensure PostgreSQL is running.
2. Ensure playto_harvester database exists.
3. Apply schema:

    psql "postgres://postgres@localhost:5432/playto_harvester?sslmode=disable" -f schema.sql

### 2) Start API server

Run from project root:

    go run ./cmd/server/main.go

### 3) Test Go endpoints

List jobs:

    curl -i http://localhost:8080/api/jobs

Create job:

    curl -i -X POST http://localhost:8080/api/jobs \
      -H "Content-Type: application/json" \
      -d '{
        "client_name":"Playto QA",
        "job_title":"API Smoke",
        "candidate_name":"Candidate Test",
        "portfolio_url":"https://example.com"
      }'

Get by id:

    curl -i http://localhost:8080/api/jobs/<JOB_ID>

Poll status:

    while true; do
      curl -s http://localhost:8080/api/jobs/<JOB_ID>
      echo
      sleep 2
    done

Expected transition:
- queued -> in_progress -> completed
or
- queued -> in_progress -> failed

### 4) Test Python harvester directly (standalone)

    ./.venv/bin/python worker/harvester.py \
      --url "https://example.com" \
      --output "./temp_harvest/manual_test"

### 5) Error-path tests

Invalid id format:

    curl -i http://localhost:8080/api/jobs/not-a-uuid

Expected: HTTP 400.

Missing id (valid UUID format, not present):

    curl -i http://localhost:8080/api/jobs/11111111-1111-1111-1111-111111111111

Expected: HTTP 404.

## Current limitations / next phase
- Google Drive upload link persistence is not fully wired yet.
- Frontend UI integration is pending.
- Queue-based worker orchestration is not implemented yet (currently direct async exec from API process).
