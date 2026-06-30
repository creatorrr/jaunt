---
name: "spacy"
description: "Use when generating NLP code with spaCy — loading pipelines (nlp = spacy.load), processing Doc/Span/Token, entities, and common pipeline components."
---

# spacy

## What it is
spaCy is an NLP library built around trained pipelines and efficient token containers. Use it
when generated code needs tokenization, part-of-speech tags, dependency parses, named
entities, sentence segmentation, or custom pipeline components.

Keep pipeline loading separate from text processing. Load `nlp` once at application startup
or in a fixture, then pass it into functions that process text.

## Core concepts
- `spacy.load("en_core_web_sm")` loads a trained language pipeline.
- Calling `nlp(text)` returns a `Doc`.
- Iterating over a `Doc` yields `Token` objects with text, lemma, POS, dependency, and
  character offset attributes.
- `Doc.ents` contains named-entity `Span` objects with labels and offsets.
- Pipeline components run in order and can be disabled when only tokenization or entities are
  needed.
- `nlp.pipe(texts)` efficiently processes many texts in batches.

## Common patterns
Load and pass a pipeline:

```python
import spacy
from spacy.language import Language


def load_pipeline() -> Language:
    return spacy.load("en_core_web_sm")
```

Extract entities with offsets:

```python
def extract_entities(nlp: Language, text: str) -> list[dict[str, object]]:
    doc = nlp(text)
    return [
        {"text": ent.text, "label": ent.label_, "start": ent.start_char, "end": ent.end_char}
        for ent in doc.ents
    ]
```

Process batches with `nlp.pipe`:

```python
def tokenize_many(nlp: Language, texts: list[str]) -> list[list[str]]:
    return [[token.text for token in doc] for doc in nlp.pipe(texts, batch_size=64)]
```

Use a blank pipeline for tokenization-only code:

```python
blank_nlp = spacy.blank("en")
tokens = [token.text for token in blank_nlp("Hello, world!")]
```

## Gotchas
- Model packages such as `en_core_web_sm` are separate installs. Code should fail clearly or
  accept an injected `Language` object when a model is unavailable.
- Loading a pipeline is expensive. Do not call `spacy.load()` for every request.
- Token indices are token positions; character offsets are `start_char` and `end_char`.
- Entity labels depend on the loaded model and training data. Do not hard-code labels without
  documenting the required model.
- Disable unneeded components for speed, but remember that some outputs depend on upstream
  components.

## Testing notes
Unit tests can use `spacy.blank("en")` for deterministic tokenization without downloading a
model. For entity or parser behavior, inject a small fixture pipeline or mock the function
that returns `Doc` objects. Tests should assert token text, offsets, and labels that are
stable for the chosen pipeline, and should avoid requiring network downloads.
