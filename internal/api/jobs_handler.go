package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"strings"

	"playto-folio-harvester/internal/models"
	"playto-folio-harvester/internal/repository"
	"playto-folio-harvester/internal/worker"

	"github.com/go-chi/chi/v5"
	chiMiddleware "github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"
)

type JobHandler struct {
	jobs      repository.JobRepository
	processor *worker.Processor
}

func NewJobHandler(jobs repository.JobRepository, processor *worker.Processor) *JobHandler {
	return &JobHandler{jobs: jobs, processor: processor}
}

func (h *JobHandler) RegisterRoutes(router chi.Router) {
	router.Route("/api", func(r chi.Router) {
		r.Post("/jobs", h.createJob)
		r.Get("/jobs", h.listJobs)
		r.Get("/jobs/{id}", h.getJobByID)
	})
}

type createJobRequest struct {
	ClientName    string `json:"client_name"`
	JobTitle      string `json:"job_title"`
	CandidateName string `json:"candidate_name"`
	PortfolioURL  string `json:"portfolio_url"`
}

type createJobResponse struct {
	ID string `json:"id"`
}

func (h *JobHandler) createJob(w http.ResponseWriter, r *http.Request) {
	requestID := chiMiddleware.GetReqID(r.Context())
	log.Printf("request_id=%s endpoint=POST /api/jobs event=request_received", requestID)

	var req createJobRequest
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()

	if err := decoder.Decode(&req); err != nil {
		log.Printf("request_id=%s endpoint=POST /api/jobs event=decode_error error=%q", requestID, err)
		writeError(w, http.StatusBadRequest, fmt.Sprintf("invalid JSON payload: %v", err))
		return
	}

	if err := validateCreateJobRequest(req); err != nil {
		log.Printf("request_id=%s endpoint=POST /api/jobs event=validation_error error=%q", requestID, err)
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	job, err := h.jobs.CreateJob(r.Context(), models.CreateJobInput{
		ClientName:    strings.TrimSpace(req.ClientName),
		JobTitle:      strings.TrimSpace(req.JobTitle),
		CandidateName: strings.TrimSpace(req.CandidateName),
		PortfolioURL:  strings.TrimSpace(req.PortfolioURL),
	})
	if err != nil {
		log.Printf("request_id=%s endpoint=POST /api/jobs event=create_job_failed error=%q", requestID, err)
		writeError(w, http.StatusInternalServerError, "failed to create job")
		return
	}

	h.processor.Start(job)
	log.Printf("request_id=%s endpoint=POST /api/jobs event=job_created job_id=%s status=%s", requestID, job.ID, job.Status)
	writeJSON(w, http.StatusCreated, createJobResponse{ID: job.ID.String()})
}

func (h *JobHandler) listJobs(w http.ResponseWriter, r *http.Request) {
	requestID := chiMiddleware.GetReqID(r.Context())
	log.Printf("request_id=%s endpoint=GET /api/jobs event=request_received", requestID)

	jobs, err := h.jobs.ListJobs(r.Context())
	if err != nil {
		log.Printf("request_id=%s endpoint=GET /api/jobs event=list_jobs_failed error=%q", requestID, err)
		writeError(w, http.StatusInternalServerError, "failed to fetch jobs")
		return
	}

	log.Printf("request_id=%s endpoint=GET /api/jobs event=request_succeeded total_jobs=%d", requestID, len(jobs))
	writeJSON(w, http.StatusOK, jobs)
}

func (h *JobHandler) getJobByID(w http.ResponseWriter, r *http.Request) {
	requestID := chiMiddleware.GetReqID(r.Context())
	idParam := chi.URLParam(r, "id")
	log.Printf("request_id=%s endpoint=GET /api/jobs/{id} event=request_received id_param=%q", requestID, idParam)

	id, err := uuid.Parse(idParam)
	if err != nil {
		log.Printf("request_id=%s endpoint=GET /api/jobs/{id} event=id_parse_failed id_param=%q error=%q", requestID, idParam, err)
		writeError(w, http.StatusBadRequest, "invalid job id")
		return
	}

	job, err := h.jobs.GetJobByID(r.Context(), id)
	if err != nil {
		if errors.Is(err, repository.ErrJobNotFound) {
			log.Printf("request_id=%s endpoint=GET /api/jobs/{id} event=job_not_found job_id=%s", requestID, id)
			writeError(w, http.StatusNotFound, "job not found")
			return
		}

		log.Printf("request_id=%s endpoint=GET /api/jobs/{id} event=get_job_failed job_id=%s error=%q", requestID, id, err)
		writeError(w, http.StatusInternalServerError, "failed to fetch job")
		return
	}

	log.Printf("request_id=%s endpoint=GET /api/jobs/{id} event=request_succeeded job_id=%s status=%s", requestID, id, job.Status)
	writeJSON(w, http.StatusOK, job)
}

func validateCreateJobRequest(req createJobRequest) error {
	if strings.TrimSpace(req.PortfolioURL) == "" {
		return fmt.Errorf("portfolio_url is required")
	}

	parsed, err := url.ParseRequestURI(strings.TrimSpace(req.PortfolioURL))
	if err != nil {
		return fmt.Errorf("portfolio_url must be a valid URL")
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return fmt.Errorf("portfolio_url must use http or https")
	}

	return nil
}

func writeJSON(w http.ResponseWriter, statusCode int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		http.Error(w, "failed to encode response", http.StatusInternalServerError)
	}
}

func writeError(w http.ResponseWriter, statusCode int, message string) {
	writeJSON(w, statusCode, map[string]string{"error": message})
}
