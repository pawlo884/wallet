import { useCallback, useEffect, useState } from "react";
import { api, type Book, type Overview, type ReadingStats, type TasksData } from "./api";
import { ActivityTile } from "./components/ActivityTile";
import { BooksTile } from "./components/BooksTile";
import { DashboardTile } from "./components/DashboardTile";
import { MoneyTile } from "./components/MoneyTile";
import { TasksTile } from "./components/TasksTile";

const COMING = [
  { title: "Kolejni agenci", subtitle: "Pilnowanie, fitness, samorozwój", accent: "slate" as const },
];

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [tasks, setTasks] = useState<TasksData>({ open: [], done: [], google: false, calendar: [] });
  const [books, setBooks] = useState<Book[]>([]);
  const [reading, setReading] = useState<ReadingStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const load = useCallback(async () => {
    const [t, b] = await Promise.all([api.tasks(), api.books()]);
    setTasks(t);
    setBooks(b.books);
    setReading(b.reading);
    setRefreshKey((k) => k + 1);
    api.overview().then(setOverview).catch((e) => setError(e.message)); // Wallet bywa wolny — nie blokuje list
  }, []);

  useEffect(() => {
    load().catch((e) => setError(e.message));
    const timer = setInterval(() => load().catch(() => {}), 60_000); // zmiany z Telegrama widać bez odświeżania
    return () => clearInterval(timer);
  }, [load]);

  const today = new Date().toLocaleDateString("pl-PL", { weekday: "long", day: "numeric", month: "long" });

  return (
    <div className="mx-auto max-w-7xl p-4 sm:p-6">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Asystent</h1>
          <p className="text-slate-400 first-letter:uppercase">{today}</p>
        </div>
        {overview && (
          <p className="text-sm text-slate-400">
            {overview.tasks.overdue > 0 && <span className="text-rose-400">Zaległe: {overview.tasks.overdue} · </span>}
            Na dziś: {overview.tasks.today} · czytasz: {overview.books.reading}
          </p>
        )}
      </header>

      {error && (
        <div className="mb-4 flex items-center justify-between rounded-lg border border-rose-900 bg-rose-950/50 px-4 py-2 text-sm text-rose-200">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="text-rose-400">
            ✕
          </button>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <TasksTile
          open={tasks.open}
          done={tasks.done}
          google={tasks.google}
          calendar={tasks.calendar}
          doneToday={overview?.tasks.done_today ?? 0}
          onChange={load}
          onError={setError}
        />
        <BooksTile books={books} reading={reading} onChange={load} onError={setError} />
        <MoneyTile overview={overview} />
        {COMING.map((c) => (
          <DashboardTile key={c.title} title={c.title} subtitle={c.subtitle} accent={c.accent} comingSoon>
            <p className="text-sm text-slate-500">
              Plan dnia rano i podsumowanie wieczorem, Strava i cele treningowe, nawyki. Kolejne etapy planu.
            </p>
          </DashboardTile>
        ))}
        <ActivityTile overview={overview} refreshKey={refreshKey} />
      </div>
    </div>
  );
}
