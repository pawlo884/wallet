# Polityka prywatności — asystent (bot Wallet)

Ostatnia zmiana: 18.10.2026

Asystent to prywatny bot jednej osoby (właściciela tego repozytorium), uruchomiony na jego własnym serwerze.
Nie ma innych użytkowników i nie jest udostępniany publicznie.

**Jakie dane z Google:** dostęp do Kalendarza Google (zakres `https://www.googleapis.com/auth/calendar`).
Bot zakłada kalendarz „Zadania” i zapisuje w nim zadania z terminem oraz odczytuje wydarzenia z widocznych
kalendarzy, żeby pokazać plan dnia i ostrzec o kolizjach.

**Gdzie trafiają:** tokeny dostępu i lista zadań są zapisane tylko na serwerze właściciela (plik
`data/google.json` i baza `data/assistant.db`). Wydarzenia z kalendarza są trzymane w pamięci przez kilka minut
i nie są zapisywane. Dane nie są sprzedawane, udostępniane ani używane do reklam.

**Model językowy:** treść wiadomości do bota (w tym tytuły zadań) jest wysyłana do API Anthropic (Claude),
żeby rozpoznać, o co chodzi. Wydarzenia z kalendarza nie są tam wysyłane.

**Usunięcie dostępu:** w każdej chwili na https://myaccount.google.com/permissions albo przez usunięcie
pliku `data/google.json` na serwerze.

Kontakt: przez GitHub, https://github.com/pawlo884.
