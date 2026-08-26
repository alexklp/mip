#!/usr/bin/env python3
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import stanza


HERE = Path(__file__).resolve().parent
OVERRIDES_FILE = HERE / "lemma_overrides_v1.json"

SUPPORTED_LANGS = {"uk", "ru"}


@lru_cache(maxsize=1)
def load_lemma_overrides() -> dict[str, list[dict]]:
    return json.loads(
        OVERRIDES_FILE.read_text(encoding="utf-8")
    )


def apply_lemma_override(
    lang: str,
    surface: str,
    lemma: str,
) -> str:
    surface = surface.lower()
    lemma = lemma.lower()

    rules = load_lemma_overrides().get(lang, [])

    for rule in rules:
        if lemma != rule["lemma"]:
            continue

        prefix = rule.get("surface_prefix")
        if prefix and not surface.startswith(prefix):
            continue

        return rule["replace"]

    return lemma


@lru_cache(maxsize=2)
def get_pipeline(lang: str):
    if lang not in SUPPORTED_LANGS:
        raise ValueError(f"Unsupported language: {lang}")

    processors = (
        "tokenize,mwt,pos,lemma"
        if lang == "uk"
        else "tokenize,pos,lemma"
    )

    return stanza.Pipeline(
        lang,
        processors=processors,
        use_gpu=False,
        download_method=None,
        verbose=False,
    )


def lemmatize_words(
    text: str,
    lang: str,
) -> list[tuple[str, str, str]]:
    doc = get_pipeline(lang)(text)

    result = []

    for sentence in doc.sentences:
        for word in sentence.words:
            surface = (word.text or "").lower()
            raw_lemma = (word.lemma or surface).lower()

            lemma = apply_lemma_override(
                lang,
                surface,
                raw_lemma,
            )

            result.append(
                (
                    surface,
                    lemma,
                    word.upos or "",
                )
            )

    return result


@lru_cache(maxsize=1)
def get_langid_pipeline():
    return stanza.Pipeline(
        lang="multilingual",
        processors="langid",
        langid_lang_subset=["uk", "ru", "en"],
        use_gpu=False,
        download_method=None,
        verbose=False,
    )


def detect_language(text: str) -> str:
    from stanza.models.common.doc import Document

    sample = (text or "").strip()[:4000]
    if not sample:
        return "unknown"

    doc = Document([], text=sample)
    result = get_langid_pipeline()(doc)

    return result.lang or "unknown"


def normalized_tokens(
    text: str,
    lang: str,
    stopwords: set[str] | None = None,
) -> list[str]:
    if lang not in SUPPORTED_LANGS:
        return []

    stopwords = stopwords or set()
    result = []

    for surface, lemma, pos in lemmatize_words(text, lang):
        if pos == "PUNCT":
            continue

        # Preserve known surface stopwords before replacing the token
        # with its lemma. This avoids turning e.g. UA "має/мають"
        # into the content-looking lemma "мати".
        if surface in stopwords:
            result.append(surface)
        else:
            result.append(lemma)

    return result
