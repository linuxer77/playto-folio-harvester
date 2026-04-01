"use client"

import { useCallback, useEffect, useState } from "react"

import { JobSubmissionForm } from "@/components/dashboard/job-submission-form"
import { toast } from "@/components/ui/toast"
import { JobsTable } from "@/components/dashboard/jobs-table"
import { type CreateJobPayload, type HarvestJob } from "@/types/job"

const POLL_INTERVAL_MS = 3000

interface LoadJobsOptions {
  signal?: AbortSignal
  silent?: boolean
}

async function readErrorMessage(response: Response): Promise<string> {
  const fallback = `Request failed with status ${response.status}.`

  try {
    const payload = (await response.json()) as { error?: string; message?: string }
    return payload.error || payload.message || fallback
  } catch {
    return fallback
  }
}

export function JobsDashboard() {
  const [jobs, setJobs] = useState<HarvestJob[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const loadJobs = useCallback(async ({ signal, silent = false }: LoadJobsOptions = {}) => {
    try {
      const response = await fetch("/api/jobs", {
        method: "GET",
        cache: "no-store",
        signal,
      })

      if (!response.ok) {
        throw new Error(await readErrorMessage(response))
      }

      const payload = (await response.json()) as HarvestJob[]
      setJobs(Array.isArray(payload) ? payload : [])
      setLoadError(null)
    } catch (error) {
      if ((error as Error).name === "AbortError") {
        return
      }

      if (!silent) {
        setLoadError(
          error instanceof Error ? error.message : "Could not load jobs from the API."
        )
      }
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()

    void loadJobs({ signal: controller.signal })

    const intervalId = window.setInterval(() => {
      void loadJobs({ silent: true })
    }, POLL_INTERVAL_MS)

    return () => {
      controller.abort()
      window.clearInterval(intervalId)
    }
  }, [loadJobs])

  const createJob = useCallback(
    async (payload: CreateJobPayload) => {
      setIsSubmitting(true)

      try {
        const response = await fetch("/api/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        })

        if (!response.ok) {
          throw new Error(await readErrorMessage(response))
        }

        toast.success("Harvest job queued successfully.")
        await loadJobs()
        return true
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : "Failed to submit harvest job."
        )
        return false
      } finally {
        setIsSubmitting(false)
      }
    },
    [loadJobs]
  )

  return (
    <div className="space-y-6">
      <JobSubmissionForm isSubmitting={isSubmitting} onSubmit={createJob} />
      <JobsTable jobs={jobs} isLoading={isLoading} loadError={loadError} />
    </div>
  )
}
