"""Schema profiler — derive data facts instead of writing them by hand.

`DESIGN.md` pillar 2, and the only pillar with a reproduced failure behind it.
`FAILURES.md` §8's frozen statistics — measured once, pasted into a prompt, then
recited as current fact months later — is the single failure mode this project's
evaluation actually reproduces, at 90-100% on the model that was in production
when it happened. No model improvement fixes it: the model is misinformed by its
own prompt and has no way to check.

Generated facts regenerate. Pasted facts rot.

Usage::

    from fidelity.profiler import SQLiteDialect, profile_database, derive, render

    tables, joins = profile_database(run, SQLiteDialect())
    document = render(derive(tables, joins), database="warehouse")

### Verifying a dialect

A dialect is not verified by unit tests. Writing one against the spec and
recording a fake runner checks SQL shape and row parsing; it does not check
that a real server accepts the SQL. The Postgres dialect passed 7 unit tests
and then failed on its first live query.

Bring up a server and profile a real sample database:

    docker run -d --name fidelity-pg -e POSTGRES_PASSWORD=... \
        -e POSTGRES_USER=fidelity -p 5432:5432 postgres:17
    createdb dvdrental && pg_restore -d dvdrental dvdrental.tar

Then run `profile_database` against it. Expect to find things: type-specific
aggregate gaps, key-naming assumptions, and thresholds tuned on a fixture that
do not survive a real schema.

Better still, profile two ports of the *same* sample data through different
dialects — dvdrental on Postgres and Sakila on MySQL — and diff the facts. They
should agree, and where they do not, one dialect is wrong.

`run` is any callable taking SQL and returning rows. The profiler opens no
connections, reads no environment, and issues only aggregates and bounded
samples — never a full table scan of row data.
"""

from .dialects import (  # noqa: F401
    Dialect,
    MySQLDialect,
    PostgresDialect,
    SQLServerDialect,
    SQLiteDialect,
    infer_by_value_overlap,
    infer_soft_keys,
)
from .facts import derive, dominant_format, render, shape_of  # noqa: F401
from .model import (  # noqa: F401
    ColumnProfile,
    ColumnRef,
    Fact,
    ForeignKey,
    JoinProfile,
    Runner,
    TableProfile,
    TableRef,
)
from .probe import profile_database, profile_join, profile_table  # noqa: F401
