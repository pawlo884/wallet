// Klient API bota (bot/panel_api.py). Panel i API są pod tym samym adresem, więc ścieżki względne.
const BASE = "/api/v1";

export type BookStatus = "to_buy" | "to_read" | "reading" | "read";

export interface Task {
  id: number;
  title: string;
  due: string | null;
  status: "open" | "done";
  moves: number;
  created_at: number;
  done_at: number | null;
  due_label: string;
  overdue: boolean;
}

export interface Book {
  id: number;
  title: string;
  author: string | null;
  status: BookStatus;
  note: string | null;
  created_at: number;
  status_at: number;
}

export interface EventRow {
  id: number;
  at: number;
  trace: string | null;
  kind: string;
  agent: string | null;
  name: string | null;
  input: string | null;
  output: string | null;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  ms: number | null;
}

export interface DayStat {
  date: string;
  actions: number;
  llm: number;
  cost_usd: number;
}

export interface Overview {
  today: string;
  tasks: { open: number; overdue: number; today: number; done_today: number };
  books: Record<BookStatus, number>;
  cost: { today_usd: number; month_usd: number; month_calls: number; budget_usd: number | null };
  month: {
    month: string;
    day: number;
    currency: string;
    income: number;
    expense: number;
    categories: { name: string; value: number }[];
  } | null;
  forecast_url: string | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (data.error) msg = data.error;
    } catch {
      /* odpowiedź bez JSON */
    }
    throw new Error(msg);
  }
  return res.json();
}

const send = <T>(method: string, path: string, body?: unknown) =>
  request<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  overview: () => request<Overview>("/overview"),
  tasks: () => request<{ open: Task[]; done: Task[] }>("/tasks"),
  addTask: (title: string, due: string | null) => send<Task>("POST", "/tasks", { title, due }),
  updateTask: (id: number, patch: Partial<Pick<Task, "title" | "due" | "status">>) =>
    send<Task>("PATCH", `/tasks/${id}`, patch),
  deleteTask: (id: number) => send<{ ok: true }>("DELETE", `/tasks/${id}`),
  books: () => request<{ books: Book[]; statuses: Record<BookStatus, string> }>("/books"),
  addBook: (title: string, author: string | null, status: BookStatus) =>
    send<Book & { created: boolean }>("POST", "/books", { title, author, status }),
  setBookStatus: (id: number, status: BookStatus) => send<Book>("PATCH", `/books/${id}`, { status }),
  deleteBook: (id: number) => send<{ ok: true }>("DELETE", `/books/${id}`),
  events: (kind?: string, before?: number) => {
    const q = new URLSearchParams({ limit: "50" });
    if (kind) q.set("kind", kind);
    if (before) q.set("before", String(before));
    return request<{ events: EventRow[] }>(`/events?${q}`);
  },
  daily: (days = 90) => request<{ days: DayStat[] }>(`/stats/daily?days=${days}`),
};

export const money = (v: number, cur = "PLN") =>
  `${v.toLocaleString("pl-PL", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${cur}`;

export const usd = (v: number) => `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`;

export function isoDate(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
