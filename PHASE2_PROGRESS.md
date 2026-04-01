# Playto Folio Harvester - Architecture and Progress (Current Snapshot)

## Scope and objective
- The system ingests candidate portfolio URLs, harvests media assets, sanitizes outputs, and prepares files for client-safe delivery.
- Current implemented scope includes Python harvesting, Go API orchestration, PostgreSQL job tracking, and a working Next.js operator dashboard.

## Whole code architecture (current)

### Components
- Frontend UI (implemented): Next.js App Router dashboard in frontend/.
- API backend (implemented): Go HTTP service with Chi.
- Worker (implemented): Python media harvester.
- Database (implemented): PostgreSQL jobs table for state and error tracking.
- Local storage (implemented): temp_harvest/<job_id> for harvested output.
- Drive integration (pending): service account credentials are in place, but API does not yet persist completed drive_link values from an upload step.

### Repository architecture
- cmd/server: app bootstrap, env loading, startup, graceful shutdown.
- internal/api: route registration and handlers.
- internal/db: PostgreSQL connection setup.
- internal/repository: jobs data access and persistence normalization.
- internal/worker: async execution of the Python harvester from Go.
- internal/models: shared job model and status types.
- worker/harvester.py: Python harvesting engine.
- frontend/: Next.js admin UI (form submission, polling table, status/result display).

## Frontend status (implemented)

### Dashboard capabilities
- Header and dashboard shell implemented.
- Job submission form implemented with shadcn/ui components.
- Only portfolio_url is required in the form.
- client_name, job_title, and candidate_name inputs remain visible but are optional.
- Toast notifications for create-job success/failure.
- Jobs table with columns for client/job, candidate, URL, status, and result.
- Status badge color mapping:
  - queued: yellow
  - in_progress: blue
  - completed: green
  - failed: red
- Failed jobs expose error_logs via a View Error toggle.
- Job list polling every 3 seconds for near real-time status updates.

### Frontend/API integration
- Next.js rewrite proxy is configured:

    /api/:path* -> http://localhost:8080/api/:path*

- This allows frontend requests to use relative /api/* paths without browser CORS issues.

### Frontend runtime notes
- Root layout includes suppressHydrationWarning on html to avoid dev-time hydration warnings.
- Toaster uses a fixed light theme to avoid server/client attribute drift.

## Python harvester status (Phase 1 deliverables)

### Implemented capabilities
- Browser-driven extraction using Playwright for JS-heavy sites.
- Asset discovery and download for images, PDFs, and video candidates.
- Video fallback behavior with screenshot + video_fallback_readme.txt when yt-dlp fails.
- Image sanitization pass and generic output renaming.
- Generic renaming for docs and videos.
- Structured worker logs for each major stage.

### Diagnostics improvements completed
- Logs parsed args, runtime context, browser lifecycle events, and stage summaries.
- Logs exception type/message in failure paths.
- Logs tracebacks in top-level failures and timeout cases.

## Go API and DB status (Phase 2 deliverables)

### Database
- schema.sql defines jobs table and created_at index.
- Status lifecycle persisted in DB: queued, in_progress, completed, failed.
- error_logs and drive_link columns are persisted for job history.
- Schema keeps NOT NULL on client_name, job_title, candidate_name.

### Endpoints implemented
- POST /api/jobs: create job and return id.
- GET /api/jobs: list jobs ordered by created_at DESC.
- GET /api/jobs/{id}: fetch job details and current status.

### Request validation and optional fields
- Backend now requires only portfolio_url.
- client_name, job_title, and candidate_name may be omitted or empty.
- Repository normalizes empty optional values to "N/A" before insert to satisfy current NOT NULL schema.

### Async processing implemented
- POST /api/jobs starts a goroutine for worker processing.
- Go updates status to in_progress, runs Python worker, then updates to completed or failed.
- On worker failure, captured stderr/stdout details are stored in error_logs.

### Logging improvements completed
- Request-level logs with request_id on API endpoints.
- Startup logs include masked DB URL and runtime config.
- DB connection start/success logs.
- Worker lifecycle logs include command start, exit code, duration, and status changes.

## Is harvester.py connected to the Go server?

Yes. It is fully connected and active in the current API flow.

- Integration mode: direct process invocation from Go using os/exec.
- Trigger point: POST /api/jobs.
- Command pattern:

    PYTHON_BIN worker/harvester.py --url <portfolio_url> --output ./temp_harvest/<job_id>

- Current architecture does not use an external queue broker yet; execution is async inside the API process.

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
- Go auto-loads .env at startup when run from project root.

## How to run and test the current system

### 1) Prerequisites
1. Ensure PostgreSQL is running.
2. Ensure playto_harvester database exists.
3. Apply schema:

    psql "postgres://postgres@localhost:5432/playto_harvester?sslmode=disable" -f schema.sql

### 2) Start API server

Run from project root:

    go run ./cmd/server/main.go

### 3) Start frontend dashboard

Run from project root:

    cd frontend
    npm install
    npm run dev

Open:

    http://localhost:3000

### 4) API endpoint smoke tests

List jobs:

    curl -i http://localhost:8080/api/jobs

Create job (minimal payload, now supported):

    curl -i -X POST http://localhost:8080/api/jobs \
      -H "Content-Type: application/json" \
      -d '{
        "portfolio_url":"https://example.com"
      }'

Create job (full optional metadata):

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

### 5) Python harvester standalone test

    ./.venv/bin/python worker/harvester.py \
      --url "https://example.com" \
      --output "./temp_harvest/manual_test"

### 6) Error-path tests

Invalid id format:

    curl -i http://localhost:8080/api/jobs/not-a-uuid

Expected: HTTP 400.

Missing id (valid UUID format, not present):

    curl -i http://localhost:8080/api/jobs/11111111-1111-1111-1111-111111111111

Expected: HTTP 404.

## Current limitations / next phase
- Google Drive upload and completed drive_link persistence are not fully wired yet.
- Queue-based worker orchestration is not implemented yet (current model is in-process async execution).
- PDF redaction/flattening pipeline is not yet implemented in the Python worker; current flow downloads and renames PDFs.
