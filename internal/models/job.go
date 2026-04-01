package models

import (
	"time"

	"github.com/google/uuid"
)

type JobStatus string

const (
	JobStatusQueued     JobStatus = "queued"
	JobStatusInProgress JobStatus = "in_progress"
	JobStatusCompleted  JobStatus = "completed"
	JobStatusFailed     JobStatus = "failed"
)

type Job struct {
	ID            uuid.UUID `json:"id"`
	ClientName    string    `json:"client_name"`
	JobTitle      string    `json:"job_title"`
	CandidateName string    `json:"candidate_name"`
	PortfolioURL  string    `json:"portfolio_url"`
	Status        JobStatus `json:"status"`
	DriveLink     *string   `json:"drive_link"`
	ErrorLogs     *string   `json:"error_logs"`
	CreatedAt     time.Time `json:"created_at"`
}

type CreateJobInput struct {
	ClientName    string
	JobTitle      string
	CandidateName string
	PortfolioURL  string
}
