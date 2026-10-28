# `sql/` — validation queries

`validation_queries.sql` lets a reviewer check the acceptance criteria without reading Python.
`iris-harvest validate` runs them and exits non-zero on any violation; `make demo` runs it at the end.

Queries 1, 2, 4 and 10 are defence in depth — the schema constraints already guarantee them.
Queries 11, 12 and 15 are real data-quality checks that can genuinely fail. Query 3 is a coarse
bounding check, labelled as such.

Queries 6 and 6b together are the idempotency proof: nothing was written by the latest run, while
liveness tracking still advanced.
