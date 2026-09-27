"""Shipped tokenizer vocabularies for :mod:`voidcode.provider.tokenizer`.

Each ``*.utok1.bz2`` is a bz2-compressed UTOK1 container: ``"UTOK1\\n"`` + a
``<u32 LE>`` token count + N x (``<ULEB128 length>`` + raw token bytes), with
rank == index. Regenerate with ``python scripts/extract_tokenizer_data.py``.
"""
