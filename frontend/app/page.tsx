import { JobsDashboard } from "@/components/dashboard/jobs-dashboard"

export default function Home() {
  return (
    <div className="min-h-screen bg-[linear-gradient(120deg,#eef2ff_0%,#f8fafc_40%,#ecfeff_100%)]">
      <header className="border-b border-slate-300/60 bg-white/80 backdrop-blur-sm">
        <nav className="mx-auto flex h-16 w-full max-w-7xl items-center px-4 sm:px-6 lg:px-8">
          <h1 className="text-lg font-semibold tracking-tight text-slate-900 sm:text-xl">
            Playto Folio Harvester
          </h1>
        </nav>
      </header>

      <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <JobsDashboard />
      </main>
    </div>
  )
}
