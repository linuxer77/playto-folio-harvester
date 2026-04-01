package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"playto-folio-harvester/internal/models"
	"playto-folio-harvester/internal/repository"
	"playto-folio-harvester/internal/worker"

	"github.com/go-chi/chi/v5"
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
	var req createJobRequest
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()

	if err := decoder.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("invalid JSON payload: %v", err))
		return
	}

	if err := validateCreateJobRequest(req); err != nil {
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
		writeError(w, http.StatusInternalServerError, "failed to create job")
		return
	}

	h.processor.Start(job)
	writeJSON(w, http.StatusCreated, createJobResponse{ID: job.ID.String()})
}

func (h *JobHandler) listJobs(w http.ResponseWriter, r *http.Request) {
	jobs, err := h.jobs.ListJobs(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to fetch jobs")
		return
	}

	writeJSON(w, http.StatusOK, jobs)
}

func (h *JobHandler) getJobByID(w http.ResponseWriter, r *http.Request) {
	idParam := chi.URLParam(r, "id")
	id, err := uuid.Parse(idParam)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid job id")
		return
	}

	job, err := h.jobs.GetJobByID(r.Context(), id)
	if err != nil {
		if errors.Is(err, repository.ErrJobNotFound) {
			writeError(w, http.StatusNotFound, "job not found")
			return
		}

		writeError(w, http.StatusInternalServerError, "failed to fetch job")
		return
	}

	writeJSON(w, http.StatusOK, job)
}

func validateCreateJobRequest(req createJobRequest) error {
	if strings.TrimSpace(req.ClientName) == "" {
		return fmt.Errorf("client_name is required")
	}
	if strings.TrimSpace(req.JobTitle) == "" {
		return fmt.Errorf("job_title is required")
	}
	if strings.TrimSpace(req.CandidateName) == "" {
		return fmt.Errorf("candidate_name is required")
	}
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
