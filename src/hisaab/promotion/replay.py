"""Replay validation (tech-design §4.10 step 3): run a candidate spec over its examples and compare every
field with the model's cached reading. Pure: no database.

The spec is checked first (§4.5: patterns compile under RE2 and stay within length bounds); a spec with
problems is refused before any replay. Then each example must be selected by the spec (its sender and
subject pattern) and every field, `outcome` included, must equal the cached value exactly (time to the
minute: the model reads HH:MM, the time: transform yields HH:MM:SS). One mismatch anywhere fails the
candidate.

Replay proves consistency, not correctness: a template that faithfully reproduces a misreading passes.
That is why the result keeps every value, expected and got, for the admin to read beside the raw email.
"""

from dataclasses import dataclass
from typing import Any

from hisaab.parsing.email_interpreter import EmailArtifact, EmailTemplate, Interpretation, interpret, trace
from hisaab.parsing.email_spec import CONTRACT_FIELDS, EmailSpec, check_email_spec, same_value
from hisaab.parsing.templates import select_email_candidates

COMPARED: tuple[str, ...] = ("outcome", *CONTRACT_FIELDS)


@dataclass(frozen=True)
class Example:
    source_id: str
    artifact: EmailArtifact
    expected: dict[str, Any]  # the cached §10.4 reading


def replay(spec: dict[str, Any], examples: list[Example]) -> dict[str, Any]:
    parsed, problems = check_email_spec(spec)
    if problems:
        return {"passed": False, "problems": problems, "examples": []}
    candidate: EmailTemplate = EmailTemplate("candidate", "candidate", parsed)
    rows: list[dict[str, Any]] = []
    for n, example in enumerate(examples, start=1):
        if not select_email_candidates([candidate], example.artifact.sender, example.artifact.subject):
            problems.append(f"email {n}: the sender or subject pattern doesn't match")
        result: Interpretation = interpret([candidate], example.artifact)
        got: dict[str, Any] = result.extraction if result.extraction is not None else _read(parsed, example.artifact)
        fields: list[dict[str, Any]] = [{"field": name, "expected": example.expected.get(name), "got": got.get(name),
                                         "match": same_value(name, example.expected.get(name), got.get(name))}
                                        for name in COMPARED]
        problems += [f"email {n}: {f['field']} differs" for f in fields if not f["match"]]
        rows.append({"source_id": example.source_id, "matched": result.extraction is not None, "fields": fields})
    return {"passed": not problems, "problems": problems, "examples": rows}


def _read(spec: EmailSpec, artifact: EmailArtifact) -> dict[str, Any]:
    """What each field read when the template as a whole declined: shown so the admin sees which part broke."""
    _, results = trace(spec, artifact)
    return {"outcome": None, **{r.name: r.value for r in results}}
