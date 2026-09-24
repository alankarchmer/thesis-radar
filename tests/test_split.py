from thesis_radar.split import MAX_TOKENS, estimate_tokens, split_pages


def test_short_paragraphs_on_a_page_merge():
    page = "Inventory rose.\n\nPricing held.\n"
    drafts = split_pages([page])
    assert [(d.seq, d.page, d.text) for d in drafts] == [(0, 1, "Inventory rose. Pricing held.")]
    assert page[drafts[0].char_start:drafts[0].char_end] == "Inventory rose.\n\nPricing held."


def test_pages_never_merge():
    drafts = split_pages(["Inventory rose.", "Pricing held."])
    assert [(d.page, d.text) for d in drafts] == [(1, "Inventory rose."), (2, "Pricing held.")]


def test_merging_stops_at_the_target_size():
    paragraph = "word " * 120  # about 150 tokens
    assert len(split_pages([f"{paragraph}\n\n{paragraph}"])) == 2


def test_long_paragraphs_split_at_sentences_with_their_own_spans():
    sentence = "Dealer inventory rose again in the quarter across most regions. "
    page = "  " + sentence * 60
    drafts = split_pages([page])
    assert len(drafts) > 1
    assert all(estimate_tokens(d.text) <= MAX_TOKENS for d in drafts)
    assert all(d.text.endswith(".") for d in drafts)
    for d in drafts:
        assert " ".join(page[d.char_start:d.char_end].split()) == d.text
    assert drafts[0].char_start == 2 and drafts[1].char_start > drafts[0].char_end - 2


def test_one_giant_sentence_is_hard_cut():
    drafts = split_pages(["x" * 3000 + " " + "y" * 3000])
    assert all(estimate_tokens(d.text) <= MAX_TOKENS for d in drafts)
    assert "".join(d.text for d in drafts).replace(" ", "") == "x" * 3000 + "y" * 3000


def test_transcript_turns_keep_speakers_apart_with_spans():
    page = (
        "Operator: Welcome to the call.\n"
        "Jane Doe - CFO: Inventory rose.\n"
        "It should normalize by spring.\n"
        "Q: What about pricing?\n"
    )
    drafts = split_pages([page], transcript=True)
    assert [(d.speaker, d.text) for d in drafts] == [
        ("Operator", "Welcome to the call."),
        ("Jane Doe - CFO", "Inventory rose. It should normalize by spring."),
        ("Q", "What about pricing?"),
    ]
    assert page[drafts[1].char_start:drafts[1].char_end] == "Inventory rose.\nIt should normalize by spring."


def test_section_headings_are_not_speakers():
    drafts = split_pages(["Forward-Looking Statements: we may be wrong.\nOperator: Hi."], transcript=True)
    assert drafts[0].speaker is None


def test_colons_are_not_speakers_outside_transcripts():
    assert all(d.speaker is None for d in split_pages(["Revenue: up 5%.\n\nMargins: flat."]))


def test_blank_pages_produce_nothing():
    assert split_pages(["", "  \n\n "]) == []
