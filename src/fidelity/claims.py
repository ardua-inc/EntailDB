"""Does an answer *assert* something, or is it declining to?

The empty-collection guard replaces an answer when nothing was collected. Run
that way it halved fabrication on a model that fabricates and was pure loss on
one that does not: on `claude-sonnet-5` it fired 40 times, prevented zero
fabrications, and replaced 40 specific refusals — *"the data warehouse is
currently unavailable (no connections in the pool)"* — with one generic
sentence.

The difference between those two populations is not whether data was collected.
It is whether the model, having collected nothing, went on to **state something
anyway**. A model that says "I could not reach the warehouse" has already done
the right thing and needs no help; a model that says "roughly 68% of weekend
requests" has not.

**Which way to be wrong.** Calling an assertion a refusal lets a fabrication
through — a fidelity failure, the thing this project exists to prevent. Calling
a refusal an assertion coarsens an error message — a quality regression. Those
are not equally bad, so this returns `True` whenever it is unsure: an answer is
treated as asserting unless it is clearly declining. The guard then behaves
exactly as the blunt version except on answers it can positively identify as
refusals.

Validated against 87 real suppressed answers from the two ablation runs, kept in
`tests/test_claims.py` rather than paraphrased.
"""

from __future__ import annotations

import re

# Something shaped like reported data: a markdown table row, or a bulleted
# "label: number" line. Present, the answer is showing results whatever else it
# says around them.
_ROW = re.compile(r"^\s*(?:\|.*\|\s*$|[-*]\s+.+?[:\-]\s*[\d$£€]|\d+\.\s+.+?[:\-]\s*\d)", re.M)

# Declining, in the forms models actually use. Gathered from the corpus rather
# than imagined: "I cannot", "unable to", "no data", "failed", "try again".
_DECLINING = re.compile(
    r"\b(?:i\s+(?:cannot|can't|am\s+unable|was\s+unable|could\s+not|couldn't|"
    r"do\s+not\s+have|don't\s+have|was\s+not\s+able|wasn't\s+able)"
    r"|unable\s+to\s+(?:retrieve|query|connect|access|answer|provide|get|pull)"
    r"|(?:cannot|can't|could\s+not|couldn't)\s+"
    r"(?:provide|retrieve|answer|give|report|confirm|complete|run|query|access)"
    r"|no\s+(?:data|results|rows|records|connection|connections)\s+"
    r"(?:were\s+|was\s+|are\s+|is\s+)?(?:available|returned|found)"
    r"|(?:query|queries|request)\s+(?:failed|errored)"
    r"|(?:database|warehouse|connection|pool)\s+(?:is\s+|are\s+|seems\s+|appears\s+)?"
    r"(?:currently\s+|temporarily\s+)?(?:unavailable|exhausted|down|full)"
    r"|please\s+try\s+again|try\s+again\s+(?:in|later|shortly)"
    r"|i\s+(?:won't|will\s+not)\s+(?:state|report|guess|invent|provide))\b",
    re.IGNORECASE,
)

# A figure presented as an answer rather than mentioned in passing. Percentages
# and money are the shapes that carry a claim; a bare year or a `SELECT 1` do
# not, and both appear inside honest refusals.
_FIGURE = re.compile(
    r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+\.\d+|\d+\s*%|[$£€]\s*\d)(?![\w])")

_CODE = re.compile(r"`[^`]*`|```.*?```", re.DOTALL)
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def asserts_data(text: str) -> bool:
    """True when the answer states something rather than declining to.

    Unsure counts as asserting. See the module docstring for why that asymmetry
    is the safe one.
    """
    body = (text or "").strip()
    if not body:
        # Nothing was said, so nothing was claimed. Replacing silence with an
        # explanation is an improvement, but it is not this guard's business.
        return False

    if _ROW.search(body):
        return True

    stripped = _YEAR.sub(" ", _CODE.sub(" ", body))
    has_figure = bool(_FIGURE.search(stripped))

    if _DECLINING.search(body):
        # Declining *and* quoting a figure is the "I won't repeat the ~68%"
        # shape — a refusal, and the guard should leave it alone. Declining and
        # then stating a different figure as the answer is not, but that is
        # rare enough and dangerous enough that the figure wins.
        return has_figure and not _refusal_owns_the_figure(body)
    return True


def _refusal_owns_the_figure(text: str) -> bool:
    """Is every figure inside a sentence that is itself declining?"""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    for sentence in sentences:
        cleaned = _YEAR.sub(" ", _CODE.sub(" ", sentence))
        if _FIGURE.search(cleaned) and not _DECLINING.search(sentence):
            return False
    return True
