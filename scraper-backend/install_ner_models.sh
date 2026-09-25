#!/bin/sh
# Installs the spaCy models pipeline/legal_ner_extraction.py loads, into
# whichever Python is on PATH (activate the venv first when running locally).
#
# --no-deps: both wheels' metadata pin spacy<3.3, but they load and produce
# identical act/section output on spacy 3.8 (requirements.txt).
# The HuggingFace wheel is renamed because its published filename has no
# version, which modern pip rejects as an invalid wheel filename.
set -e

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

LEGAL_NER_WHEEL="$TMP_DIR/en_legal_ner_sm-3.2.0-py3-none-any.whl"
curl -fsSL -o "$LEGAL_NER_WHEEL" \
    https://huggingface.co/opennyaiorg/en_legal_ner_sm/resolve/main/en_legal_ner_sm-any-py3-none-any.whl

python -m pip install --no-cache-dir --no-deps \
    "$LEGAL_NER_WHEEL" \
    https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.2.0/en_core_web_sm-3.2.0-py3-none-any.whl
