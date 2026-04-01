"use client"

import { useState } from "react"
import { ExternalLink } from "lucide-react"

import { StatusBadge } from "@/components/dashboard/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { type HarvestJob } from "@/types/job"

interface JobsTableProps {
  jobs: HarvestJob[]
  isLoading: boolean
  loadError: string | null
}

export function JobsTable({ jobs, isLoading, loadError }: JobsTableProps) {
  const [expandedErrors, setExpandedErrors] = useState<Record<string, boolean>>({})

  const toggleError = (jobId: string) => {
    setExpandedErrors((prev) => ({
      ...prev,
      [jobId]: !prev[jobId],
    }))
  }

  return (
    <Card className="border-0 bg-white/85 shadow-md ring-1 ring-slate-900/10 backdrop-blur-sm">
      <CardHeader>
        <CardTitle>Job History</CardTitle>
        <CardDescription>
          Status refreshes every 3 seconds while workers process queued jobs.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loadError ? (
          <p className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {loadError}
          </p>
        ) : null}

        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Client &amp; Job</TableHead>
              <TableHead>Candidate</TableHead>
              <TableHead>URL</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Result</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && jobs.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  Loading jobs...
                </TableCell>
              </TableRow>
            ) : null}

            {!isLoading && jobs.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  No jobs submitted yet.
                </TableCell>
              </TableRow>
            ) : null}

            {jobs.map((job) => {
              const isErrorVisible = expandedErrors[job.id]
              const hasErrorLogs = Boolean(job.error_logs)

              return (
                <TableRow key={job.id}>
                  <TableCell className="font-medium whitespace-normal">
                    <p>{job.client_name}</p>
                    <p className="text-xs text-muted-foreground">{job.job_title}</p>
                  </TableCell>
                  <TableCell>{job.candidate_name}</TableCell>
                  <TableCell>
                    <a
                      href={job.portfolio_url}
                      target="_blank"
                      rel="noreferrer"
                      className="block max-w-[20rem] truncate text-sm text-blue-700 underline-offset-2 hover:underline"
                      title={job.portfolio_url}
                    >
                      {job.portfolio_url}
                    </a>
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={job.status} />
                  </TableCell>
                  <TableCell className="max-w-[24rem] whitespace-normal">
                    {job.status === "completed" && job.drive_link ? (
                      <a
                        href={job.drive_link}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-sm text-emerald-700 underline-offset-2 hover:underline"
                        title={job.drive_link}
                      >
                        Drive Link
                        <ExternalLink className="size-3.5" />
                      </a>
                    ) : null}

                    {job.status === "completed" && !job.drive_link ? (
                      <span className="text-sm text-muted-foreground">No link available.</span>
                    ) : null}

                    {job.status === "failed" ? (
                      <div className="space-y-2">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => toggleError(job.id)}
                        >
                          {isErrorVisible ? "Hide Error" : "View Error"}
                        </Button>
                        {isErrorVisible && hasErrorLogs ? (
                          <p className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700 whitespace-pre-wrap">
                            {job.error_logs}
                          </p>
                        ) : null}
                        {isErrorVisible && !hasErrorLogs ? (
                          <p className="text-xs text-muted-foreground">No error logs available.</p>
                        ) : null}
                      </div>
                    ) : null}

                    {job.status !== "completed" && job.status !== "failed" ? (
                      <span className="text-sm text-muted-foreground">Pending</span>
                    ) : null}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
