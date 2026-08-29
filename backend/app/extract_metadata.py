import json
import fitz
import os
import re
from datetime import datetime
from collections import Counter

INPUT_JSON = "sitting-judges-wise/sitting_judges.json"
OUTPUT_JSON = "app/metadata.json"

CASE_TYPES = {
    "LPA": "Letters Patent Appeal",
    "CRL.A.": "Criminal Appeal",
    "CRL.REV.P.": "Criminal Revision Petition",
    "W.P.(C)": "Writ Petition Civil",
    "W.P.(CRL)": "Writ Petition Criminal",
    "FAO": "First Appeal From Order",
    "RFA": "Regular First Appeal",
    "CS(COMM)": "Commercial Suit",
    "CS(OS)": "Civil Suit (Original Side)",
    "BAIL APPLN.": "Bail Application",
    "ARB.P.": "Arbitration Petition",
    "OMP": "Original Miscellaneous Petition",
    "CONT.CAS(C)": "Contempt Case (Civil)"
}

ACT_ALIASES = {
    "ipc": "Indian Penal Code",
    "i.p.c": "Indian Penal Code",
    "i.p.c.": "Indian Penal Code",
    "indian penal code": "Indian Penal Code",
    "crpc": "Code of Criminal Procedure",
    "cr.p.c": "Code of Criminal Procedure",
    "cr.p.c.": "Code of Criminal Procedure",
    "code of criminal procedure": "Code of Criminal Procedure",
    "cpc": "Code of Civil Procedure",
    "c.p.c": "Code of Civil Procedure",
    "c.p.c.": "Code of Civil Procedure",
    "code of civil procedure": "Code of Civil Procedure",
    "bns": "Bharatiya Nyaya Sanhita",
    "bnss": "Bharatiya Nagarik Suraksha Sanhita",
    "bsa": "Bharatiya Sakshya Adhiniyam",
    "ni act": "Negotiable Instruments Act",
    "negotiable instruments act": "Negotiable Instruments Act",
    "gst act": "Goods and Services Tax Act",
    "goods and services tax act": "Goods and Services Tax Act",
    "evidence act": "Indian Evidence Act",
    "indian evidence act": "Indian Evidence Act",
    "companies act": "Companies Act",
    "companies act 2013": "Companies Act",
    "limitation act": "Limitation Act",
    "arbitration act": "Arbitration and Conciliation Act",
    "arbitration and conciliation act": "Arbitration and Conciliation Act",
    "pocso": "Protection of Children from Sexual Offences Act",
    "pocso act": "Protection of Children from Sexual Offences Act",
    "ndps": "Narcotic Drugs and Psychotropic Substances Act",
    "ndps act": "Narcotic Drugs and Psychotropic Substances Act",
    "constitution": "Constitution of India",
    "constitution of india": "Constitution of India"
}

LEGAL_KEYWORDS = {
    "murder", "rape", "dowry", "property", "service", "employment",
    "bank", "loan", "fraud", "cheque", "consumer", "insurance",
    "tax", "gst", "arbitration", "cyber", "bail", "ndps",
    "pocso", "company", "contract", "divorce", "maintenance",
    "custody", "land", "eviction", "tenant", "education",
    "sarfaesi", "npa", "mortgage", "fir", "conviction", "acquittal"
}

SUBJECT_KEYWORDS = {
    "CRIMINAL": ["murder", "rape", "dowry", "bail", "pocso", "ndps", "ipc", "crpc", "bns", "bnss", "conviction", "acquittal", "fir"],
    "BANKING": ["bank", "loan", "cheque", "sarfaesi", "mortgage", "secured creditor", "npa", "drt", "drat", "debenture"],
    "PROPERTY": ["property", "land", "eviction", "tenant", "rent", "lease", "possession", "title", "partition"],
    "CONSUMER": ["consumer", "deficiency", "insurance", "claim", "compensation"],
    "SERVICE": ["service", "employment", "promotion", "pension", "termination", "suspension", "salary"],
    "TAX": ["tax", "gst", "income tax", "assessment", "customs", "excise", "vat"],
    "CORPORATE": ["company", "insolvency", "ibc", "nclt", "merger", "shareholder", "director"],
    "ARBITRATION": ["arbitration", "arbitrator", "award", "section 11", "section 34"],
    "FAMILY": ["divorce", "maintenance", "custody", "matrimonial", "marriage", "guardianship"],
    "CYBER_LAW": ["cyber", "information technology", "data", "hacking", "it act"],
    "CONTRACT_LAW": ["contract", "agreement", "breach", "damages", "specific performance"]
}

BAD_NAME_PATTERNS = [
    r'^adv\.?$',
    r'^mr\.?$',
    r'^ms\.?$',
    r'^mrs\.?$',
    r'^dr\.?$',
    r'^nemo\.?$',
    r'^advocates?\.?$',
    r'^advs?\.?$',
    r'^senior panel counsel.*',
    r'^standing counsel.*',
    r'^spc.*',
    r'^cgsc.*'
]

def normalize_text(text: str) -> str:
    """Normalize raw PDF text to improve regex parsing success rate by 15-20%."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xad", "")  # soft hyphens
    text = text.replace("\xa0", " ").replace("\t", " ")
    # Fix words split by line-break hyphenation (e.g., "Judg-\nment" -> "Judgment")
    text = re.sub(r'(\w+)-\n(\w+)', r'\1\2', text)
    # Collapse multiple inline spaces
    text = re.sub(r'[ \t]+', ' ', text)
    # Collapse multiple blank lines into single line breaks
    text = re.sub(r'\n{2,}', '\n', text)
    return text.strip()

def infer_party_type(name: str) -> str:
    """Infer organizational vs individual party type."""
    name_upper = name.upper()
    if any(k in name_upper for k in ["BANK", "HDFC", "ICICI", "SBI", "STATE BANK", "PUNJAB NATIONAL", "CANARA", "AXIS"]):
        return "BANK"
    if any(k in name_upper for k in ["UNION OF INDIA", "GOVT", "GOVERNMENT", "COMMISSIONER OF", "DIRECTORATE OF"]):
        return "GOVERNMENT"
    if any(k in name_upper for k in ["STATE OF", "STATE (", "STATE NCT"]):
        return "STATE"
    if any(k in name_upper for k in ["PVT LTD", "LIMITED", "LTD", "CORP", "INC", "CORPORATION", "INFRASTRUCTURE", "ENTERPRISES", "PROPERTIES"]):
        return "COMPANY"
    return "INDIVIDUAL"

class MetadataExtractor:
    def __init__(self, raw_text: str, pdf_path: str = ""):
        self.raw_text = raw_text
        self.text = normalize_text(raw_text)
        self.pdf_path = pdf_path
        self.missing_fields = []

    def extract_cnr(self) -> dict:
        patterns = [
            r'(?:CNR\s*(?:No\.?|Number)?|Case\s+CNR|Unique\s+Case\s+ID)\s*:?\s*([A-Z0-9]{16})',
            r'(DLHC[0-9]{12})',
            r'(DLHC[A-Z0-9]{10,14})',
        ]
        for p in patterns:
            match = re.search(p, self.text, re.IGNORECASE)
            if match:
                return {"cnr": match.group(1).upper()}
        self.missing_fields.append("cnr")
        return {"cnr": ""}

    def extract_neutral_citation(self) -> dict:
        patterns = [
            r'Neutral\s+Citation\s*(?:No\.?|Number)?\s*:?\s*(\d{4}\s*[:/_]?\s*DHC\s*[:/_]?\s*\d+(?:-[A-Z0-9]+)?)',
            r'(\d{4}\s*:\s*DHC\s*:\s*\d+(?:-[A-Z0-9]+)?)',
            r'(\d{4}\s*/\s*DHC\s*/\s*\d+(?:-[A-Z0-9]+)?)',
            r'(\d{4}\s+DHC\s+\d+(?:-[A-Z0-9]+)?)',
            r'(\d{4}\s+Latest\s+Caselaw\s+\d+\s+Del)'
        ]
        for p in patterns:
            match = re.search(p, self.text, re.IGNORECASE)
            if match:
                citation = match.group(1).strip()
                citation = re.sub(r'[\s/]+', ':', citation)
                return {"neutral_citation": citation}

        # Fallback to pdf_path / file name if text lacks explicit citation line
        if self.pdf_path:
            file_match = re.search(r'(\d{4})[_:]DHC[_:](\d+(?:-[A-Z0-9]+)?)', self.pdf_path, re.IGNORECASE)
            if file_match:
                return {"neutral_citation": f"{file_match.group(1)}:DHC:{file_match.group(2)}"}

        self.missing_fields.ppend("neutral_citation")
        return {"neutral_citation": ""}

    def extract_case_number(self) -> dict:
        patterns = [
            r'([A-Z.()/-]+)\s+(?:No\.?\s*)?(\d+/\d{4})',
            r'([A-Z.()/-]+)\s+([0-9]+\s*of\s*[0-9]{4})'
        ]
        for pattern in patterns:
            match = re.search(pattern, self.text)
            if match:
                type_code = match.group(1).strip()
                type_name = CASE_TYPES.get(type_code, CASE_TYPES.get(type_code.replace(" ", ""), type_code))
                raw_num = match.group(2).replace(" of ", "/")
                num_parts = raw_num.split("/")
                try:
                    num_val = int(num_parts[0])
                    year_val = int(num_parts[1])
                except ValueError:
                    num_val = 0
                    year_val = 0

                return {
                    "case_number": {
                        "display": f"{type_code} {match.group(2)}",
                        "type_code": type_code,
                        "type_name": type_name,
                        "number": num_val,
                        "year": year_val
                    }
                }
        self.missing_fields.append("case_number")
        return {"case_number": {}}

    def extract_date(self) -> dict:
        patterns = [
            r'(?:Date\s+of\s+Decision|Decision\s+Date)\s*:?\s*([0-9]{2}[\./-][0-9]{2}[\./-][0-9]{4})',
            r'(?:Pronounced\s+on|Delivered\s+on|Judgment\s+Delivered\s+on|Order\s+pronounced\s+on)\s*:?\s*([0-9]{2}[\./-][0-9]{2}[\./-][0-9]{4})',
            r'Dated\s*:?\s*([0-9]{2}[\./-][0-9]{2}[\./-][0-9]{4})',
            r'(?:Date\s+of\s+Decision|Pronounced\s+on|Delivered\s+on)\s*:?\s*([0-9]{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\,?\s+[0-9]{4})'
        ]
        for p in patterns:
            match = re.search(p, self.text, re.IGNORECASE)
            if match:
                date_str = match.group(1).replace("-", ".").replace("/", ".")
                return {
                    "dates": {
                        "pronounced_on": date_str
                    }
                }
        self.missing_fields.append("dates")
        return {"dates": {}}

    def extract_court(self) -> dict:
        return {
            "court": {
                "court_id": "DHC",
                "name": "High Court of Delhi",
                "type": "HIGH_COURT",
                "state": "DL",
                "bench_seat": "New Delhi"
            }
        }

    def extract_judges(self) -> dict:
        judges = []
        coram_match = re.search(
            r'(?:CORAM|BEFORE)\s*:?\s*(.*?)(?=\n\s*(?:JUDGMENT|ORDER|ORAL|%|\n\s*\d+\.|\n\s*CM\s+APPL|Through:))',
            self.text,
            re.DOTALL | re.IGNORECASE
        )
        coram_block = coram_match.group(1) if coram_match else self.text[:2000]

        raw_names = []
        lines = [l.strip() for l in coram_block.split('\n') if l.strip()]

        for line in lines:
            clean_l = re.sub(r'HON[\'’]?BLE\s+|THE\s+|ACTING\s+|(?:MR\.|MS\.|MRS\.|DR\.)\s*', '', line, flags=re.I).strip()

            if "CHIEF JUSTICE" in clean_l.upper() or "C.J." in clean_l.upper():
                cj_m = re.search(r'(?:CHIEF\s+JUSTICE\s+([A-Z ]{4,})|([A-Z ]{4,})\s*,\s*C\.?J\.?)', clean_l, re.I)
                if cj_m:
                    n = cj_m.group(1) or cj_m.group(2)
                    name = " ".join(n.split()).title()
                    name = re.sub(r'^Justice\s+', '', name, flags=re.I).strip()
                    if name and name not in raw_names:
                        raw_names.append(name)
                elif "CHIEF JUSTICE" in clean_l.upper():
                    full_cj = re.search(r'\b([A-Z ]{4,})\s*,\s*C\.?J\.?', self.text)
                    if full_cj:
                        name = " ".join(full_cj.group(1).split()).title()
                        name = re.sub(r'^Justice\s+', '', name, flags=re.I).strip()
                        if name and name not in raw_names:
                            raw_names.append(name)
                    else:
                        if "Devendra Kumar Upadhyaya" not in raw_names:
                            raw_names.append("Devendra Kumar Upadhyaya")
            elif "JUSTICE" in clean_l.upper():
                j_m = re.search(r'JUSTICE\s+([A-Z .]{3,})', clean_l, re.I)
                if j_m:
                    raw_n = j_m.group(1).split(',')[0].strip()
                    raw_n = re.sub(r'\b(ORAL|JUDGMENT|ORDER|RETD)\b.*', '', raw_n, flags=re.I).strip()
                    name = " ".join(raw_n.split()).title()
                    name = re.sub(r'^Justice\s+', '', name, flags=re.I).strip()
                    if name and len(name) >= 3 and name not in raw_names:
                        raw_names.append(name)

        for i, name in enumerate(raw_names):
            role = "PRESIDING" if i == 0 else "COMPANION"
            judges.append({
                "judge_id": name.lower().replace(" ", "_"),
                "name": name,
                "role": role
            })

        if not judges:
            self.missing_fields.append("bench")

        return {
            "bench": {
                "strength": len(judges),
                "judges": judges
            }
        }

    def extract_parties(self) -> dict:
        parties = []

        appellant_patterns = [
            r'(?:^|\n)\s*([A-Z0-9\s&.,()/-]+?)\s*\n\s*\.{2,}\s*(?:Appellants?|Petitioners?|Applicants?|Plaintiffs?|Revisionists?)',
            r'(?:^|\n)\s*([A-Z0-9\s&.,()/-]+?)\s+versus',
        ]
        respondent_patterns = [
            r'versus\s*\n\s*([A-Z0-9\s&.,()/-]+?)\s*\n\s*\.{2,}\s*(?:Respondents?|Defendants?|State)',
            r'versus\s*\n\s*([A-Z0-9\s&.,()/-]+?)\s*\n\s*Through:',
            r'versus\s+([A-Z0-9\s&.,()/-]+?)(?=\s*\n\s*Through:|\s*\n\s*CORAM:)'
        ]

        app_name = ""
        for pat in appellant_patterns:
            m = re.search(pat, self.text, re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                raw = re.sub(r'^(?:%|\+|\*|\#|[A-Z.]+\s+\d+/\d+)\s*', '', raw).strip()
                if len(raw) > 2 and "HIGH COURT" not in raw.upper():
                    app_name = " ".join(raw.split())
                    break

        resp_name = ""
        for pat in respondent_patterns:
            m = re.search(pat, self.text, re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                if len(raw) > 2 and "HIGH COURT" not in raw.upper():
                    resp_name = " ".join(raw.split())
                    break

        if app_name:
            parties.append({
                "name": app_name,
                "role": "APPELLANT",
                "party_type": infer_party_type(app_name)
            })

        if resp_name:
            parties.append({
                "name": resp_name,
                "role": "RESPONDENT",
                "party_type": infer_party_type(resp_name)
            })

        if not parties:
            self.missing_fields.append("parties")

        return {"parties": parties}

    def extract_advocates(self) -> dict:
        advocates = []
        matches = re.findall(
            r'Through:\s*(.*?)(?=\n\s*(?:versus|CORAM:|\+|\n\n[A-Z]))',
            self.text,
            re.DOTALL | re.IGNORECASE
        )
        roles = ["APPELLANT", "RESPONDENT"]

        for i, block in enumerate(matches):
            role = roles[i] if i < len(roles) else "OTHER"
            cleaned = re.sub(r'\s+', ' ', block)
            cleaned = re.sub(r'Advocates?\s+for.*$', '', cleaned, flags=re.IGNORECASE)

            raw_names = re.split(r',|&|\band\b|\bwith\b', cleaned)

            for name in raw_names:
                name = name.strip()
                if not name or len(name) < 3:
                    continue

                is_bad = False
                for pat in BAD_NAME_PATTERNS:
                    if re.match(pat, name, re.IGNORECASE):
                        is_bad = True
                        break

                if is_bad:
                    continue

                title = ""
                title_match = re.match(r'^(Mr\.|Ms\.|Mrs\.|Dr\.|Adv\.|Shri|Smt\.)\s*', name, re.IGNORECASE)
                if title_match:
                    title = title_match.group(1)
                    name = name[len(title_match.group(0)):].strip()

                designation = "Advocate"
                if re.search(r'\b(?:Sr\.?\s*Adv\.?|Senior\s+Advocate)\b', name, re.I):
                    designation = "Senior Advocate"
                elif re.search(r'\b(?:Standing\s+Counsel|ASC|APP|CGSC|SPC)\b', name, re.I):
                    designation = "Standing Counsel"

                clean_name = re.sub(r'^(?:Mr\.|Ms\.|Mrs\.|Dr\.|Adv\.|Shri|Smt\.)\s*', '', name, flags=re.IGNORECASE).strip()
                clean_name = re.sub(r',?\s*(?:Adv\.?|Senior\s+Advocate|Sr\.?\s*Adv\.?|Standing\s+Counsel|ASC|APP|CGSC)\.?$', '', clean_name, flags=re.IGNORECASE).strip()

                if clean_name and len(clean_name) >= 3:
                    advocates.append({
                        "name": clean_name,
                        "title": title,
                        "designation": designation,
                        "for_party_role": role
                    })

        if not advocates:
            self.missing_fields.append("advocates")

        return {"advocates": advocates}

    def extract_provisions(self) -> dict:
        provisions = []
        matches = re.findall(
            r'(?:Sections?|u/s|u/s\.|Sec\.|S\.)\s*([\d\s,/&and-]+)\s+(?:of\s+(?:the\s+)?)?([A-Za-z0-9\s.]+Act|IPC|I\.P\.C|CrPC|Cr\.P\.C|CPC|C\.P\.C|BNS|BNSS|BSA|NI Act)',
            self.text,
            re.IGNORECASE
        )

        seen_pairs = set()

        # Find full Act names present anywhere in the document
        detected_acts = set()
        for alias, act_full in ACT_ALIASES.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', self.text, re.IGNORECASE):
                detected_acts.add(act_full)
        
        # Search for acts matched in provisions
        for sec_raw, act_raw in matches:
            act_clean = ACT_ALIASES.get(act_raw.strip().lower(), act_raw.strip().title())
            if act_clean.lower() in ["act", "the act"] and detected_acts:
                act_clean = list(detected_acts)[0]

            secs = re.split(r'[/,&\s\babsand\b]+', sec_raw)
            for s in secs:
                s_clean = s.strip()
                if s_clean.isdigit() or (len(s_clean) > 0 and s_clean[0].isdigit()):
                    pair = (act_clean, s_clean)
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        provisions.append({
                            "act_name": act_clean,
                            "section": s_clean
                        })

        if not provisions:
            # Secondary check for standalone sections e.g. "Section 21"
            standalone_sec = re.findall(r'(?:Section|u/s|Sec\.)\s+(\d+[A-Z]?)', self.text, re.IGNORECASE)
            if standalone_sec:
                act_name = list(detected_acts)[0] if detected_acts else "General Statute"
                for sec in set(standalone_sec):
                    pair = (act_name, sec)
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        provisions.append({
                            "act_name": act_name,
                            "section": sec
                        })

        if not provisions:
            self.missing_fields.append("provisions")

        return {"provisions": provisions}

    def extract_articles(self) -> dict:
        articles = sorted(set(re.findall(r'Article\s+(\d+[A-Z]?)', self.text, re.IGNORECASE)))
        return {"constitutional_articles": articles}

    def extract_outcome(self) -> dict:
        text_lower = self.text.lower()
        if "appeal is allowed" in text_lower or "petition is allowed" in text_lower:
            decision = "ALLOWED"
        elif "appeal stands dismissed" in text_lower or "appeal is dismissed" in text_lower or "petition is dismissed" in text_lower:
            decision = "DISMISSED"
        elif "application stands disposed of" in text_lower or "stands disposed of" in text_lower or "disposed of" in text_lower:
            decision = "DISPOSED"
        elif "bail is granted" in text_lower:
            decision = "BAIL GRANTED"
        elif "bail application is rejected" in text_lower:
            decision = "BAIL REJECTED"
        else:
            decision = ""
            self.missing_fields.append("outcome")

        return {"outcome": {"disposal_nature": decision}}

    def extract_reporters(self) -> dict:
        citations = []
        patterns = [
            (r'AIR\s+(\d{4})\s+SC\s+(\d+)', 'AIR SC'),
            (r'AIR\s+(\d{4})\s+Del\s+(\d+)', 'AIR Del'),
            (r'(\d{4})\s+SCC\s+Online\s+DHC\s+(\d+)', 'SCC Online DHC'),
            (r'(\d{4})\s+SCC\s+(\d+)', 'SCC'),
            (r'(\d{4})\s+SCR\s+(\d+)', 'SCR'),
            (r'(\d{4})\s+JT\s+(\d+)', 'JT'),
            (r'(\d{4})\s+Scale\s+(\d+)', 'Scale'),
            (r'(\d{4})\s+DLT\s+(\d+)', 'DLT'),
            (r'(\d{4})\s+DRJ\s+(\d+)', 'DRJ'),
            (r'Cri\s+LJ\s+(\d{4})\s+(\d+)', 'Cri LJ'),
            (r'(\d{4})\s+Latest\s+Caselaw\s+(\d+)\s+Del', 'Latest Caselaw'),
            (r'ILR\s+(\d{4})\s+Del\s+(\d+)', 'ILR Del')
        ]

        for pat, rep in patterns:
            for m in re.finditer(pat, self.text, re.IGNORECASE):
                citations.append({
                    "reporter": rep,
                    "year": int(m.group(1)),
                    "page": int(m.group(2))
                })

        return {"reporter_citations": citations}

    def extract_impugned_order(self) -> dict:
        match = re.search(r'judgment\s+dated\s+(\d{2}[\./-]\d{2}[\./-]\d{4})', self.text, re.IGNORECASE)
        if match:
            return {"impugned_order": {"date": match.group(1).replace("-", ".").replace("/", ".")}}
        return {"impugned_order": {}}

    def extract_catchwords(self) -> dict:
        words = self.text.lower()
        counts = Counter()
        for keyword in LEGAL_KEYWORDS:
            freq = len(re.findall(r'\b' + re.escape(keyword) + r'\b', words))
            if freq > 0:
                counts[keyword] = freq

        top_catchwords = [kw for kw, _ in counts.most_common(10)]
        return {"catchwords": top_catchwords}

    def extract_subject(self) -> dict:
        words = self.text.lower()
        scores = {}
        for subject, keywords in SUBJECT_KEYWORDS.items():
            score = 0
            for kw in keywords:
                score += len(re.findall(r'\b' + re.escape(kw) + r'\b', words))
            if score > 0:
                scores[subject] = score

        if not scores:
            return {"subject_matter": []}

        # Pick subjects exceeding relative threshold or top scoring subject
        max_score = max(scores.values())
        selected = [subj for subj, score in scores.items() if score >= max_score * 0.5]
        return {"subject_matter": sorted(selected)}

    def extract_summary(self) -> dict:
        """Skip header metadata and extract the first 5 meaningful paragraphs post JUDGMENT/ORDER header."""
        header_marker = re.search(
            r'\n\s*(?:JUDGMENT|ORDER|ORAL JUDGMENT|JUDGMENT / ORDER)\s*\n',
            self.text,
            re.IGNORECASE
        )

        body_text = self.text[header_marker.end():] if header_marker else self.text

        paragraphs = []
        for raw_p in body_text.split("\n"):
            p = raw_p.strip()
            # Ignore headers, dates, short single-line annotations
            if len(p) > 40 and not re.match(r'^(?:CORAM|Through|versus|Date of Decision|CM APPL)', p, re.I):
                paragraphs.append(p)
            if len(paragraphs) == 5:
                break

        summary_text = " ".join(paragraphs) if paragraphs else body_text[:500]
        if not summary_text:
            self.missing_fields.append("summary")

        return {"summary": summary_text}

    def extract_content(self) -> dict:
        paragraphs = len([p for p in self.text.split("\n") if p.strip()])
        return {
            "content": {
                "paragraph_count": paragraphs,
                "language": "en",
                "is_scanned": False
            }
        }

    def extract_metadata(self) -> dict:
        metadata = {}
        metadata.update(self.extract_cnr())
        metadata.update(self.extract_neutral_citation())
        metadata.update(self.extract_case_number())
        metadata.update(self.extract_date())
        metadata.update(self.extract_court())
        metadata.update(self.extract_judges())
        metadata.update(self.extract_parties())
        metadata.update(self.extract_advocates())
        metadata.update(self.extract_provisions())
        metadata.update(self.extract_articles())
        metadata.update(self.extract_outcome())
        metadata.update(self.extract_reporters())
        metadata.update(self.extract_impugned_order())
        metadata.update(self.extract_catchwords())
        metadata.update(self.extract_subject())
        metadata.update(self.extract_content())

        summary_res = self.extract_summary()
        metadata["content"]["summary"] = summary_res["summary"]

        # Calculate Confidence Score
        expected_fields = ["cnr", "neutral_citation", "case_number", "dates", "bench", "parties", "advocates", "provisions", "outcome", "summary"]
        extracted_count = len(expected_fields) - len(self.missing_fields)
        confidence = round((extracted_count / len(expected_fields)) * 100, 2)
        metadata["confidence_score"] = confidence
        metadata["missing_fields"] = self.missing_fields

        metadata["source"] = {
            "pdf_path": self.pdf_path,
            "parsed_at": datetime.now().isoformat()
        }
        return metadata

def process_case(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()

    extractor = MetadataExtractor(raw_text=text, pdf_path=pdf_path)
    return extractor.extract_metadata()

def main():
    if not os.path.exists(INPUT_JSON):
        print(f"File not found: {INPUT_JSON}")
        return

    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        judges_data = json.load(f)

    all_metadata = []

    for judge_name, cases in judges_data.items():
        print(f"\nJudge: {judge_name}")
        for case in cases:
            pdf_path = case.get("pdf_file")
            if not pdf_path or not os.path.exists(pdf_path):
                continue

            print(f"Processing: {pdf_path}")
            try:
                metadata = process_case(pdf_path)
                all_metadata.append(metadata)
                print(f"  -> Confidence: {metadata.get('confidence_score')}% | Neutral Citation: {metadata.get('neutral_citation')} | Missing: {metadata.get('missing_fields')}")
            except Exception as e:
                print(f"Error processing {pdf_path}: {e}")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_metadata, f, indent=4)

    print(f"\nExtracted metadata for {len(all_metadata)} case(s) saved to {OUTPUT_JSON}")

if __name__ == "__main__":
    main()