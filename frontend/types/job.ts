export type JobStatus = "queued" | "in_progress" | "completed" | "failed"

export interface HarvestJob {
  id: string
  client_name: string
  job_title: string
  candidate_name: string
  portfolio_url: string
  status: JobStatus
  drive_link?: string | null
  error_logs?: string | null
  created_at?: string
}

export interface CreateJobPayload {
  client_name?: string
  job_title?: string
  candidate_name?: string
  portfolio_url: string
}
