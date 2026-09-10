"""Контракт bounded recall: джерела, occurrences, SQL-пари та coverage."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Protocol


ROUTING_DECISIONS = ("analyze", "maybe", "skip", "missing")


SPACES = ("ru_space", "ua_space")


def require_utc(value: datetime) -> datetime:
    """Відхиляємо неявний часовий пояс і ненульове зміщення UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Потрібен timezone-aware datetime у UTC")
    return value


def windows(as_of: datetime) -> dict:
    require_utc(as_of)
    return {
        "previous": (as_of - timedelta(hours=48), as_of - timedelta(hours=24)),
        "current": (as_of - timedelta(hours=24), as_of),
    }


def validate_routing_coverage(routing, coverage):
    """Routing рахує distinct content окремо в кожному вікні та просторі."""
    if not isinstance(routing, dict) or set(routing) != {'current', 'previous'}:
        raise ValueError("Некоректні routing windows")
    for window in routing:
        if not isinstance(routing[window], dict) or set(routing[window]) != set(SPACES):
            raise ValueError("Некоректні routing groups")
        for group, counts in routing[window].items():
            if not isinstance(counts, dict) or set(counts) != set(ROUTING_DECISIONS):
                raise ValueError("Некоректні routing decisions")
            if any(type(n) is not int or n < 0 for n in counts.values()):
                raise ValueError("Некоректні routing counts")
            if sum(counts.values()) != coverage[window][group]['content_count']:
                raise ValueError("Неузгоджені суми routing coverage")


@dataclass(frozen=True)
class Source:
    source_id: str
    source_group: str
    source_name: str = ""
    source_type: str = ""


@dataclass(frozen=True)
class Content:
    content_id: str
    text: str
    embedding: tuple[float, ...] | None = None
    title: str = ""
    has_embedding: bool = False
    selection_key: str = ""
    routing_decision: str | None = None


@dataclass(frozen=True)
class Occurrence:
    occurrence_id: str
    content_id: str
    source_id: str
    collected_at: datetime
    published_at: datetime | None = None
    external_ref: str = ""


@dataclass(frozen=True)
class SignalData:
    sources: tuple[Source, ...]
    contents: tuple[Content, ...]
    occurrences: tuple[Occurrence, ...]
    embedding_model: str
    dimension: int
    truncated: bool = False
    pairs: tuple[tuple[str, str, float], ...] | None = None
    coverage: dict | None = None
    selection: dict | None = None
    critical_incomplete: bool = False
    routing_coverage: dict | None = None

    def validate(self) -> None:
        if not isinstance(self.embedding_model, str) or not self.embedding_model.strip() or isinstance(self.dimension, bool) or not isinstance(self.dimension, int) or self.dimension <= 0:
            raise ValueError("Потрібні модель та додатна розмірність embeddings")
        for rows, key in ((self.sources, "source_id"), (self.contents, "content_id"), (self.occurrences, "occurrence_id")):
            ids = [getattr(row, key) for row in rows]
            if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
                raise ValueError("Ідентифікатори мають бути непорожніми й унікальними")
        for row in self.sources:
            if not all(isinstance(value, str) for value in (row.source_group, row.source_name, row.source_type)):
                raise ValueError("Некоректні metadata джерела")
        for row in self.contents:
            if row.routing_decision not in (None, "analyze", "maybe", "skip"):
                raise ValueError("Некоректний routing decision")
            if not all(isinstance(value, str) for value in (row.text, row.title, row.selection_key)) or type(row.has_embedding) is not bool:
                raise ValueError("Некоректні metadata матеріалу")
        sources = {row.source_id for row in self.sources}
        contents = {row.content_id for row in self.contents}
        for row in self.occurrences:
            if row.source_id not in sources or row.content_id not in contents:
                raise ValueError("Посилання occurrence не знайдено")
            if not isinstance(row.external_ref, str):
                raise ValueError("Некоректне посилання публікації")
            require_utc(row.collected_at)
            if row.published_at is not None:
                require_utc(row.published_at)
        seen_pairs = set()
        for left, right, distance in self.pairs or ():
            if left not in contents or right not in contents or left == right or type(distance) not in (int, float) or not math.isfinite(distance) or not 0 <= distance <= 2:
                raise ValueError("Некоректна пара кандидатів")
            key = tuple(sorted((left, right)))
            if key in seen_pairs:
                raise ValueError("Повторена пара кандидатів")
            seen_pairs.add(key)
        for row in self.contents:
            if row.embedding is not None:
                if len(row.embedding) != self.dimension or any(not math.isfinite(v) for v in row.embedding):
                    raise ValueError("Некоректна розмірність або значення embedding")
                if math.hypot(*row.embedding) == 0 or not math.isfinite(math.hypot(*row.embedding)):
                    raise ValueError("Embedding має мати скінченну ненульову норму")


class SignalDataAdapter(Protocol):
    """Межа read-only адаптера; сам контракт не відкриває з'єднань із БД.

    Адаптер має повертати узгоджений набір обох вікон і явно позначати
    неповну вибірку через truncated. Модель включає її незмінну ревізію.
    """

    def read(self, *, as_of: datetime, embedding_model: str, dimension: int) -> SignalData:
        ...
