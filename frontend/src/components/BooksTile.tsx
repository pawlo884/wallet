import { useState, type FormEvent } from "react";
import { api, type Book, type BookStatus, type ReadingStats } from "../api";
import { DashboardTile } from "./DashboardTile";

interface Props {
  books: Book[];
  reading: ReadingStats | null;
  onChange: () => Promise<void>;
  onError: (msg: string) => void;
}

const COLUMNS: {
  status: BookStatus;
  label: string;
  icon: string;
  color: string;
}[] = [
  { status: "reading", label: "Czytam", icon: "📖", color: "text-violet-300" },
  {
    status: "to_read",
    label: "Do przeczytania",
    icon: "📚",
    color: "text-sky-300",
  },
  {
    status: "to_buy",
    label: "Do kupienia",
    icon: "🛒",
    color: "text-amber-300",
  },
  {
    status: "read",
    label: "Przeczytane",
    icon: "✅",
    color: "text-emerald-300",
  },
];

const NEXT: Partial<Record<BookStatus, { status: BookStatus; label: string }>> =
  {
    to_buy: { status: "to_read", label: "Kupiona" },
    to_read: { status: "reading", label: "Zaczynam" },
    reading: { status: "read", label: "Przeczytana" },
  };

export function BooksTile({ books, reading, onChange, onError }: Props) {
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
  const [status, setStatus] = useState<BookStatus>("to_buy");
  const [showRead, setShowRead] = useState(false);

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
      const b = await api.addBook(title.trim(), author.trim() || null, status);
      if (!b.created) onError(`„${b.title}” już jest na liście.`);
      setTitle("");
      setAuthor("");
    });
  };

  return (
    <DashboardTile
      title="Książki"
      subtitle={`Na liście: ${books.length} · screeny wysyłaj botowi na Telegramie`}
      accent="violet"
      span="wide"
    >
      {reading && <ReadingSummary stats={reading} />}

      <form onSubmit={add} className="mb-5 flex flex-col gap-2 sm:flex-row">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Tytuł"
          className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-violet-600"
        />
        <input
          value={author}
          onChange={(e) => setAuthor(e.target.value)}
          placeholder="Autor (opcjonalnie)"
          className="min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-violet-600 sm:w-44"
        />
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value as BookStatus)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-300"
        >
          {COLUMNS.map((c) => (
            <option key={c.status} value={c.status}>
              {c.label}
            </option>
          ))}
        </select>
        <button className="rounded-lg bg-violet-700 px-4 py-2 text-sm font-medium hover:bg-violet-600">
          Dodaj
        </button>
      </form>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        {COLUMNS.filter((c) => c.status !== "read").map((c) => {
          const items = books.filter((b) => b.status === c.status);
          return (
            <div key={c.status}>
              <h3 className={`mb-2 text-xs font-semibold uppercase ${c.color}`}>
                {c.icon} {c.label} ({items.length})
              </h3>
              {items.length === 0 && (
                <p className="text-xs text-slate-600">—</p>
              )}
              <ul className="space-y-2">
                {items.map((b) => (
                  <BookCard key={b.id} book={b} run={run} />
                ))}
              </ul>
            </div>
          );
        })}
      </div>

      {books.some((b) => b.status === "read") && (
        <div className="mt-5">
          <button
            onClick={() => setShowRead(!showRead)}
            className="text-xs text-slate-500 hover:text-slate-300"
          >
            {showRead ? "▾" : "▸"} ✅ Przeczytane (
            {books.filter((b) => b.status === "read").length})
          </button>
          {showRead && (
            <ul className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
              {books
                .filter((b) => b.status === "read")
                .map((b) => (
                  <BookCard key={b.id} book={b} run={run} />
                ))}
            </ul>
          )}
        </div>
      )}
    </DashboardTile>
  );
}

function BookCard({
  book,
  run,
}: {
  book: Book;
  run: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const next = NEXT[book.status];
  return (
    <li className="group flex gap-3 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2">
      <Cover book={book} />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium leading-snug">{book.title}</p>
        {book.author && <p className="text-xs text-slate-500">{book.author}</p>}
        {book.status === "reading" && <Progress book={book} run={run} />}
        <div className="mt-2 flex flex-wrap items-center gap-1 text-xs">
          {next && (
            <button
              onClick={() => run(() => api.setBookStatus(book.id, next.status))}
              className="rounded bg-slate-800 px-2 py-1 text-slate-300 hover:bg-slate-700"
            >
              {next.label} →
            </button>
          )}
          <select
            value={book.status}
            onChange={(e) =>
              run(() =>
                api.setBookStatus(book.id, e.target.value as BookStatus),
              )
            }
            className="rounded bg-transparent px-1 py-1 text-slate-500 hover:text-slate-300"
            title="Zmień status"
          >
            {COLUMNS.map((c) => (
              <option key={c.status} value={c.status}>
                {c.label}
              </option>
            ))}
          </select>
          <button
            onClick={() =>
              confirm(`Usunąć „${book.title}”?`) &&
              run(() => api.deleteBook(book.id))
            }
            className="ml-auto rounded px-2 py-1 text-slate-600 hover:bg-rose-950 hover:text-rose-300 sm:opacity-0 sm:group-hover:opacity-100"
          >
            Usuń
          </button>
        </div>
      </div>
    </li>
  );
}

function Cover({ book }: { book: Book }) {
  const [broken, setBroken] = useState(false);
  if (book.cover_url && !broken) {
    return (
      <img
        src={book.cover_url}
        alt=""
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
        className="h-20 w-14 shrink-0 rounded object-cover shadow"
      />
    );
  }
  // brak okładki: inicjały na kolorowym tle (kolor stały dla tytułu)
  const hue = [...book.title].reduce(
    (h, c) => (h * 31 + c.charCodeAt(0)) % 360,
    7,
  );
  return (
    <div
      className="flex h-20 w-14 shrink-0 items-center justify-center rounded p-1 text-center text-[10px] font-semibold leading-tight text-white/80"
      style={{ background: `hsl(${hue} 35% 28%)` }}
      title={book.cover_url === null ? "Szukam okładki…" : "Brak okładki"}
    >
      {book.title.slice(0, 24)}
    </div>
  );
}

function ReadingSummary({ stats }: { stats: ReadingStats }) {
  const max = Math.max(1, ...stats.days.map(([, n]) => n));
  const items = [
    ["Dziś", stats.today],
    ["Ten tydzień", stats.week],
    ["Ten miesiąc", stats.month],
    ["Ten rok", stats.year],
  ] as const;
  return (
    <div className="mb-5 flex flex-col gap-4 rounded-lg border border-slate-800 bg-slate-950/60 p-3 sm:flex-row sm:items-end">
      <dl className="grid flex-1 grid-cols-4 gap-2">
        {items.map(([label, n]) => (
          <div key={label}>
            <dt className="text-[11px] text-slate-500">{label}</dt>
            <dd className="text-lg font-semibold tabular-nums text-violet-200">
              {n}
              <span className="ml-1 text-xs font-normal text-slate-500">str.</span>
            </dd>
          </div>
        ))}
      </dl>
      <div
        className="flex h-12 items-end gap-1 sm:w-56"
        title="Przeczytane strony, ostatnie 14 dni"
      >
        {stats.days.map(([d, n]) => (
          <div
            key={d}
            title={`${d.slice(8, 10)}.${d.slice(5, 7)}: ${n} str.`}
            className={`flex-1 rounded-sm ${n > 0 ? "bg-violet-500/80" : "bg-slate-800"}`}
            style={{ height: `${Math.max(8, (100 * n) / max)}%` }}
          />
        ))}
      </div>
    </div>
  );
}

function Progress({
  book,
  run,
}: {
  book: Book;
  run: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [page, setPage] = useState("");
  const pct = book.pages ? Math.min(100, Math.round((100 * book.page) / book.pages)) : null;
  const save = (e: FormEvent) => {
    e.preventDefault();
    const n = parseInt(page, 10);
    if (Number.isNaN(n)) return;
    run(() => api.setBookPage(book.id, n, null)).then(() => setPage(""));
  };
  return (
    <div className="mt-2">
      {pct !== null && (
        <div className="h-1.5 overflow-hidden rounded bg-slate-800">
          <div className="h-full rounded bg-violet-500" style={{ width: `${pct}%` }} />
        </div>
      )}
      <form onSubmit={save} className="mt-1 flex items-center gap-2 text-xs text-slate-400">
        <span className="whitespace-nowrap tabular-nums">
          str. {book.page}
          {book.pages ? ` / ${book.pages} · ${pct}%` : ""}
        </span>
        <input
          value={page}
          onChange={(e) => setPage(e.target.value)}
          inputMode="numeric"
          placeholder="strona"
          aria-label="Strona, na której jesteś"
          className="w-14 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 outline-none focus:border-violet-600"
        />
        {!book.pages && (
          <button
            type="button"
            onClick={() => {
              const n = parseInt(prompt("Ile stron ma książka?") || "", 10);
              if (n > 0) run(() => api.setBookPage(book.id, null, n));
            }}
            className="text-slate-500 hover:text-slate-300"
          >
            ile stron?
          </button>
        )}
      </form>
    </div>
  );
}
