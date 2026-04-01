package worker

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"playto-folio-harvester/internal/models"
	"playto-folio-harvester/internal/repository"
)

type Processor struct {
	jobs             repository.JobRepository
	pythonExecutable string
	workerScriptPath string
	outputBaseDir    string
}

func NewProcessor(
	jobs repository.JobRepository,
	pythonExecutable string,
	workerScriptPath string,
	outputBaseDir string,
) *Processor {
	return &Processor{
		jobs:             jobs,
		pythonExecutable: pythonExecutable,
		workerScriptPath: workerScriptPath,
		outputBaseDir:    outputBaseDir,
	}
}

func (p *Processor) Start(job models.Job) {
	go p.process(job)
}

func (p *Processor) process(job models.Job) {
	ctx := context.Background()
	startedAt := time.Now()

	log.Printf("job=%s event=worker_start portfolio_url=%q", job.ID, job.PortfolioURL)

	if err := p.jobs.UpdateJobStatus(ctx, job.ID, models.JobStatusInProgress, nil); err != nil {
		log.Printf("job=%s update in_progress failed: %v", job.ID, err)
		return
	}
	log.Printf("job=%s event=status_updated status=%s", job.ID, models.JobStatusInProgress)

	outputDir := filepath.Join(p.outputBaseDir, job.ID.String())
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		errMsg := fmt.Sprintf("create output directory: %v", err)
		_ = p.jobs.UpdateJobStatus(ctx, job.ID, models.JobStatusFailed, &errMsg)
		log.Printf("job=%s %s", job.ID, errMsg)
		return
	}

	cmd := exec.CommandContext(
		ctx,
		p.pythonExecutable,
		p.workerScriptPath,
		"--url",
		job.PortfolioURL,
		"--output",
		outputDir,
	)

	log.Printf(
		"job=%s event=worker_command_start command=%q script=%q output_dir=%q",
		job.ID,
		p.pythonExecutable,
		p.workerScriptPath,
		outputDir,
	)

	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = io.MultiWriter(os.Stdout, &stdout)
	cmd.Stderr = io.MultiWriter(os.Stderr, &stderr)

	if err := cmd.Run(); err != nil {
		stderrText := strings.TrimSpace(stderr.String())
		stdoutText := strings.TrimSpace(stdout.String())

		errMsg := buildWorkerErrorMessage(err, stdoutText, stderrText)

		errMsg = truncate(errMsg, 12_000)
		if updateErr := p.jobs.UpdateJobStatus(ctx, job.ID, models.JobStatusFailed, &errMsg); updateErr != nil {
			log.Printf("job=%s update failed status failed: %v", job.ID, updateErr)
		}

		exitCode := extractExitCode(err)
		log.Printf(
			"job=%s event=worker_command_failed exit_code=%d duration=%s error=%q",
			job.ID,
			exitCode,
			time.Since(startedAt).Round(time.Millisecond),
			err,
		)
		log.Printf("job=%s event=status_updated status=%s", job.ID, models.JobStatusFailed)
		return
	}

	if err := p.jobs.UpdateJobStatus(ctx, job.ID, models.JobStatusCompleted, nil); err != nil {
		log.Printf("job=%s update completed failed: %v", job.ID, err)
		return
	}

	log.Printf(
		"job=%s event=worker_completed status=%s duration=%s",
		job.ID,
		models.JobStatusCompleted,
		time.Since(startedAt).Round(time.Millisecond),
	)
}

func truncate(value string, maxLength int) string {
	if maxLength <= 0 || len(value) <= maxLength {
		return value
	}
	return value[:maxLength]
}

func extractExitCode(runErr error) int {
	if runErr == nil {
		return 0
	}

	exitErr, ok := runErr.(*exec.ExitError)
	if !ok {
		return -1
	}

	return exitErr.ExitCode()
}

func buildWorkerErrorMessage(runErr error, stdoutText string, stderrText string) string {
	builder := strings.Builder{}
	builder.WriteString(fmt.Sprintf("worker command failed: %v", runErr))

	if exitCode := extractExitCode(runErr); exitCode != -1 {
		builder.WriteString(fmt.Sprintf(" (exit_code=%d)", exitCode))
	}

	trimmedStderr := truncate(stderrText, 4_000)
	trimmedStdout := truncate(stdoutText, 4_000)

	if trimmedStderr != "" {
		builder.WriteString("\n\n--- stderr ---\n")
		builder.WriteString(trimmedStderr)
	}

	if trimmedStdout != "" {
		builder.WriteString("\n\n--- stdout ---\n")
		builder.WriteString(trimmedStdout)
	}

	return builder.String()
}
