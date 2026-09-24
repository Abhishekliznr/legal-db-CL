"""
Vendored from https://github.com/Legal-NLP-EkStep/legal_NER (Apache-2.0),
trimmed to only the preamble-splitting helpers extract_entities_from_judgment_text()
in legal_ner.py needs -- the upstream file's IndianKanoon HTML-scraping
functions (get_text_from_indiankanoon_url et al.) are dropped since this
service already has its own OCR'd judgment text and never fetches from
IndianKanoon.
"""

import re

import spacy


def get_keyword_based_preamble_end_char_offset(text):
    preamble_end_keywords = ["JUDGMENT", "ORDER", "J U D G M E N T", "O R D E R", "JUDGMENT & ORDER", "COMMON ORDER", "ORAL JUDGMENT"]
    preamble_end_char_offset = 0

    for preamble_keyword in preamble_end_keywords:
        match = re.search(r'\n\s*' + preamble_keyword + r'\s*\n', text)
        if match:
            preamble_end_char_offset = match.span()[1]
            break

    if preamble_end_char_offset == 0:
        for preamble_keyword in preamble_end_keywords:
            match = re.search(preamble_keyword, text)
            if match:
                preamble_end_char_offset = match.span()[1]
                break
    return preamble_end_char_offset


def convert_upper_case_to_title(txt):
    title_tokens = []
    for token in txt.split(' '):
        title_subtokens = []
        for subtoken in token.split('\n'):
            if subtoken.isupper():
                title_subtokens.append(subtoken.title())
            else:
                title_subtokens.append(subtoken)
        title_tokens.append('\n'.join(title_subtokens))
    title_txt = ' '.join(title_tokens)
    return title_txt


def guess_preamble_end(truncated_txt, nlp):
    preamble_end = 0
    max_length = 20000
    tokens = nlp.tokenizer(truncated_txt)
    if len(tokens) > max_length:
        chunks = [tokens[i:i + max_length] for i in range(0, len(tokens), max_length)]
        nlp_docs = [nlp(i.text) for i in chunks]
        truncated_doc = spacy.tokens.Doc.from_docs(nlp_docs)
    else:
        truncated_doc = nlp(truncated_txt)
    successive_preamble_pattern_breaks = 0
    preamble_patterns_breaks_theshold = 1
    sent_list = [sent for sent in truncated_doc.sents]
    for sent_id, sent in enumerate(sent_list):
        verb_exclusions = ['reserved', 'pronounced', 'dated', 'signed']
        sent_pos_tag = [token.pos_ for token in sent if token.lower_ not in verb_exclusions]
        verb_present = 'VERB' in sent_pos_tag

        allowed_lowercase = ['for', 'at', 'on', 'the', 'in', 'of']
        upppercase_or_titlecase = all([token.text in allowed_lowercase or token.is_upper or token.is_title or token.is_punct for token in sent if token.is_alpha])

        if verb_present and not upppercase_or_titlecase:
            successive_preamble_pattern_breaks += 1
            if successive_preamble_pattern_breaks > preamble_patterns_breaks_theshold:
                preamble_end = sent_list[sent_id - preamble_patterns_breaks_theshold - 1].end_char
                break
        else:
            if successive_preamble_pattern_breaks > 0 and (verb_present or not upppercase_or_titlecase):
                preamble_end = sent_list[sent_id - preamble_patterns_breaks_theshold - 1].end_char
                break
            else:
                successive_preamble_pattern_breaks = 0

    return preamble_end


def seperate_and_clean_preamble(txt, preamble_splitting_nlp):
    keyword_preamble_end_offset = get_keyword_based_preamble_end_char_offset(txt)
    if keyword_preamble_end_offset == 0:
        preamble_end_offset = 5000
    else:
        preamble_end_offset = keyword_preamble_end_offset + 200
    truncated_txt = txt[:preamble_end_offset]
    guessed_preamble_end = guess_preamble_end(truncated_txt, preamble_splitting_nlp)

    if guessed_preamble_end == 0:
        preamble_end = keyword_preamble_end_offset
    else:
        preamble_end = guessed_preamble_end

    preamble_txt = txt[:preamble_end]
    title_txt = convert_upper_case_to_title(preamble_txt)
    return title_txt, preamble_end


def get_sentence_docs(doc_judgment, nlp_judgment):
    sentences = [sent.text for sent in doc_judgment.sents]
    docs = []
    for doc in nlp_judgment.pipe(sentences):
        docs.append(doc)
    combined_docs = spacy.tokens.Doc.from_docs(docs)
    return combined_docs
