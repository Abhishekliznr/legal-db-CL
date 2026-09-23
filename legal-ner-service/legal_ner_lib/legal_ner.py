"""
Vendored from https://github.com/Legal-NLP-EkStep/legal_NER (Apache-2.0),
trimmed to extract_entities_from_judgment_text() itself -- dropped the
upstream __main__ block (displacy visualization demo) and the unused
get_text_from_indiankanoon_url import.
"""

import re

import spacy
from wasabi import msg

from .data_preparation import seperate_and_clean_preamble, get_sentence_docs
from .postprocessing_utils import postprocessing


def extract_entities_from_judgment_text(txt, legal_nlp, nlp_preamble_splitting, text_type, do_postprocess):
    preamble_text, preamble_end = seperate_and_clean_preamble(txt, nlp_preamble_splitting)

    judgement_text = txt[preamble_end:]
    judgement_text = re.sub(r'(\w[ -]*)(\n+)', r'\1 ', judgement_text)
    judgment_doc = nlp_preamble_splitting(judgement_text)
    if text_type == 'doc':
        doc_judgment = legal_nlp(judgement_text)
    else:
        doc_judgment = get_sentence_docs(judgment_doc, legal_nlp)

    doc_preamble = legal_nlp(preamble_text)

    combined_doc = spacy.tokens.Doc.from_docs([doc_preamble, doc_judgment])

    try:
        if do_postprocess:
            combined_doc = postprocessing(combined_doc)
    except Exception:
        msg.warn('There was some issue while performing postprocessing, skipping postprocessing...')
    return combined_doc
