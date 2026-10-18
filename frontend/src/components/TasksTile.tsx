import { useState, type FormEvent } from "react";
import { api, isoDate, type Task } from "../api";
import { DashboardTile } from "./DashboardTile";

interface Props {
  open: Task[];
  done: Task[];
  doneToday: number;
  onChange: () => Promise<void>;
  onError: (msg: string) => void;
}

const GROUPS: { key: string; label: string; test: (t: Task, today: string) => boolean }[] = [
  { key: "overdue", label: "Zaległe", test: (t) => t.overdue },
  { key: "today", label: "Dziś", test: (t, today) => t.due === today },
  { key: "soon", label: "Najbliższe", test: (t, today) => !!t.due && t.due > today },
  { key: "later", label: "Bez terminu", test: (t) => !t.due },
];

export function TasksTile({ open, done, doneToday, onChange, onError }: Props) {
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [showDone, setShowDone] = useState(false);
  const today = isoDate();

  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await onChange();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const add = (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    run(async () => {
      await api.addTask(title.trim(), due || null);
      setTitle("");
      setDue("");
    });
  };

  return (
    <DashboardTile
      title="Zadania"
      subtitle={`Otwarte: ${open.length} · zrobione dziś: ${doneToday}`}
      accent="sky"
      span="tall"
    >
      <form onSubmit={add} className="mb-4 flex flex-col gap-2 sm:flex-row">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Nowe zadanie…"
          className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-600"
        />
        <input
          type="date"
          value={due}
          onChange={(e) => setDue(e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-300 outline-none focus:border-sky-600"
        />
        <button className="rounded-lg bg-sky-700 px-4 py-2 text-sm font-medium hover:bg-sky-600">Dodaj</button>
      </form>

      {open.length === 0 && (
        <p className="text-sm text-slate-500">
          Pusto. Dodaj zadanie tutaj albo napisz do bota, np. „jutro zadzwonić do ubezpieczyciela”.
        </p>
      )}

      {GROUPS.map((g) => {
        const items = open.filter((t) => g.test(t, today));
        if (!items.length) return null;
        return (
          <div key={g.key} className="mb-4">
            <h3 className={`mb-1 text-xs font-semibold uppercase ${g.key === "overdue" ? "text-rose-400" : "text-slate-500"}`}>
              {g.label} ({items.length})
            </h3>
            <ul className="divide-y divide-slate-800/80">
              {items.map((t) => (
                <li key={t.id} className="group flex items-center gap-3 py-2">
                  <button
                    title="Zrobione"
                    onClick={() => run(() => api.updateTask(t.id, { status: "done" }))}
                    className="h-5 w-5 shrink-0 rounded-full border border-slate-600 hover:border-sky-400 hover:bg-sky-900"
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm">{t.title}</p>
                    <p className="text-xs text-slate-500">
                      {t.due_label}
                      {t.moves >= 2 && <span className="ml-2 text-amber-500">przesuwane {t.moves}×</span>}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-1 text-xs opacity-100 sm:opacity-0 sm:group-hover:opacity-100">
                    <button
                      onClick={() => run(() => api.updateTask(t.id, { due: isoDate(1) }))}
                      className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800"
                    >
                      Jutro
                    </button>
                    <button
                      onClick={() => run(() => api.updateTask(t.id, { due: isoDate(7) }))}
                      className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800"
                    >
                      +7 dni
                    </button>
                    <button
                      onClick={() => confirm(`Usunąć „${t.title}”?`) && run(() => api.deleteTask(t.id))}
                      className="rounded px-2 py-1 text-slate-500 hover:bg-rose-950 hover:text-rose-300"
                    >
                      Usuń
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        );
      })}

      {done.length > 0 && (
        <div>
          <button onClick={() => setShowDone(!showDone)} className="text-xs text-slate-500 hover:text-slate-300">
            {showDone ? "▾" : "▸"} Zrobione w ostatnim tygodniu ({done.length})
          </button>
          {showDone && (
            <ul className="mt-2 space-y-1">
              {done.map((t) => (
                <li key={t.id} className="flex items-center gap-2 text-sm text-slate-500">
                  <span className="line-through">{t.title}</span>
                  <button
                    onClick={() => run(() => api.updateTask(t.id, { status: "open" }))}
                    className="text-xs text-slate-600 hover:text-slate-300"
                  >
                    przywróć
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </DashboardTile>
  );
}
