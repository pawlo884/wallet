// Klient API bota (bot/panel_api.py). Panel i API są pod tym samym adresem, więc ścieżki względne.
const BASE = "/api/v1";

export type BookStatus = "to_buy" | "to_read" | "reading" | "read";

export interface Task {
  id: number;
  title: string;
  due: string | null;
  due_time: string | null; // HH:MM; null = cały dzień
  gcal_id: string | null; // wydarzenie w Kalendarzu Google
  status: "open" | "done";
  moves: number;
  created_at: number;
  done_at: number | null;
  due_label: string;
  overdue: boolean;
}

// Wydarzenie z Twoich kalendarzy Google (bez kalendarza „Zadania”)
export interface CalEvent {
  title: string;
  calendar: string;
  all_day: boolean;
  start: string; // YYYY-MM-DD (cały dzień) albo YYYY-MM-DDTHH:MM
  end: string;
}

export interface TasksData {
  open: Task[];
  done: Task[];
  google: boolean;
  calendar: CalEvent[];
}

export interface Book {
  id: number;
  title: string;
  author: string | null;
  status: BookStatus;
  note: string | null;
  cover_url: string | null; // null = jeszcze szukamy, "" = brak okładki
  pages: number | null; // liczba stron; 0/null = nie wiemy
  page: number; // strona, na której jestem
  created_at: number;
  status_at: number;
}

export interface ReadingStats {
  today: number;
  week: number;
  month: number;
  year: number;
  last7: number;
  days: [string, number][]; // ostatnie 14 dni: [YYYY-MM-DD, strony]
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
  tasks: () => request<TasksData>("/tasks"),
  addTask: (title: string, due: string | null, due_time: string | null) =>
    send<Task>("POST", "/tasks", { title, due, due_time }),
  updateTask: (id: number, patch: Partial<Pick<Task, "title" | "due" | "due_time" | "status">>) =>
    send<Task>("PATCH", `/tasks/${id}`, patch),
  deleteTask: (id: number) => send<{ ok: true }>("DELETE", `/tasks/${id}`),
  books: () =>
    request<{ books: Book[]; statuses: Record<BookStatus, string>; reading: ReadingStats }>("/books"),
  addBook: (title: string, author: string | null, status: BookStatus) =>
    send<Book & { created: boolean }>("POST", "/books", { title, author, status }),
  setBookStatus: (id: number, status: BookStatus) => send<Book>("PATCH", `/books/${id}`, { status }),
  setBookPage: (id: number, page: number | null, pages: number | null) =>
    send<Book>("PATCH", `/books/${id}`, { page, pages }),
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
