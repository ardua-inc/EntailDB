"""Eval harness for output fidelity.

Built before the fidelity runner, deliberately: the extraction plan sequences the
harness second and the runner third, so the runner has a target to satisfy
rather than a story to tell. `MEASUREMENT.md` §2 is the specification.
"""
