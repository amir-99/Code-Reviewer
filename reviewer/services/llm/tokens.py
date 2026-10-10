"""Conservative token estimates for sizing prompts and reserving budget.

Prompts used to be measured in UTF-8 bytes and compared with token windows,
which used roughly a quarter of each model's real window. Tokenizers average
three to four bytes per token for English and source code, but close to one
token per character for most non-Latin scripts, so each non-ASCII character
counts as a whole token and ASCII text as a third of one.
"""

import json

BYTES_PER_TOKEN = 3
# What a call reserves for its answer beyond the prompt when the model names no
# output limit. Settlement charges actual usage, so this only bounds overlap.
OUTPUT_RESERVE = 16384


def estimate(text):
    ascii_bytes = len(text.encode("ascii", "ignore"))
    return -(-ascii_bytes // BYTES_PER_TOKEN) + (len(text) - ascii_bytes)


def messages(system, user):
    """Estimated tokens of a system/user exchange as the gateway receives it."""
    return estimate(
        json.dumps(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            ensure_ascii=False,
        )
    )
