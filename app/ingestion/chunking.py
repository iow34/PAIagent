def chunk_text(
    text: str,
    max_chars: int = 1000,
    overlap: int = 100,
) -> list[str]:
    """Divides text into fragments with a character length limit."""
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero.")

    if not 0 <= overlap < max_chars:
        raise ValueError("overlap must satisfy 0 <= overlap < max_chars.")

    if not text.strip():
        return []

    chunks: list[str] = []
    start = 0
    text_length = len(text)

    while start < text_length:
        hard_end = min(start + max_chars, text_length)
        end = hard_end

        if hard_end < text_length:
            search_start = start + max(
                max_chars // 2,
                overlap + 1,
            )

            for seperator in ("\n\n", "\n", " ", "\t"):
                position = text.rfind(
                    seperator,
                    search_start,
                    hard_end,
                )

                if position != -1:
                    end = position + len(seperator)
                    break

        chunk = text[start:end]

        if chunk.strip():
            chunks.append(chunk)

        if end == text_length:
            break

        start = end - overlap

    return chunks
