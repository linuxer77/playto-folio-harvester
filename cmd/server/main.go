package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"playto-folio-harvester/internal/api"
	"playto-folio-harvester/internal/db"
	"playto-folio-harvester/internal/repository"
	"playto-folio-harvester/internal/worker"
)

func main() {
	databaseURL := os.Getenv("DATABASE_URL")
	port := envOrDefault("PORT", "8080")
	pythonExecutable := envOrDefault("PYTHON_BIN", "python")
	workerScriptPath := envOrDefault("WORKER_SCRIPT_PATH", "worker/harvester.py")
	outputBaseDir := envOrDefault("HARVEST_OUTPUT_BASE", "./temp_harvest")

	resolvedWorkerScriptPath, err := filepath.Abs(workerScriptPath)
	if err != nil {
		log.Fatalf("resolve worker script path: %v", err)
	}

	ctx := context.Background()
	pool, err := db.NewPool(ctx, databaseURL)
	if err != nil {
		log.Fatalf("connect to postgres: %v", err)
	}
	defer pool.Close()

	jobsRepo := repository.NewPostgresJobRepository(pool)
	processor := worker.NewProcessor(
		jobsRepo,
		pythonExecutable,
		resolvedWorkerScriptPath,
		filepath.Clean(outputBaseDir),
	)

	router := api.NewRouter(api.NewJobHandler(jobsRepo, processor))
	server := &http.Server{
		Addr:              ":" + port,
		Handler:           router,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		log.Printf("API listening on http://localhost:%s", port)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("server failed: %v", err)
		}
	}()

	shutdownCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	<-shutdownCtx.Done()

	gracefulCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := server.Shutdown(gracefulCtx); err != nil {
		log.Printf("server shutdown error: %v", err)
	}
}

func envOrDefault(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}
