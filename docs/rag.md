# Knowledge / RAG Architecture

## Pipeline (runs in `jobs`)
Upload/URL → store original (object storage) → parse (PDF, DOCX, TXT, CSV, HTML) → clean (boilerplate, whitespace, injection-pattern flagging) → chunk (~300-500 tokens, heading-aware, overlap) → embed (EmbeddingProvider) → store `knowledge_chunks` (pgvector HNSW) → status `indexed` → event.

Structured facts (hours, address, prices, menu items) are stored as **structured business data**, not only chunks, and served by deterministic tools (`get_hours`, `get_menu`) so they are never paraphrased from fuzzy retrieval.

## Retrieval at call time
- Knowledge vs action routing: questions → retrieval; requests to do something → tools/workflows.
- Hybrid search (vector + Postgres full-text), tenant + knowledge-base filtered (RLS as backstop), top-k 3-5, score threshold.
- Latency budget ≤ 150 ms; per-call cache of results.
- Results injected as `<data source="kb:doc_id">` blocks marked untrusted.
- Below threshold → the agent says it doesn't have that information and offers a human. Never invent business facts.

## Quality
Knowledge test console (ask a question, see retrieved chunks and answer) and per-template eval questions. Re-index on embedding model change (embedding model id stored per chunk).
