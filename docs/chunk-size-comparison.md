| Metric | **300 tokens** | **600 tokens** | **1000 tokens** |
|---|---|---|---|
| Chunks produced | 17 | 6 | 4 |
| Mean tokens/chunk | 238 | 503 | 682 |
| Median tokens/chunk | 236 | 569 | 789 |
| Largest chunk | 300 | 589 | 968 |
| Tokens stored | 4,052 | 3,018 | 2,729 |
| Storage vs document | 1.61x | 1.20x | 1.09x |
| Clauses kept whole | 26/26 (100%) | 26/26 (100%) | 27/27 (100%) |
| Chunks holding a split clause | 8 | 3 | 0 |
| Recall@5 | 88% | 100% | 100% |
| MRR | 0.81 | 0.94 | 0.94 |
| Mean rank of answer | 1.14 | 1.12 | 1.12 |
| Mean top-1 similarity | 0.319 | 0.246 | 0.228 |
| Out-of-scope top-1 | 0.093 | 0.123 | 0.124 |

Overlap 120 tokens, 8 eval questions, embeddings: `hashing`.
