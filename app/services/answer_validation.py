import re

from app.answer_types import AnswerResult
from app.services.answer_context import PreparedAnswerContext
from app.services.generation_service import (
    MAX_ANSWER_CHARS,
    GeneratedAnswer,
    GenerationResponseError,
)

SOURCE_MARKER = re.compile(r"\[([^\[\]\r\n]*)\]")


def validate_answer(
    *,
    generated: GeneratedAnswer,
    context: PreparedAnswerContext,
) -> AnswerResult:
    answer = generated.answer.strip()

    if not answer or len(answer) > MAX_ANSWER_CHARS or "\x00" in answer:
        raise GenerationResponseError("Answer text is empty or too long.")

    cited_ids = SOURCE_MARKER.findall(answer)
    remaining_text = SOURCE_MARKER.sub("", answer)

    if "[" in remaining_text or "]" in remaining_text:
        raise GenerationResponseError("Answer contains malformed source markers.")

    if generated.status == "insufficient_information":
        if generated.source_ids or cited_ids:
            raise GenerationResponseError(
                "An insufficient-information answer must not cite sources."
            )

        return AnswerResult(
            answer=answer,
            sources=(),
            status="insufficient_information",
        )

    declared_ids = generated.source_ids

    if not declared_ids or not cited_ids:
        raise GenerationResponseError("An answered result must cite sources.")

    if len(declared_ids) != len(set(declared_ids)):
        raise GenerationResponseError("Source IDs must not contain duplicates.")

    if set(declared_ids) != set(cited_ids):
        raise GenerationResponseError(
            "Declared source IDs do not match the answer markers."
        )

    sources_by_id = {source.source_id: source for source in context.sources}

    if set(cited_ids) - sources_by_id.keys():
        raise GenerationResponseError("Answer refers to an unknown source.")

    sources = tuple(sources_by_id[source_id] for source_id in dict.fromkeys(cited_ids))

    return AnswerResult(
        answer=answer,
        sources=sources,
        status="answered",
    )
