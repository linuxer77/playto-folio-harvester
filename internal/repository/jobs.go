package repository

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"playto-folio-harvester/internal/models"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var ErrJobNotFound = errors.New("job not found")

type JobRepository interface {
	CreateJob(ctx context.Context, input models.CreateJobInput) (models.Job, error)
	ListJobs(ctx context.Context) ([]models.Job, error)
	GetJobByID(ctx context.Context, id uuid.UUID) (models.Job, error)
	UpdateJobStatus(
		ctx context.Context,
		id uuid.UUID,
		status models.JobStatus,
		driveLink *string,
		errorLogs *string,
	) error
}

type PostgresJobRepository struct {
	pool *pgxpool.Pool
}

func NewPostgresJobRepository(pool *pgxpool.Pool) *PostgresJobRepository {
	return &PostgresJobRepository{pool: pool}
}

const optionalFieldDefaultValue = "N/A"

func normalizeOptionalField(value string) string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return optionalFieldDefaultValue
	}

	return trimmed
}

func (r *PostgresJobRepository) CreateJob(ctx context.Context, input models.CreateJobInput) (models.Job, error) {
	clientName := normalizeOptionalField(input.ClientName)
	jobTitle := normalizeOptionalField(input.JobTitle)
	candidateName := normalizeOptionalField(input.CandidateName)
	portfolioURL := strings.TrimSpace(input.PortfolioURL)

	query := `
		INSERT INTO jobs (client_name, job_title, candidate_name, portfolio_url, status)
		VALUES ($1, $2, $3, $4, $5)
		RETURNING id, client_name, job_title, candidate_name, portfolio_url, status, drive_link, error_logs, created_at
	`

	row := r.pool.QueryRow(
		ctx,
		query,
		clientName,
		jobTitle,
		candidateName,
		portfolioURL,
		models.JobStatusQueued,
	)

	job, err := scanJob(row)
	if err != nil {
		return models.Job{}, fmt.Errorf("create job: %w", err)
	}

	return job, nil
}

func (r *PostgresJobRepository) ListJobs(ctx context.Context) ([]models.Job, error) {
	query := `
		SELECT id, client_name, job_title, candidate_name, portfolio_url, status, drive_link, error_logs, created_at
		FROM jobs
		ORDER BY created_at DESC
	`

	rows, err := r.pool.Query(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("list jobs: %w", err)
	}
	defer rows.Close()

	jobs := make([]models.Job, 0)
	for rows.Next() {
		var job models.Job
		if err := rows.Scan(
			&job.ID,
			&job.ClientName,
			&job.JobTitle,
			&job.CandidateName,
			&job.PortfolioURL,
			&job.Status,
			&job.DriveLink,
			&job.ErrorLogs,
			&job.CreatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan job row: %w", err)
		}
		jobs = append(jobs, job)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate jobs rows: %w", err)
	}

	return jobs, nil
}

func (r *PostgresJobRepository) GetJobByID(ctx context.Context, id uuid.UUID) (models.Job, error) {
	query := `
		SELECT id, client_name, job_title, candidate_name, portfolio_url, status, drive_link, error_logs, created_at
		FROM jobs
		WHERE id = $1
	`

	row := r.pool.QueryRow(ctx, query, id)
	job, err := scanJob(row)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return models.Job{}, ErrJobNotFound
		}
		return models.Job{}, fmt.Errorf("get job: %w", err)
	}

	return job, nil
}

func (r *PostgresJobRepository) UpdateJobStatus(
	ctx context.Context,
	id uuid.UUID,
	status models.JobStatus,
	driveLink *string,
	errorLogs *string,
) error {
	query := `
		UPDATE jobs
		SET status = $2,
			drive_link = $3,
			error_logs = $4
		WHERE id = $1
	`

	result, err := r.pool.Exec(ctx, query, id, status, driveLink, errorLogs)
	if err != nil {
		return fmt.Errorf("update job status: %w", err)
	}

	if result.RowsAffected() == 0 {
		return ErrJobNotFound
	}

	return nil
}

func scanJob(row pgx.Row) (models.Job, error) {
	var job models.Job
	if err := row.Scan(
		&job.ID,
		&job.ClientName,
		&job.JobTitle,
		&job.CandidateName,
		&job.PortfolioURL,
		&job.Status,
		&job.DriveLink,
		&job.ErrorLogs,
		&job.CreatedAt,
	); err != nil {
		return models.Job{}, err
	}

	return job, nil
}
