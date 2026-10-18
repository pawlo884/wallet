import { money, type Overview } from "../api";
import { DashboardTile } from "./DashboardTile";

export function MoneyTile({ overview }: { overview: Overview | null }) {
  const m = overview?.month;
  return (
    <DashboardTile title="Wydatki" subtitle={m ? `${m.month} · dzień ${m.day}` : "Bieżący miesiąc z Wallet"} accent="emerald">
      {!m && <p className="text-sm text-slate-500">{overview ? "Wallet chwilowo niedostępny." : "Wczytuję…"}</p>}
      {m && (
        <>
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="text-xs text-slate-500">Przychody</dt>
              <dd className="font-semibold text-emerald-300">{money(m.income, m.currency)}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Wydatki</dt>
              <dd className="font-semibold text-rose-300">{money(m.expense, m.currency)}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Bilans</dt>
              <dd className="font-semibold">{money(m.income - m.expense, m.currency)}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Średnio dziennie</dt>
              <dd className="font-semibold">{money(m.expense / m.day, m.currency)}</dd>
            </div>
          </dl>
          {m.categories.length > 0 && (
            <ul className="mt-4 space-y-1.5">
              {m.categories.slice(0, 6).map((c) => (
                <li key={c.name} className="text-xs">
                  <div className="flex justify-between text-slate-400">
                    <span className="truncate">{c.name}</span>
                    <span>{money(c.value, m.currency)}</span>
                  </div>
                  <div className="mt-0.5 h-1.5 rounded bg-slate-800">
                    <div
                      className="h-1.5 rounded bg-emerald-600"
                      style={{ width: `${Math.max(2, (c.value / m.expense) * 100)}%` }}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      <a href={overview?.forecast_url || "/"} className="mt-4 inline-block text-xs text-emerald-400 hover:underline">
        Prognoza na 12 miesięcy →
      </a>
    </DashboardTile>
  );
}
