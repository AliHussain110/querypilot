# ✈️ QueryPilot

**A self-hosted, user-aware natural-language-to-SQL agent.** Ask a question in plain English;
QueryPilot writes the SQL, runs it safely with row-level security, and answers with a table and
a short summary. It remembers which questions it has answered well, and it shows the generated
SQL only to admins.

Inspired by the architecture of Vanna 2.0, but deliberately scoped as a focused, one-person
project: one LLM provider, one vector store, one UI.

<!-- Add a screenshot or GIF here: docs/screenshot.png -->

## Features

- **Natural language → SQL** with OpenAI tool calling (`run_sql`), at most 5 tool iterations per question.
- **Safe execution**: single read-only `SELECT`, validated with a SQL parser, read-only session, timeout and row cap.
- **Row-level security**: every table with an `org_id` is automatically filtered to the user's organization.
- **Agent memory** (ChromaDB): successful question → SQL pairs are saved and the most similar ones are injected into later prompts.
- **Roles**: admins can expand the generated SQL; normal users only see the answer and the table.
- **Gradio chat UI** (dark theme) with live status updates, collapsible SQL, Markdown tables and sample-question buttons.
- **SQLite out of the box** (seeded demo data, three organizations), **Postgres** by changing one URL.
- Docker, docker-compose, type hints, ruff, pytest, GitHub Actions CI.

## Architecture

```mermaid
flowchart TD
    UI["Gradio chat UI<br/>role + organization picker"] -->|question| AG["Agent<br/>OpenAI tool-calling loop (max 5)"]
    AG <-->|"messages + run_sql tool"| LLM["OpenAI"]
    AG <-->|"similar past queries / save successes"| MEM[("ChromaDB memory<br/>scoped per organization")]
    AG -->|"run_sql(sql)"| RUN["SqlRunner"]
    subgraph SAFE ["SqlRunner pipeline"]
        direction LR
        V["1. Validate<br/>single read-only SELECT"] --> R["2. RLS rewrite<br/>org_id filters"] --> X["3. Execute<br/>read-only, timeout, row cap"]
    end
    RUN --> V
    X --> DB[("SQLite / Postgres")]
    AG -->|"status, SQL (admin), table, answer"| UI
```

One question, end to end:

```mermaid
sequenceDiagram
    actor U as User
    participant UI as Gradio UI
    participant A as Agent
    participant M as Memory (Chroma)
    participant L as OpenAI
    participant R as SqlRunner
    participant D as Database

    U->>UI: "Top 5 customers by revenue?"
    UI->>A: stream(question)
    A->>M: search(question, org_id)
    M-->>A: similar question → SQL pairs
    loop at most 5 iterations
        A->>L: system prompt + history + run_sql tool
        L-->>A: tool call with SQL (or the final answer)
        A->>R: run(sql, user)
        R->>R: validate, then rewrite for RLS
        R->>D: read-only query
        D-->>R: rows
        R-->>A: result, or an error the model can fix
    end
    A->>M: save(question, sql, summary) if rows were returned
    A-->>UI: events: status, SQL (admins only), table, answer
```

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <your-repo-url> querypilot && cd querypilot
uv sync                          # installs runtime + dev dependencies
cp .env.example .env             # then set OPENAI_API_KEY
uv run python -m app.main        # http://127.0.0.1:7860
```

The first start creates and seeds `data/querypilot.db`. In the UI, pick a **role** and an
**organization** at the top, then ask a question or click an example.

Prefer the terminal? `uv run python -m app.cli --user alice` (also `bob`, `grace`, `ivan`).

### Docker

```bash
cp .env.example .env             # set OPENAI_API_KEY
docker compose up --build        # http://localhost:7860
```

The SQLite file and ChromaDB memory live in the `querypilot-data` volume. To use Postgres
instead, set `DATABASE_URL=postgresql+psycopg://querypilot:querypilot@postgres:5432/querypilot`
in `.env` and run `docker compose --profile postgres up --build`. The demo data is seeded
into an empty database on first start. To use your own Postgres, install the driver locally with
`uv sync --extra postgres` and point `DATABASE_URL` at it. Your own database must have an
`org_id` column on tenant tables for RLS to apply (see below).

## Configuration

Everything is read from environment variables or `.env` (see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | – | Required. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Chat model used for SQL generation. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embeddings for memory. |
| `DATABASE_URL` | `sqlite:///data/querypilot.db` | SQLAlchemy URL (SQLite or Postgres). |
| `MAX_RESULT_ROWS` | `200` | Hard cap on rows returned per query. |
| `QUERY_TIMEOUT_SECONDS` | `15` | Wall-clock limit per query. |
| `MAX_TOOL_ITERATIONS` | `5` | Tool-calling rounds per question. |
| `MEMORY_ENABLED` | `true` | Turn agent memory on or off. |
| `MEMORY_TOP_K` | `5` | Similar examples injected into the prompt. |
| `MEMORY_MAX_DISTANCE` | `0.55` | Cosine-distance cut-off; lower is stricter. |
| `CHROMA_PATH` | `data/chroma` | ChromaDB persistence folder. |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `7860` | Web UI binding (`0.0.0.0` in Docker). |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs the executed SQL. |

## How it works

### The agent loop

For each question the agent builds a system prompt containing the live database schema,
today's date, business definitions (for example, "revenue = completed orders"), the user's
organization, and similar past examples. It then calls OpenAI with a single tool, `run_sql`.
If the model calls it, the SQL goes through `SqlRunner` and the rows (or the error message)
go back to the model, which may fix its SQL and retry, up to 5 rounds. When the model answers
in plain text, that answer is shown next to the table. Short-term conversation history keeps
follow-ups like "and for last month?" working.

The agent yields **events** (`status`, `sql`, `result`, `answer`, `error`) instead of a final
string. The UI and the CLI both render the same stream.

### Memory

After a question succeeds with at least one row, QueryPilot stores the **question, the SQL the
model wrote, and a short summary** in ChromaDB. Embeddings come from OpenAI and are passed to
Chroma explicitly, so Chroma is only the persistent vector store.

On the next question the closest examples (cosine distance below `MEMORY_MAX_DISTANCE`) are
added to the prompt as verified patterns to reuse. Details worth knowing:

- Memory is **per organization**: examples are stored with the `org_id` and only retrieved for
  that organization, so literals from one tenant's questions never reach another's prompt.
- The stored SQL is the model's original SQL, **not** the RLS-rewritten version.
- Asking the same question again replaces the old entry, so memory improves rather than grows.
- Memory is best-effort: if Chroma fails, the question is still answered.

### Row-level security

Queries are rewritten on the parsed syntax tree (sqlglot) before they run. Every reference to a
protected table is replaced with a filtered subquery that keeps the original alias:

```sql
-- what the model wrote
SELECT c.name, SUM(o.total_amount) AS revenue
FROM customers c JOIN orders o ON o.customer_id = c.id
GROUP BY c.name

-- what runs for a user in organization 2
SELECT c.name, SUM(o.total_amount) AS revenue
FROM (SELECT * FROM customers WHERE org_id = 2) AS c
JOIN (SELECT * FROM orders WHERE org_id = 2) AS o ON o.customer_id = c.id
GROUP BY c.name
```

Filtering the table rather than appending to a `WHERE` clause keeps outer joins, CTEs,
subqueries and `UNION`s correct, because no reference can escape the filter. Protected tables
are discovered from the schema: any table with an `org_id` column. The `organizations` table is
filtered on `id`, so users can only see their own organization row. Tables that aren't part of
the application schema (`sqlite_master`, `pg_catalog`, …) and CTEs named like a protected table
are rejected. Admins can see both the model's SQL and the executed, rewritten SQL.

### Safety layers

| Layer | What it does |
|---|---|
| Static validation | Exactly one statement, which must be a query; no `INSERT`/`UPDATE`/`DELETE`/DDL anywhere in the tree; dangerous functions denied (`load_extension`, `pg_sleep`, …). |
| RLS rewrite | Every protected table is replaced by an `org_id`-filtered subquery. |
| Read-only session | SQLite `PRAGMA query_only`; Postgres `SET TRANSACTION READ ONLY`. |
| Limits | Timeout per query and a hard row cap. |
| Role redaction | The agent itself strips SQL, SQL error text and API error text from events for normal users, so no front end can leak them. |

### Roles and identity

There is no real authentication, by design. The UI lets you pick **Admin** or **User** and an
**organization**; that identity drives SQL visibility and RLS. Replace `UserContext` with real
auth when you need it; the rest of the code only depends on that object.

## Sample questions

All of these work against the demo data:

- Who are our top 5 customers by revenue?
- Monthly revenue for the last 6 months
- Which products sell the most units?
- What share of orders were cancelled or refunded?
- Average order value by customer country
- Which customers signed up in the last 90 days?
- Which product category brings in the most revenue?
- List every organization in the database *(shows only your own, which is RLS at work)*

## Demo data

Three organizations, each with its own catalog, 30 customers and 180 orders spread over the
past year. The data is generated from a fixed random seed.

| id | Organization | Plan | Sells |
|---|---|---|---|
| 1 | Acme Corp | enterprise | consumer electronics |
| 2 | Globex Industries | pro | outdoor gear |
| 3 | Initech Labs | free | B2B software and services |

`uv run python -m app.db.seed --reset` drops and reseeds.

## Project structure

```
querypilot/
├── app/
│   ├── main.py            # Gradio entry point (python -m app.main)
│   ├── ui.py              # Blocks layout, theme, ChatController
│   ├── chat_view.py       # agent events -> chat messages, Markdown tables
│   ├── cli.py             # terminal chat loop
│   ├── bootstrap.py       # start-up wiring (database, OpenAI client, memory)
│   ├── context.py         # roles and mock users
│   ├── config.py          # typed settings
│   ├── agent/
│   │   ├── agent.py       # tool-calling loop, event stream, role redaction
│   │   ├── memory.py      # ChromaDB memory
│   │   ├── prompts.py     # system prompt
│   │   └── tools.py       # run_sql tool definition and result formatting
│   └── db/
│       ├── models.py      # SQLAlchemy models, tenant-table discovery
│       ├── runner.py      # SqlRunner: validate, scope, execute
│       ├── rls.py         # row-level security rewrite
│       ├── errors.py      # error types
│       └── seed.py        # demo data
├── tests/
├── Dockerfile · docker-compose.yml · .env.example · pyproject.toml
```

## Development

```bash
uv run pytest                        # tests use fakes: no API key or network needed
uv run ruff check . && uv run ruff format --check .
```

CI (GitHub Actions) runs ruff and pytest on Python 3.11 and 3.12.

## Design decisions and limitations

- **Application-level RLS.** The rewrite is a second line of defence that works on any SQL
  database. For production Postgres, also use native row-level security and connect with a
  read-only role.
- **Generated SQL can still be wrong.** The model may misread a question; admins can inspect the
  SQL, and the schema and business definitions in the prompt are the main lever for accuracy.
- **Not token-streamed.** The UI streams progress events (thinking, executing, results) but the
  final answer arrives as one block.
- **Memory is per organization**, so a new organization starts without examples.
- **Mock identities.** Anyone can pick any role; this is a demo, not an access-control system.
- **One LLM provider and one vector store**, by design.
