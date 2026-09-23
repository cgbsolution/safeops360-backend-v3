"""Form & Workflow Engine services (Part A).

    schema.py      the field-type contract + the publish gate
    formula.py     safe deterministic expression evaluator (no eval, no LLM)
    validation.py  record validation against a pinned definition version
    numbering.py   reference numbering (MAX+1, soft-delete safe)
    binding.py     where a form's data physically lives
    records.py     draft → submit → approval handoff to the platform engine
"""

from app.services.form_engine.binding import BindingError
from app.services.form_engine.binding import resolve as resolve_binding
from app.services.form_engine.numbering import PatternError, next_reference
from app.services.form_engine.schema import SchemaError, validate_definition
from app.services.form_engine.validation import ValidationError
from app.services.form_engine.validation import evaluate as evaluate_record

__all__ = [
    "BindingError",
    "PatternError",
    "SchemaError",
    "ValidationError",
    "evaluate_record",
    "next_reference",
    "resolve_binding",
    "validate_definition",
]
