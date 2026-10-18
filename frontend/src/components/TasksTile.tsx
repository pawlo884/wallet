import { useState, type FormEvent } from "react";
import { api, isoDate, type CalEvent, type Task } from "../api";
import { DashboardTile } from "./DashboardTile";

interface Props {
  open: Task[];
  done: Task[];
  doneToday: number;
  google: boolean;
  calendar: CalEvent[];
  onChange: () => Promise<void>;
  onError: (msg: string) => void;
}

const GROUPS: {
  key: string;
  label: string;
  test: (t: Task, today: string) => boolean;
}[] = [
  { key: "overdue", label: "Zaległe", test: (t) => t.overdue },
  { key: "today", label: "Dziś", test: (t, today) => t.due === today },
  {
    key: "soon",
    label: "Najbliższe",
    test: (t, today) => !!t.due && t.due > today,
  },
  { key: "later", label: "Bez terminu", test: (t) => !t.due },
];

const DAY = (iso: string, today: string) => {
  if (iso === today) return "Dziś";
  if (iso === isoDate(1)) return "Jutro";
  return new Date(`${iso}T12:00`).toLocaleDateString("pl-PL", {
    weekday: "short",
    day: "numeric",
    month: "numeric",
  });
};

export function TasksTile({
  open,
  done,
  doneToday,
  google,
  calendar,
  onChange,
  onError,
}: Props) {
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [time, setTime] = useState("");
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
      await api.addTask(title.trim(), due || null, (due && time) || null);
      setTitle("");
      setDue("");
      setTime("");
    });
  };

  return (
    <DashboardTile
      title="Zadania"
      subtitle={`Otwarte: ${open.length} · zrobione dziś: ${doneToday}`}
      accent="sky"
      span="tall"
    >
      <form onSubmit={add} className="mb-4 flex flex-col gap-2">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Nowe zadanie…"
          className="min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-600"
        />
        <div className="flex gap-2">
          <input
            type="date"
            value={due}
            onChange={(e) => setDue(e.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-300 outline-none focus:border-sky-600"
          />
          <input
            type="time"
            value={time}
            disabled={!due}
            title={due ? "Godzina (opcjonalnie)" : "Najpierw wybierz dzień"}
            onChange={(e) => setTime(e.target.value)}
            className="w-28 shrink-0 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-300 outline-none focus:border-sky-600 disabled:opacity-40"
          />
          <button className="shrink-0 rounded-lg bg-sky-700 px-4 py-2 text-sm font-medium hover:bg-sky-600">
            Dodaj
          </button>
        </div>
      </form>

      {calendar.length > 0 && <Calendar events={calendar} today={today} />}
      {!google && (
        <p className="mb-4 text-xs text-slate-600">
          Kalendarz Google niepołączony. Napisz do bota /google, a zadania z
          terminem pojawią się w kalendarzu.
        </p>
      )}

      {open.length === 0 && (
        <p className="text-sm text-slate-500">
          Pusto. Dodaj zadanie tutaj albo napisz do bota, np. „jutro zadzwonić
          do ubezpieczyciela”.
        </p>
      )}

      {GROUPS.map((g) => {
        const items = open.filter((t) => g.test(t, today));
        if (!items.length) return null;
        return (
          <div key={g.key} className="mb-4">
            <h3
              className={`mb-1 text-xs font-semibold uppercase ${g.key === "overdue" ? "text-rose-400" : "text-slate-500"}`}
            >
              {g.label} ({items.length})
            </h3>
            <ul className="divide-y divide-slate-800/80">
              {items.map((t) => (
                <li key={t.id} className="group flex items-center gap-3 py-2">
                  <button
                    title="Zrobione"
                    onClick={() =>
                      run(() => api.updateTask(t.id, { status: "done" }))
                    }
                    className="h-5 w-5 shrink-0 rounded-full border border-slate-600 hover:border-sky-400 hover:bg-sky-900"
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm">
                      {t.title}
                      {t.gcal_id && (
                        <span
                          title="W Kalendarzu Google"
                          className="ml-1.5 text-xs"
                        >
                          📅
                        </span>
                      )}
                    </p>
                    <p className="text-xs text-slate-500">
                      {t.due_label}
                      {t.moves >= 2 && (
                        <span className="ml-2 text-amber-500">
                          przesuwane {t.moves}×
                        </span>
                      )}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-1 text-xs opacity-100 sm:opacity-0 sm:group-hover:opacity-100">
                    <button
                      onClick={() =>
                        run(() => api.updateTask(t.id, { due: isoDate(1) }))
                      }
                      className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800"
                    >
                      Jutro
                    </button>
                    <button
                      onClick={() =>
                        run(() => api.updateTask(t.id, { due: isoDate(7) }))
                      }
                      className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800"
                    >
                      +7 dni
                    </button>
                    <button
                      onClick={() =>
                        confirm(`Usunąć „${t.title}”?`) &&
                        run(() => api.deleteTask(t.id))
                      }
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
          <button
            onClick={() => setShowDone(!showDone)}
            className="text-xs text-slate-500 hover:text-slate-300"
          >
            {showDone ? "▾" : "▸"} Zrobione w ostatnim tygodniu ({done.length})
          </button>
          {showDone && (
            <ul className="mt-2 space-y-1">
              {done.map((t) => (
                <li
                  key={t.id}
                  className="flex items-center gap-2 text-sm text-slate-500"
                >
                  <span className="line-through">{t.title}</span>
                  <button
                    onClick={() =>
                      run(() => api.updateTask(t.id, { status: "open" }))
                    }
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

function Calendar({ events, today }: { events: CalEvent[]; today: string }) {
  const days = [...new Set(events.map((e) => e.start.slice(0, 10)))].slice(
    0,
    3,
  );
  return (
    <div className="mb-4 rounded-lg bg-slate-950/60 px-3 py-2">
      <h3 className="mb-1 text-xs font-semibold uppercase text-slate-500">
        W kalendarzu
      </h3>
      {days.map((d) => (
        <div key={d} className="mb-1 text-xs">
          <p className="text-slate-500">{DAY(d, today)}</p>
          <ul>
            {events
              .filter((e) => e.start.slice(0, 10) === d)
              .map((e, i) => (
                <li
                  key={i}
                  className="flex gap-2 text-slate-300"
                  title={e.calendar}
                >
                  <span className="w-20 shrink-0 text-slate-500">
                    {e.all_day
                      ? "cały dzień"
                      : `${e.start.slice(11)}–${e.end.slice(11)}`}
                  </span>
                  <span className="truncate">{e.title}</span>
                </li>
              ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
