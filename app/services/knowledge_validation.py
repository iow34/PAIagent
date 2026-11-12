import re
from uuid import UUID

from app.knowledge_types import (
    DocumentKnowledge,
    KnowledgeFact,
    KnowledgeSource,
)
from app.services.knowledge_context import (
    KnowledgeContextError,
    PreparedKnowledgeContext,
)
from app.services.knowledge_generation import (
    MAX_KNOWLEDGE_FACT_CHARS,
    MAX_KNOWLEDGE_FACTS,
    MAX_KNOWLEDGE_SUMMARY_CHARS,
    MAX_KNOWLEDGE_TOPIC_CHARS,
    MAX_KNOWLEDGE_TOPICS,
    GeneratedDocumentKnowledge,
    KnowledgeResponseError,
)

KNOWLEDGE_MARKER = re.compile(r"\[K[1-9][0-9]*\]")


def _validate_text(
    value: str,
    *,
    field_name: str,
    max_chars: int,
) -> str:
    if len(value) > max_chars:
        raise KnowledgeResponseError(f"{field_name} exceeds the allowed length.")

    text = value.strip()

    if not text:
        raise KnowledgeResponseError(f"{field_name} must not be empty.")

    if "\x00" in text:
        raise KnowledgeResponseError(f"{field_name} must not contain NUL characters.")

    if KNOWLEDGE_MARKER.search(text):
        raise KnowledgeResponseError(
            f"{field_name} must not contain inline knowledge markers."
        )

    return text


def _resolve_chunk_ids(
    source_ids: list[str],
    *,
    sources_by_id: dict[str, KnowledgeSource],
) -> tuple[UUID, ...]:
    if not source_ids:
        raise KnowledgeResponseError("Knowledge references must not be empty.")

    if len(source_ids) != len(set(source_ids)):
        raise KnowledgeResponseError(
            "Knowledge references contain duplicate source IDs."
        )

    chunk_ids: list[UUID] = []

    for source_id in source_ids:
        source = sources_by_id.get(source_id)

        if source is None:
            raise KnowledgeResponseError("Knowledge refers to an unknown source.")

        chunk_ids.append(source.chunk_id)

    return tuple(chunk_ids)


def validate_knowledge(
    *,
    generated: GeneratedDocumentKnowledge,
    context: PreparedKnowledgeContext,
) -> DocumentKnowledge:
    if not context.sources:
        raise KnowledgeContextError("Knowledge validation requires source fragments.")

    sources_by_id = {
        f"K{index}": source
        for index, source in enumerate(
            context.sources,
            start=1,
        )
    }

    summary = _validate_text(
        generated.summary,
        field_name="Knowledge summary",
        max_chars=MAX_KNOWLEDGE_SUMMARY_CHARS,
    )

    summary_chunk_ids = _resolve_chunk_ids(
        generated.summary_source_ids,
        sources_by_id=sources_by_id,
    )

    if len(generated.topics) > MAX_KNOWLEDGE_TOPICS:
        raise KnowledgeResponseError("Knowledge contains too many topics.")

    topics: list[str] = []
    seen_topics: set[str] = set()

    for value in generated.topics:
        topic = _validate_text(
            value,
            field_name="Knowledge topic",
            max_chars=MAX_KNOWLEDGE_TOPIC_CHARS,
        )

        topic_key = " ".join(topic.split()).casefold()

        if topic_key in seen_topics:
            raise KnowledgeResponseError("Knowledge contains duplicate topics.")

        seen_topics.add(topic_key)
        topics.append(topic)

    if len(generated.facts) > MAX_KNOWLEDGE_FACTS:
        raise KnowledgeResponseError("Knowledge contains too many facts.")

    facts: list[KnowledgeFact] = []
    seen_facts: set[str] = set()

    for generated_fact in generated.facts:
        text = _validate_text(
            generated_fact.text,
            field_name="Knowledge fact",
            max_chars=MAX_KNOWLEDGE_FACT_CHARS,
        )

        fact_key = " ".join(text.split()).casefold()

        if fact_key in seen_facts:
            raise KnowledgeResponseError("Knowledge contains duplicate facts.")

        chunk_ids = _resolve_chunk_ids(
            generated_fact.source_ids,
            sources_by_id=sources_by_id,
        )

        seen_facts.add(fact_key)
        facts.append(
            KnowledgeFact(
                text=text,
                chunk_ids=chunk_ids,
            )
        )

    return DocumentKnowledge(
        document_id=context.document_id,
        summary=summary,
        summary_chunk_ids=summary_chunk_ids,
        topics=tuple(topics),
        facts=tuple(facts),
        sources=context.sources,
        coverage=context.coverage,
    )
