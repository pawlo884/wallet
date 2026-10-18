import { Fragment, useEffect, useState } from "react";
import { api, usd, type DayStat, type EventRow, type Overview } from "../api";
import { DashboardTile } from "./DashboardTile";

const KINDS: { kind: string; label: string; icon: string }[] = [
  { kind: "", label: "Wszystko", icon: "" },
  { kind: "message", label: "Wiadomości", icon: "💬" },
  { kind: "llm", label: "Model", icon: "🧠" },
  { kind: "button", label: "Przyciski", icon: "🔘" },
  { kind: "command", label: "Komendy", icon: "⌨️" },
  { kind: "notify", label: "Od bota", icon: "🔔" },
  { kind: "panel", label: "Panel", icon: "🖥" },
  { kind: "error", label: "Błędy", icon: "⚠️" },
];
const ICON = Object.fromEntries(KINDS.map((k) => [k.kind, k.icon]));

const AGENT_COLOR: Record<string, string> = {
  wydatki: "bg-emerald-900/60 text-emerald-300",
  zadania: "bg-sky-900/60 text-sky-300",
  ksiazki: "bg-violet-900/60 text-violet-300",
  kalendarz: "bg-teal-900/60 text-teal-300",
  panel: "bg-slate-800 text-slate-300",
};

const time = (at: number) =>
  new Date(at * 1000).toLocaleString("pl-PL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

export function ActivityTile({ overview, refreshKey }: { overview: Overview | null; refreshKey: number }) {
  const [kind, setKind] = useState("");
  const [events, setEvents] = useState<EventRow[]>([]);
  const [days, setDays] = useState<DayStat[]>([]);
  const [openTrace, setOpenTrace] = useState<number | null>(null);
  const [more, setMore] = useState(true);

  useEffect(() => {
    api.events(kind || undefined).then((r) => {
      setEvents(r.events);
      setMore(r.events.length === 50);
    });
  }, [kind, refreshKey]);

  useEffect(() => {
    api.daily(91).then((r) => setDays(r.days));
  }, [refreshKey]);

  const loadMore = async () => {
    const last = events[events.length - 1];
    if (!last) return;
    const r = await api.events(kind || undefined, last.id);
    setEvents([...events, ...r.events]);
    setMore(r.events.length === 50);
  };

  const cost = overview?.cost;
  const maxCost = Math.max(...days.map((d) => d.cost_usd), 0.0001);
  const maxActions = Math.max(...days.map((d) => d.actions), 1);
  const last30 = days.slice(-30);

  return (
    <DashboardTile title="Co robi bot" subtitle="Dziennik zdarzeń i koszt modelu" accent="amber" span="full">
      <div className="mb-5 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Stat label="Model dziś" value={cost ? usd(cost.today_usd) : "…"} />
        <Stat
          label="Model w tym miesiącu"
          value={cost ? usd(cost.month_usd) : "…"}
          hint={cost?.budget_usd ? `limit ${usd(cost.budget_usd)}` : undefined}
          warn={!!cost?.budget_usd && cost.month_usd >= cost.budget_usd}
        />
        <Stat label="Wywołania modelu (miesiąc)" value={cost ? String(cost.month_calls) : "…"} />
        <Stat label="Twoje akcje (30 dni)" value={String(last30.reduce((s, d) => s + d.actions, 0))} />
      </div>

      <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase text-slate-500">Aktywność (13 tygodni)</h3>
          <div className="grid grid-flow-col grid-rows-7 gap-1 overflow-x-auto">
            {days.map((d) => (
              <div
                key={d.date}
                title={`${d.date}: ${d.actions} akcji, ${d.llm} wywołań modelu`}
                className={`h-3 w-3 rounded-sm ${heat(d.actions / maxActions, d.actions)}`}
              />
            ))}
          </div>
        </div>
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase text-slate-500">Koszt modelu dziennie (30 dni)</h3>
          <div className="flex h-16 items-end gap-0.5">
            {last30.map((d) => (
              <div
                key={d.date}
                title={`${d.date}: ${usd(d.cost_usd)} · ${d.llm} wywołań`}
                className="flex-1 rounded-t bg-amber-600/80 hover:bg-amber-400"
                style={{ height: `${d.cost_usd ? Math.max(4, (d.cost_usd / maxCost) * 100) : 1}%` }}
              />
            ))}
          </div>
        </div>
      </div>

      <div className="mb-3 flex flex-wrap gap-1">
        {KINDS.map((k) => (
          <button
            key={k.kind}
            onClick={() => setKind(k.kind)}
            className={`rounded-full px-3 py-1 text-xs ${
              kind === k.kind ? "bg-amber-700 text-white" : "bg-slate-800 text-slate-400 hover:bg-slate-700"
            }`}
          >
            {k.icon} {k.label}
          </button>
        ))}
      </div>

      <div className="max-h-[32rem] overflow-auto">
        <table className="w-full text-left text-xs">
          <thead className="sticky top-0 bg-slate-900 text-slate-500">
            <tr>
              <th className="py-1 pr-3 font-normal">Kiedy</th>
              <th className="py-1 pr-3 font-normal">Agent</th>
              <th className="py-1 pr-3 font-normal">Co</th>
              <th className="py-1 pr-3 text-right font-normal">Czas</th>
              <th className="py-1 text-right font-normal">Koszt</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/70">
            {events.map((e) => (
              <Fragment key={e.id}>
                <tr
                  onClick={() => setOpenTrace(openTrace === e.id ? null : e.id)}
                  className="cursor-pointer hover:bg-slate-800/40"
                >
                  <td className="whitespace-nowrap py-1.5 pr-3 text-slate-500">
                    {ICON[e.kind] ?? "•"} {time(e.at)}
                  </td>
                  <td className="py-1.5 pr-3">
                    {e.agent && (
                      <span className={`rounded px-1.5 py-0.5 ${AGENT_COLOR[e.agent.split("+")[0]] ?? "bg-slate-800"}`}>
                        {e.agent}
                      </span>
                    )}
                  </td>
                  <td className="max-w-md truncate py-1.5 pr-3 text-slate-300">
                    {e.kind === "llm"
                      ? `${e.name} · ${e.tokens_in}→${e.tokens_out} tok · ${e.model}`
                      : e.input || e.output || e.name}
                  </td>
                  <td className="whitespace-nowrap py-1.5 pr-3 text-right text-slate-500">
                    {e.ms ? `${(e.ms / 1000).toFixed(1)} s` : ""}
                  </td>
                  <td className="whitespace-nowrap py-1.5 text-right text-slate-500">
                    {e.cost_usd ? usd(e.cost_usd) : ""}
                  </td>
                </tr>
                {openTrace === e.id && (
                  <tr>
                    <td colSpan={5} className="bg-slate-950/60 px-3 py-2 text-slate-400">
                      {e.input && <Detail label="Wejście" text={e.input} />}
                      {e.output && <Detail label="Odpowiedź" text={e.output} />}
                      {e.trace && <p className="mt-1 text-slate-600">trace {e.trace}</p>}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {events.length === 0 && <p className="py-4 text-sm text-slate-500">Brak zdarzeń.</p>}
        {more && events.length > 0 && (
          <button onClick={loadMore} className="mt-3 text-xs text-amber-400 hover:underline">
            Starsze →
          </button>
        )}
      </div>
    </DashboardTile>
  );
}

function Stat({ label, value, hint, warn }: { label: string; value: string; hint?: string; warn?: boolean }) {
  return (
    <div className="rounded-lg bg-slate-950/60 px-3 py-2">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-lg font-semibold ${warn ? "text-rose-300" : ""}`}>{value}</p>
      {hint && <p className="text-xs text-slate-600">{hint}</p>}
    </div>
  );
}

function Detail({ label, text }: { label: string; text: string }) {
  return (
    <p className="whitespace-pre-wrap break-words">
      <span className="text-slate-600">{label}: </span>
      {text}
    </p>
  );
}

function heat(ratio: number, count: number): string {
  if (count === 0) return "bg-slate-800";
  if (ratio <= 0.25) return "bg-amber-900";
  if (ratio <= 0.5) return "bg-amber-700";
  if (ratio <= 0.75) return "bg-amber-500";
  return "bg-amber-300";
}
