package db

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func NewPool(ctx context.Context, databaseURL string) (*pgxpool.Pool, error) {
	if databaseURL == "" {
		return nil, fmt.Errorf("DATABASE_URL is required")
	}

	cfg, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse database config: %w", err)
	}

	log.Printf(
		"db_connect_start host=%s port=%d database=%s user=%s",
		cfg.ConnConfig.Host,
		cfg.ConnConfig.Port,
		cfg.ConnConfig.Database,
		cfg.ConnConfig.User,
	)

	const (
		maxAttempts   = 30
		retryInterval = 2 * time.Second
		pingTimeout   = 5 * time.Second
	)

	var lastPingErr error
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		pool, err := pgxpool.NewWithConfig(ctx, cfg)
		if err != nil {
			return nil, fmt.Errorf("create pool: %w", err)
		}

		pingCtx, cancel := context.WithTimeout(ctx, pingTimeout)
		pingErr := pool.Ping(pingCtx)
		cancel()

		if pingErr == nil {
			log.Printf("db_connect_success host=%s port=%d database=%s", cfg.ConnConfig.Host, cfg.ConnConfig.Port, cfg.ConnConfig.Database)
			return pool, nil
		}

		pool.Close()
		lastPingErr = pingErr

		if attempt == maxAttempts {
			break
		}

		log.Printf(
			"db_connect_retry attempt=%d max=%d wait_seconds=%d error=%q",
			attempt,
			maxAttempts,
			int(retryInterval.Seconds()),
			pingErr,
		)

		timer := time.NewTimer(retryInterval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, fmt.Errorf("ping database canceled: %w", ctx.Err())
		case <-timer.C:
		}
	}

	return nil, fmt.Errorf("ping database: %w", lastPingErr)
}
