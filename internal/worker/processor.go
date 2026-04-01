package worker

import (
	"bytes"
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

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

	if err := p.jobs.UpdateJobStatus(ctx, job.ID, models.JobStatusInProgress, nil); err != nil {
		log.Printf("job=%s update in_progress failed: %v", job.ID, err)
		return
	}

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

	var stderr bytes.Buffer
	cmd.Stdout = os.Stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		errMsg := strings.TrimSpace(stderr.String())
		if errMsg == "" {
			errMsg = err.Error()
		}

		errMsg = truncate(errMsg, 12_000)
		if updateErr := p.jobs.UpdateJobStatus(ctx, job.ID, models.JobStatusFailed, &errMsg); updateErr != nil {
			log.Printf("job=%s update failed status failed: %v", job.ID, updateErr)
		}
		log.Printf("job=%s worker command failed: %v", job.ID, err)
		return
	}

	if err := p.jobs.UpdateJobStatus(ctx, job.ID, models.JobStatusCompleted, nil); err != nil {
		log.Printf("job=%s update completed failed: %v", job.ID, err)
	}
}

func truncate(value string, maxLength int) string {
	if maxLength <= 0 || len(value) <= maxLength {
		return value
	}
	return value[:maxLength]
}
