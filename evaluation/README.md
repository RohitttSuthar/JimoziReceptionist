# Retrieval/answer evaluation

Store a versioned, reviewed dataset here before tuning thresholds. Each case should contain:

- `query`: natural-language user question
- `mode`: `consumer` or `chef`
- `expected_answer_facts`: facts that must be present, not a brittle exact sentence
- `expected_source_ids`: approved source documents
- `should_abstain`: true when no approved source supports the answer
- `language`: Hindi, Romanized Hindi, English, or mixed

Track retrieval Recall@5, grounded answer correctness, unsupported-answer rate, abstention precision/recall, and p50/p95 latency. Do not treat the existing unit tests as an accuracy benchmark.
