from normalization.paragraphs import split_into_paragraphs, split_into_paragraphs_with_offsets

TEXT = "1. First paragraph text.\n\n2. Second paragraph text.\n\n3. Third paragraph text.\n"


def test_offsets_variant_agrees_with_plain_variant_on_number_and_text():
    plain = split_into_paragraphs(TEXT)
    with_offsets = split_into_paragraphs_with_offsets(TEXT)
    assert [(n, t) for n, t, _, _ in with_offsets] == plain


def test_offsets_point_back_at_the_exact_source_substring():
    with_offsets = split_into_paragraphs_with_offsets(TEXT)
    for number, text, start, end in with_offsets:
        assert TEXT[start:end] == text


def test_returns_empty_list_below_minimum_paragraph_count():
    assert split_into_paragraphs_with_offsets("1. Only one paragraph here.\n") == []


def test_returns_empty_list_for_empty_text():
    assert split_into_paragraphs_with_offsets("") == []
