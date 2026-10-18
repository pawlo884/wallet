import type { ReactNode } from "react";

interface DashboardTileProps {
  title: string;
  subtitle?: string;
  accent?: "violet" | "emerald" | "amber" | "sky" | "slate";
  span?: "normal" | "wide" | "tall" | "full";
  comingSoon?: boolean;
  children: ReactNode;
}

const accentBorder: Record<string, string> = {
  violet: "border-violet-800/40",
  emerald: "border-emerald-800/40",
  amber: "border-amber-800/40",
  sky: "border-sky-800/40",
  slate: "border-slate-800",
};

const accentLabel: Record<string, string> = {
  violet: "text-violet-400",
  emerald: "text-emerald-400",
  amber: "text-amber-400",
  sky: "text-sky-400",
  slate: "text-slate-400",
};

const spanClass: Record<string, string> = {
  normal: "",
  wide: "lg:col-span-2",
  tall: "lg:row-span-2",
  full: "lg:col-span-3",
};

export function DashboardTile({
  title,
  subtitle,
  accent = "slate",
  span = "normal",
  comingSoon = false,
  children,
}: DashboardTileProps) {
  return (
    <section
      className={`flex flex-col rounded-xl border bg-slate-900/50 ${accentBorder[accent]} ${spanClass[span]} ${
        comingSoon ? "opacity-60" : ""
      }`}
    >
      <header className="border-b border-slate-800/80 px-5 py-4">
        <div className="flex items-center justify-between gap-2">
          <div>
            <h2 className={`text-sm font-semibold uppercase tracking-wide ${accentLabel[accent]}`}>
              {title}
            </h2>
            {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
          </div>
          {comingSoon && (
            <span className="rounded-full bg-slate-800 px-2 py-0.5 text-xs text-slate-500">
              Wkrótce
            </span>
          )}
        </div>
      </header>
      <div className="flex-1 p-5">{children}</div>
    </section>
  );
}
