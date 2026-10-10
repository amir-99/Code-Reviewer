from reviewer.services.llm.tokens import estimate, messages


def test_ascii_counts_a_third_and_non_ascii_a_whole_token_per_character():
    assert estimate("") == 0
    assert estimate("abc") == 1 and estimate("abcd") == 2
    assert estimate("é" * 10) == 10
    assert estimate("審査" + "abc") == 3


def test_message_estimate_covers_the_serialized_exchange():
    assert messages("system", "user") > estimate("systemuser")
