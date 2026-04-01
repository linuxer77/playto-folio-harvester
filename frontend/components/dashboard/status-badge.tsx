import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { type JobStatus } from "@/types/job"

const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  in_progress: "In Progress",
  completed: "Completed",
  failed: "Failed",
}

const STATUS_CLASSES: Record<JobStatus, string> = {
  queued: "border-yellow-300 bg-yellow-100 text-yellow-900",
  in_progress: "border-blue-300 bg-blue-100 text-blue-900",
  completed: "border-emerald-300 bg-emerald-100 text-emerald-900",
  failed: "border-red-300 bg-red-100 text-red-900",
}

interface StatusBadgeProps {
  status: JobStatus
}

export function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <Badge
      variant="outline"
      className={cn("rounded-md px-2 py-0.5 text-xs font-semibold", STATUS_CLASSES[status])}
    >
      {STATUS_LABELS[status]}
    </Badge>
  )
}
