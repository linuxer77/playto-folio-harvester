# Project Context: Playto Folio Harvester

## 1. Project Mission
We are building an internal admin tool for an agency named Playto. The tool extracts media (images, videos, PDFs) from candidate portfolio websites and uploads them to a secure Google Drive folder, strictly ensuring ZERO candidate contact information (PII, names, metadata) is exposed to the client.

We are strictly implementing "Approach B: Media Harvesting". We are NOT cloning HTML or deploying static websites.

## 2. Architecture & Tech Stack
This is a decoupled system optimized for background processing.
* **Frontend (The UI):** Next.js (App Router), Tailwind CSS, shadcn/ui.
* **Backend (The Brain):** Go (using Gin or Chi) + PostgreSQL.
* **Worker (The Muscle):** Python.

## 3. The Core Data Flow
1.  **Job Creation:** Operator uses the Next.js UI to create a job (Client Name, Job Title) and add Candidate URLs.
2.  **State Management:** The Go API saves this job to PostgreSQL with a status of `queued`.
3.  **Processing:** The Go API triggers the Python worker (via CLI execution or a simple message broker/queue).
4.  **Media Extraction (Python):** * Navigates the URL using `Playwright` (to handle JS-heavy React/Vue sites).
    * Downloads images and linked PDFs.
    * Uses `yt-dlp` to download high-res videos (Vimeo/YouTube embeds).
5.  **Sanitization (Python - CRITICAL):**
    * Strips EXIF data from all images.
    * Strips metadata from PDFs.
    * Visually redacts PII inside PDFs using `PyMuPDF`.
    * Renames all files to generic names (e.g., `image_01.jpg`, `video_01.mp4`).
6.  **Export (Python):** Uploads the clean files to Google Drive using a Service Account, creates a shareable link, and updates the PostgreSQL job status to `completed` with the link.
7.  **Delivery:** The Next.js UI polls the Go API and displays the final Drive link to the operator.

## 4. Strict Engineering Constraints (DO NOT HALLUCINATE OR BYPASS)
* **Google Drive Auth:** You MUST use the Google Drive API v3 with a Service Account JSON key. This runs entirely server-side. Do NOT implement OAuth flows that require human consent screens.
* **PDF Redaction Rule:** Do NOT just extract and delete text from PDFs. You MUST use `PyMuPDF` (fitz) to find regex matches for emails/phone numbers, draw an opaque white rectangle over the coordinates, and FLATTEN the document.
* **File Naming:** Never preserve original filenames. They often contain candidate names.
* **Graceful Video Degradation:** If `yt-dlp` fails to download a video due to DRM, catch the error, take a screenshot of the video player, and write the URL to a `README.txt` file in the candidate's folder. Do not fail the entire job.
* **Out of Scope:** We are NOT building Notion support for V1. Do not write Notion-specific scraping logic.

## 5. PostgreSQL Schema Guidelines
At a minimum, the database needs a `jobs` table to track state:
* `id` (UUID)
* `client_name` (String)
* `job_title` (String)
* `candidate_name` (String - for internal UI only, never exposed)
* `portfolio_url` (String)
* `status` (Enum: queued, in_progress, completed, failed)
* `drive_link` (String - populated when complete)
* `error_logs` (Text - populated if failed)