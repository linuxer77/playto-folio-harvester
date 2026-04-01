"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { type CreateJobPayload } from "@/types/job"

const formSchema = z.object({
  client_name: z.string().trim(),
  job_title: z.string().trim(),
  candidate_name: z.string().trim(),
  portfolio_url: z
    .string()
    .trim()
    .url("Please enter a valid URL (including https://)."),
})

type JobFormValues = z.infer<typeof formSchema>

interface JobSubmissionFormProps {
  isSubmitting: boolean
  onSubmit: (payload: CreateJobPayload) => Promise<boolean>
}

export function JobSubmissionForm({
  isSubmitting,
  onSubmit,
}: JobSubmissionFormProps) {
  const form = useForm<JobFormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      client_name: "",
      job_title: "",
      candidate_name: "",
      portfolio_url: "",
    },
  })

  const handleSubmit = form.handleSubmit(async (values) => {
    const payload: CreateJobPayload = {
      portfolio_url: values.portfolio_url,
    }

    if (values.client_name !== "") {
      payload.client_name = values.client_name
    }
    if (values.job_title !== "") {
      payload.job_title = values.job_title
    }
    if (values.candidate_name !== "") {
      payload.candidate_name = values.candidate_name
    }

    const success = await onSubmit(payload)

    if (success) {
      form.reset()
    }
  })

  return (
    <Card className="border-0 bg-white/85 shadow-md ring-1 ring-slate-900/10 backdrop-blur-sm">
      <CardHeader>
        <CardTitle>Create Harvest Job</CardTitle>
        <CardDescription>
          Only Portfolio URL is required. Add client, role, and candidate details when useful.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="grid gap-4 md:grid-cols-2">
              <FormField
                control={form.control}
                name="client_name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Client Name</FormLabel>
                    <FormControl>
                      <Input placeholder="Playto" autoComplete="off" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="job_title"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Job Title</FormLabel>
                    <FormControl>
                      <Input
                        placeholder="Senior Visual Designer"
                        autoComplete="off"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="candidate_name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Candidate Name</FormLabel>
                    <FormControl>
                      <Input placeholder="Internal only" autoComplete="off" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="portfolio_url"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Portfolio URL</FormLabel>
                    <FormControl>
                      <Input
                        placeholder="https://portfolio.example.com"
                        type="url"
                        autoComplete="off"
                        {...field}
                      />
                    </FormControl>
                    <FormDescription>
                      Use a full URL so the harvester can resolve dynamic websites correctly.
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <Button type="submit" disabled={isSubmitting} className="w-full md:w-auto">
              {isSubmitting ? "Queueing Job..." : "Harvest Media"}
            </Button>
          </form>
        </Form>
      </CardContent>
    </Card>
  )
}
