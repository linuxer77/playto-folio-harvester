# Running Playto Folio Harvester (Linux)

This guide explains how to set up and run the project locally, including Go installation and starting both backend and frontend.

## 1. Prerequisites

Install the following:

- Go 1.26 or newer (go.mod currently targets 1.26.1)
- Python 3.11+
- Node.js 20+
- npm 10+
- PostgreSQL 14+
- ffmpeg (recommended for robust video handling with yt-dlp)

## 2. Install Go on Linux

Choose one method.

### Method A: Official Go tarball (recommended for exact version control)

```bash
# Example for amd64 Linux. Update version if needed.
cd /tmp
wget https://go.dev/dl/go1.26.1.linux-amd64.tar.gz
sudo rm -rf /usr/local/go
sudo tar -C /usr/local -xzf go1.26.1.linux-amd64.tar.gz

# Add Go to your shell profile
echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.zshrc
source ~/.zshrc

go version
```

### Method B: Package manager (faster, but version may be older)

```bash
sudo apt update
sudo apt install -y golang-go
go version
```

## 3. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs npm postgresql postgresql-contrib ffmpeg
```

Verify:

```bash
python3 --version
node --version
npm --version
psql --version
```

## 4. Prepare backend dependencies

Run from repository root:

```bash
# Go modules
go mod download

# Python virtual environment for worker
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Playwright browser required by worker
python -m playwright install chromium
```

If Chromium Linux dependencies are missing, run:

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

## 5. Prepare database

Create database and apply schema:

```bash
sudo -u postgres psql -c "CREATE DATABASE playto_harvester;"
psql "postgres://postgres@localhost:5432/playto_harvester?sslmode=disable" -f schema.sql
```

If the database already exists, only run the schema command.

## 6. Configure environment file

Create or update a root .env file with values like below:

```dotenv
PORT=8080
DATABASE_URL=postgres://postgres@localhost:5432/playto_harvester?sslmode=disable

PYTHON_BIN=./.venv/bin/python
WORKER_SCRIPT_PATH=worker/harvester.py
HARVEST_OUTPUT_BASE=./temp_harvest

GOOGLE_OAUTH_CLIENT_SECRET=./oauth-secret.json
GOOGLE_OAUTH_TOKEN_PATH=./token.json
DRIVE_TARGET_FOLDER_ID=<YOUR_DRIVE_FOLDER_ID>
```

Notes:

- The backend reads .env automatically at startup.
- Frontend rewrite expects backend at http://localhost:8080.
- Keep credential JSON files and .env out of version control.

## 7. Run backend and frontend

Use two terminals.

### Terminal 1: Backend API (Go)

```bash
source .venv/bin/activate
go run ./cmd/server/main.go
```

Expected log includes API listening on port 8080.

### Terminal 2: Frontend (Next.js)

```bash
cd /frontend
npm install
npm run dev
```

Frontend should be available at:

- http://localhost:3000

## 8. Quick verification

Check backend health by listing jobs:

```bash
curl -i http://localhost:8080/api/jobs
```

Create a test job:

```bash
curl -i -X POST http://localhost:8080/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"portfolio_url":"https://example.com"}'
```

Then open frontend and confirm status transitions in the dashboard:

- queued -> in_progress -> completed or failed

## 9. Common startup issues

- Error: DATABASE_URL is required
  - Set DATABASE_URL in .env and restart backend.

- POST/GET /api/jobs returns 500
  - Apply schema.sql to the playto_harvester database.

- Python worker fails with missing packages
  - Ensure PYTHON_BIN points to ./.venv/bin/python and requirements are installed.

- Browser/OAuth prompt appears on server
  - Pre-provision a valid token.json for non-interactive environments.
