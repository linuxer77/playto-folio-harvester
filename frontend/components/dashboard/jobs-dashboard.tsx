"use client"

import { useCallback, useEffect, useState } from "react"

import { JobSubmissionForm } from "@/components/dashboard/job-submission-form"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { toast } from "@/components/ui/toast"
import { JobsTable } from "@/components/dashboard/jobs-table"
import { type CreateJobPayload, type HarvestJob } from "@/types/job"

const POLL_INTERVAL_MS = 3000
const GOOGLE_AUTH_POPUP_FEATURES = "width=560,height=760,resizable=yes,scrollbars=yes"

const TOKEN_ERROR_MARKERS = [
  "google drive oauth token is missing/invalid",
  "invalid_grant",
  "token has been expired or revoked",
]

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

function isGoogleTokenFailure(job: HarvestJob): boolean {
  if (job.status !== "failed" || !job.error_logs) {
    return false
  }

  const lower = job.error_logs.toLowerCase()
  return TOKEN_ERROR_MARKERS.some((marker) => lower.includes(marker))
}

export function JobsDashboard() {
  const [jobs, setJobs] = useState<HarvestJob[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [isAuthorizingGoogle, setIsAuthorizingGoogle] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const needsGoogleReconnect = jobs.some(isGoogleTokenFailure)

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

  const reconnectGoogleDrive = useCallback(() => {
    if (typeof window === "undefined") {
      return
    }

    const popup = window.open(
      "/api/auth/google/start",
      "playto-google-auth",
      GOOGLE_AUTH_POPUP_FEATURES
    )

    if (!popup) {
      toast.error("Please allow popups to reconnect Google Drive.")
      return
    }

    setIsAuthorizingGoogle(true)

    let cleaned = false
    let closeWatcher = 0
    let timeoutHandle = 0

    const cleanup = () => {
      if (cleaned) {
        return
      }
      cleaned = true

      if (closeWatcher) {
        window.clearInterval(closeWatcher)
      }
      if (timeoutHandle) {
        window.clearTimeout(timeoutHandle)
      }
      window.removeEventListener("message", onMessage)
      setIsAuthorizingGoogle(false)
    }

    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) {
        return
      }
      if (!event.data || typeof event.data !== "object") {
        return
      }

      const payload = event.data as { type?: string; message?: string }
      if (payload.type === "google-auth-success") {
        toast.success(payload.message || "Google Drive connected successfully.")
        cleanup()
        void loadJobs()
        return
      }

      if (payload.type === "google-auth-error") {
        toast.error(payload.message || "Google authorization failed.")
        cleanup()
      }
    }

    window.addEventListener("message", onMessage)

    closeWatcher = window.setInterval(() => {
      if (popup.closed) {
        cleanup()
      }
    }, 500)

    timeoutHandle = window.setTimeout(() => {
      if (!popup.closed) {
        popup.close()
      }
      toast.error("Google authorization timed out. Please try again.")
      cleanup()
    }, 5 * 60 * 1000)
  }, [loadJobs])

  return (
    <div className="space-y-6">
      {needsGoogleReconnect ? (
        <Card className="border-amber-300 bg-amber-50/90 ring-amber-300/40">
          <CardHeader>
            <CardTitle>Reconnect Google Drive</CardTitle>
            <CardDescription>
              A recent job failed because the Google token expired. Reconnect once, then retry.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3">
            <Button onClick={reconnectGoogleDrive} disabled={isAuthorizingGoogle}>
              {isAuthorizingGoogle ? "Waiting For Google Auth..." : "Open Google Auth"}
            </Button>
            <p className="text-sm text-slate-600">This opens a popup and saves token.json on the backend.</p>
          </CardContent>
        </Card>
      ) : null}
      <JobSubmissionForm isSubmitting={isSubmitting} onSubmit={createJob} />
      <JobsTable jobs={jobs} isLoading={isLoading} loadError={loadError} />
    </div>
  )
}
