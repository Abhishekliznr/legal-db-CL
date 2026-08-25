import json
import os
import re
import ssl
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz

# ============================================================
# SUPREME COURT OF INDIA
# PDF + SCRAPER JSON METADATA EXTRACTOR
#
# Location:
#     app/SUPREME_COURT_OF_INDIA_SCRAPER/pdf_metadata_extractor.py
#
# Run:
#     python pdf_metadata_extractor.py
#
# Output:
#     supreme_court_metadata.json
# ============================================================


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

INPUT_JSON = BASE_DIR / "supreme_court_judgments.json"
PDF_DIR = BASE_DIR / "pdf"
OUTPUT_JSON = BASE_DIR / "supreme_court_metadata.json"

SCHEMA_VERSION = "1.0"
EXTRACTOR_VERSION = "1.0"


def get_groq_api_key() -> str | None:
    key = os.getenv("GROQ_API_KEY")
    if key and key.strip():
        return key.strip()

    candidate_env_paths = [
        BASE_DIR.parent.parent / ".env",
        BASE_DIR.parent / ".env",
        BASE_DIR / ".env",
        Path.cwd() / ".env",
    ]

    for env_file in candidate_env_paths:
        if env_file.exists():
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip().startswith("GROQ_API_KEY="):
                            val = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                            if val:
                                return val
            except Exception:
                pass
    return None


def parse_llm_json(raw_content: str) -> dict | None:
    if not raw_content:
        return None

    cleaned = raw_content.strip()

    # Remove markdown code fences.
    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(
        r"\s*```$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Find first JSON object if extra text exists.
    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]

    try:
        result = json.loads(cleaned)

        if isinstance(result, dict):
            return result

    except json.JSONDecodeError as error:
        print(f"    [LLM JSON ERROR] {error}")

    return None


def call_groq_api(prompt: str) -> dict | None:
    api_key = get_groq_api_key()
    if not api_key:
        print("    [LLM] GROQ_API_KEY not found")
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    models = ["qwen/qwen3.6-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    ctx = ssl._create_unverified_context()

    for model_name in models:
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": """You are an expert legal case-note writer for Indian Supreme Court and High Court judgments.

Your task is to generate a concise MANUPATRA-style legal Case Note.

IMPORTANT:
You are NOT receiving the complete judgment.
You are receiving only:
- case metadata
- identified legal provisions
- small text snippets surrounding Section / Rule / Article references
- disposition information

Use ONLY the supplied information.

Do NOT invent facts.
Do NOT assume facts that are not present.
Do NOT mention information that is absent from the supplied context.

The Case Note should:
1. Identify the main legal subject of the case.
2. Mention the important legal provisions involved.
3. Explain the central legal issue visible from the supplied context.
4. State the important legal principle/finding supported by the supplied context.
5. Mention the final disposition when supplied.

Write the Case Note as ONE compact professional paragraph of approximately 80-150 words.

The writing style should resemble a legal database case note:
- concise
- factual
- legally precise
- information-rich
- no unnecessary procedural narration
- no repetition
- no headings
- no bullet points
- no markdown

Return ONLY valid JSON:

{
  "case_note": "..."
}""",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0.1,
            "max_tokens": 1200,
        }

        for attempt in range(2):
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=30, context=ctx) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    raw_content = res_data["choices"][0]["message"]["content"].strip()
                    parsed = parse_llm_json(raw_content)
                    if parsed and "case_note" in parsed:
                        return parsed
            except urllib.error.HTTPError as err:
                if err.code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                else:
                    break
            except Exception as e:
                print(f"    [LLM ERROR - {model_name}] {e}")
                break

    return None


# ============================================================
# CASE TYPES MAPPING
# ============================================================

CASE_TYPES = {
    "C.A.": "Civil Appeal",
    "CA": "Civil Appeal",
    "CIVIL APPEAL": "Civil Appeal",
    "CRL.A.": "Criminal Appeal",
    "CRL.A": "Criminal Appeal",
    "CRIMINAL APPEAL": "Criminal Appeal",
    "SLP(C)": "Special Leave Petition (Civil)",
    "SLP(C) NO.": "Special Leave Petition (Civil)",
    "S.L.P.(C)": "Special Leave Petition (Civil)",
    "SLP(CRL)": "Special Leave Petition (Criminal)",
    "SLP(CRL) NO.": "Special Leave Petition (Criminal)",
    "S.L.P.(CRL)": "Special Leave Petition (Criminal)",
    "W.P.(C)": "Writ Petition (Civil)",
    "W.P.(C) NO.": "Writ Petition (Civil)",
    "WP(C)": "Writ Petition (Civil)",
    "W.P.(CRL)": "Writ Petition (Criminal)",
    "W.P.(CRL) NO.": "Writ Petition (Criminal)",
    "WP(CRL)": "Writ Petition (Criminal)",
    "T.P.(C)": "Transfer Petition (Civil)",
    "T.P.(CRL)": "Transfer Petition (Criminal)",
    "CONMT.PET.(C)": "Contempt Petition (Civil)",
    "CONMT.PET.(CRL)": "Contempt Petition (Criminal)",
    "R.P.(C)": "Review Petition (Civil)",
    "R.P.(CRL)": "Review Petition (Criminal)",
    "CURATIVE PET.": "Curative Petition",
    "M.A.": "Miscellaneous Application",
    "MA": "Miscellaneous Application",
}


# ============================================================
# CASE CATEGORY MAPPING
# ============================================================

CASE_CATEGORY_MAP = {
    "C.A.": "CIVIL_APPEAL",
    "CA": "CIVIL_APPEAL",
    "CIVIL APPEAL": "CIVIL_APPEAL",
    "CRL.A.": "CRIMINAL_APPEAL",
    "CRL.A": "CRIMINAL_APPEAL",
    "CRIMINAL APPEAL": "CRIMINAL_APPEAL",
    "SLP(C)": "SPECIAL_LEAVE_PETITION_CIVIL",
    "SLP(C) NO.": "SPECIAL_LEAVE_PETITION_CIVIL",
    "S.L.P.(C)": "SPECIAL_LEAVE_PETITION_CIVIL",
    "SLP(CRL)": "SPECIAL_LEAVE_PETITION_CRIMINAL",
    "SLP(CRL) NO.": "SPECIAL_LEAVE_PETITION_CRIMINAL",
    "S.L.P.(CRL)": "SPECIAL_LEAVE_PETITION_CRIMINAL",
    "W.P.(C)": "WRIT_PETITION_CIVIL",
    "W.P.(C) NO.": "WRIT_PETITION_CIVIL",
    "WP(C)": "WRIT_PETITION_CIVIL",
    "W.P.(CRL)": "WRIT_PETITION_CRIMINAL",
    "W.P.(CRL) NO.": "WRIT_PETITION_CRIMINAL",
    "WP(CRL)": "WRIT_PETITION_CRIMINAL",
    "T.P.(C)": "TRANSFER_PETITION_CIVIL",
    "T.P.(CRL)": "TRANSFER_PETITION_CRIMINAL",
    "CONMT.PET.(C)": "CONTEMPT_PETITION_CIVIL",
    "CONMT.PET.(CRL)": "CONTEMPT_PETITION_CRIMINAL",
    "R.P.(C)": "REVIEW_PETITION_CIVIL",
    "R.P.(CRL)": "REVIEW_PETITION_CRIMINAL",
    "CURATIVE PET.": "CURATIVE_PETITION",
    "M.A.": "MISCELLANEOUS_APPLICATION",
    "MA": "MISCELLANEOUS_APPLICATION",
}


# ============================================================
# OVERRULED PATTERNS
# ============================================================

OVERRULED_PATTERNS = [
    r"\boverruled\b",
    r"\boverruled the decision\b",
    r"\bhereby overruled\b",
    r"\bjudgment overruled\b",
    r"\bearlier decision is overruled\b",
    r"\bcase is overruled\b",
]



# ============================================================
# ACT ALIASES
# ============================================================

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
    "constitution of india": "Constitution of India",
    "supreme court rules": "Supreme Court Rules",
}


# ============================================================
# LEGAL KEYWORDS
# ============================================================

LEGAL_KEYWORDS = {
    # Requested Keywords
    "absence", "account", "act", "allegation", "appeal", "application", "article",
    "authority", "bench", "benefit", "board", "case", "challenge", "charge",
    "claim", "commissioner", "concern", "condition", "conduct", "consideration",
    "copy", "cost", "date", "date of", "decision", "direction", "dispute",
    "division", "due", "evidence", "filing", "finding", "form", "government",
    "grant", "ground", "hearing", "history", "india", "information", "interest",
    "issue", "judgment", "jurisdiction", "land", "legal", "letter", "material",
    "member", "month", "name", "nature", "notice", "objection", "office",
    "officer", "order", "parties", "pass", "payment", "pending", "period",
    "person", "petition", "position", "possession", "post", "principle",
    "procedure", "proceeding", "proceedings", "property", "public", "reason",
    "receipt", "record", "reference", "register", "registered", "relief",
    "report", "return", "revision", "right", "sale", "service", "set aside",
    "state", "statement", "subject", "suit", "terms", "title", "transfer",
    "trial", "trial court", "tribunal", "witness", "writ", "writ petition",
    # Additional specific legal terms
    "murder", "rape", "dowry", "employment", "bank", "loan", "fraud", "cheque",
    "consumer", "insurance", "tax", "gst", "arbitration", "cyber", "bail",
    "ndps", "pocso", "company", "contract", "divorce", "maintenance", "custody",
    "eviction", "tenant", "education", "sarfaesi", "npa", "mortgage", "fir",
    "conviction", "acquittal", "pension", "promotion", "termination", "suspension",
    "salary", "lease", "partition", "constitution", "fundamental rights",
}


# ============================================================
# SUBJECT KEYWORDS
# ============================================================

SUBJECT_KEYWORDS = {
    "Adjective Laws": ["adjective law", "adjective laws", "procedural law", "law of procedure"],
    "Administrative": ["administrative", "admin law", "administrative law", "tribunal", "central administrative tribunal", "cat"],
    "Agriculture": ["agriculture", "agricultural", "farm", "farmer", "crop", "land ceiling"],
    "Arbitration": ["arbitration", "arbitrator", "arbitral award", "section 11", "section 34", "arbitration and conciliation act"],
    "Articles": ["article", "articles", "article 32", "article 136", "article 142", "article 226"],
    "Banking": ["banking", "bank", "loan", "borrower", "npa", "sarfaesi", "drt", "drat", "rbi", "debenture", "mortgage"],
    "Capital Market": ["capital market", "sebi", "securities", "stock exchange", "shares", "insider trading"],
    "Civil": ["civil", "civil appeal", "cpc", "code of civil procedure", "plaint", "written statement", "decree", "civil suit"],
    "Commercial": ["commercial", "commercial dispute", "commercial court", "commercial division", "business"],
    "Company": ["company", "company law", "companies act", "director", "shareholder", "winding up", "nclt", "nclat", "merger"],
    "Constitution": ["constitution", "constitutional", "constitution of india", "fundamental rights", "constitutional bench", "article 32", "article 226", "ultra vires"],
    "Consumer": ["consumer", "consumer protection", "consumer forum", "ncdrc", "deficiency in service", "consumer dispute"],
    "Contempt of Court": ["contempt", "contempt of court", "contempt petition", "wilful disobedience"],
    "Contract": ["contract", "contract law", "agreement", "breach of contract", "damages", "specific performance", "indemnity"],
    "Criminal": ["criminal", "criminal appeal", "crpc", "ipc", "bns", "bnss", "fir", "conviction", "acquittal", "bail", "charge sheet", "ipc section"],
    "Customs": ["customs", "customs act", "import duty", "export duty", "customs duty", "smuggling", "cestat"],
    "Cyber Laws": ["cyber", "cyber law", "cyber laws", "information technology act", "it act", "cyber crime", "data protection", "hacking"],
    "Decentralization": ["decentralization", "panchayat", "municipality", "local body", "gram panchayat", "panchayat raj"],
    "Defence": ["defence", "armed forces", "army", "navy", "air force", "court martial", "armed forces tribunal", "aft"],
    "Direct Taxation": ["direct tax", "direct taxation", "income tax", "income tax act", "assessment year", "cbdt", "taxation"],
    "Education": ["education", "university", "college", "school", "admission", "examination", "ugc", "aicte", "medical college"],
    "Election": ["election", "election commission", "representation of the people act", "voter", "polling", "candidate", "electoral"],
    "Electricity": ["electricity", "power", "electricity act", "tariff", "discom", "state electricity regulatory commission", "cerc"],
    "Employment Laws": ["employment", "employment law", "employment laws", "workman", "employee", "termination", "retrenchment", "industrial dispute"],
    "Environment": ["environment", "environmental", "environmental law", "ngt", "national green tribunal", "pollution", "forest", "ecologically sensitive"],
    "Excise": ["excise", "central excise", "excise duty", "excise act", "cestat"],
    "Family": ["family", "family law", "divorce", "matrimonial", "maintenance", "custody", "marriage", "hindu marriage act", "guardianship"],
    "FEMA": ["fema", "foreign exchange management act", "foreign exchange"],
    "FERA": ["fera", "foreign exchange regulation act"],
    "Food Adulteration": ["food adulteration", "prevention of food adulteration", "pfa", "fssai", "food safety"],
    "Funds/Donations": ["fund", "funds", "donation", "donations", "charitable fund", "trust fund"],
    "Goods and Services Tax": ["goods and services tax", "gst", "cgst", "sgst", "igst", "gst act", "input tax credit"],
    "Human Rights": ["human rights", "nhrc", "shrc", "human rights commission", "custodial violence", "torture"],
    "Import/Export": ["import", "export", "import export", "exim policy", "dgft", "foreign trade"],
    "Indirect Taxation": ["indirect tax", "indirect taxation", "gst", "customs", "excise", "service tax", "vat", "cbec", "cbic"],
    "Information and Technology Law": ["information technology law", "it law", "it act", "electronic record", "digital signature"],
    "Insolvency": ["insolvency", "ibc", "insolvency and bankruptcy code", "nclt", "nclat", "resolution plan", "corporate debtor", "liquidation"],
    "Insurance": ["insurance", "insurance policy", "insurer", "insured", "claim", "irda", "irdaic", "motor accident claim", "mact"],
    "IPR": ["ipr", "intellectual property", "trademark", "patent", "copyright", "patent act", "trademark act", "infringement"],
    "Labour and Industrial": ["labour", "industrial", "labour law", "industrial disputes act", "workman", "trade union", "strike", "lockout", "labour court"],
    "Land Acquisition": ["land acquisition", "land acquisition act", "compensation", "collector", "acquisition of land", "requisition"],
    "Law of Evidence": ["evidence", "evidence act", "indian evidence act", "proof", "witness", "admissibility", "dying declaration", "burden of proof"],
    "Law of Interpretation": ["interpretation", "interpretation of statutes", "statutory interpretation", "golden rule", "literal rule"],
    "Law of Medicine": ["medicine", "medical", "medical negligence", "doctor", "hospital", "medical council", "nmc", "mbbs"],
    "Law of Registration and Stamp Duty": ["registration", "stamp duty", "registration act", "indian stamp act", "stamping", "unregistered document"],
    "Limitation": ["limitation", "limitation act", "condonation of delay", "period of limitation", "time barred", "section 5 limitation act"],
    "Marine Laws": ["marine", "maritime", "admiralty", "shipping", "vessel", "merchant shipping act"],
    "Media and Communication": ["media", "broadcasting", "press", "journalism", "defamation", "telecom", "trai", "cable tv"],
    "Mines and Minerals": ["mines", "minerals", "mining", "mmdra", "mines and minerals act", "lease", "royalty", "illegal mining"],
    "Miscellaneous": ["miscellaneous", "general", "other"],
    "Motor Vehicles": ["motor vehicles", "motor vehicle", "motor vehicles act", "mact", "accident", "compensation", "vehicle insurance"],
    "MRTP/ Competition Laws": ["mrtp", "competition law", "competition act", "cci", "competition commission of india", "monopoly", "cartel"],
    "Municipal Tax": ["municipal tax", "property tax", "octroi", "municipal corporation", "house tax"],
    "Narcotics": ["narcotics", "ndps", "ndps act", "psychotropic substances", "contraband", "commercial quantity"],
    "Other Taxes": ["other taxes", "taxation", "wealth tax", "gift tax", "entertainment tax", "entry tax", "professional tax"],
    "Power and Energy": ["power", "energy", "power project", "electricity", "renewable energy", "cerc", "serc"],
    "Professional Conduct": ["professional conduct", "advocates act", "bar council", "bar council of india", "professional misconduct"],
    "Property": ["property", "immovable property", "transfer of property act", "title", "possession", "eviction", "partition", "tenant", "rent"],
    "Right to Information": ["right to information", "rti", "rti act", "information commissioner", "cic", "sic", "public information officer"],
    "Sales Tax/VAT": ["sales tax", "vat", "value added tax", "sales tax act", "commercial tax"],
    "Service": ["service", "service matter", "government service", "service rules", "seniority", "promotion", "pension", "disciplinary proceeding"],
    "Service Matters": ["service matters", "service matter", "service rules", "recruitment", "appointment", "promotion", "pension", "termination", "suspension", "salary"],
    "Service Tax": ["service tax", "service tax act", "finance act 1994", "taxable service"],
    "SICA": ["sica", "sick industrial companies act", "bifr", "aaifr"],
    "Specific Relief": ["specific relief", "specific relief act", "specific performance", "injunction", "remedy"],
    "Tenancy": ["tenancy", "tenant", "landlord", "rent control", "eviction", "rent act", "tenancy act"],
    "Tort": ["tort", "tortious liability", "negligence", "damages", "vicarious liability", "nuisance"],
    "Trusts and Societies": ["trust", "trusts", "society", "societies", "charitable trust", "societies registration act", "trustee", "trust act"],
}



# ============================================================
# DOCUMENT TYPES
# ============================================================

DOCUMENT_TYPES = [
    "Absence Business Policy",
    "Caselaws",
    "Central BareActs & Rules",
    "Central Notifications & Circulars",
    "Comm Reports, Drafts, Stamp Duty, Ordinance",
    "Corporate Laws & Taxation",
    "Forms",
    "Miscellaneous",
    "Other",
    "State BareActs & Rules",
    "State Notifications & Circulars",
    "Tribunals",
]


# ============================================================
# INDUSTRY KEYWORDS
# ============================================================

INDUSTRY_KEYWORDS = {
    "Agriculture and Agro Products": ["agriculture", "agro", "crop", "farming", "fertilizer"],
    "Aquaculture and Fisheries": ["aquaculture", "fishery", "fisheries", "fish"],
    "Auto": ["auto", "automobile", "motor vehicle", "vehicle", "car", "truck"],
    "Aviation": ["aviation", "airline", "airport", "aircraft", "flight"],
    "Banks": ["bank", "banking", "sbi", "hdfc", "icici", "rbi"],
    "Breweries and Distilleries": ["brewery", "distillery", "liquor", "alcohol", "spirits"],
    "Capital Goods/ Engineering": ["engineering", "capital goods", "machinery"],
    "Cement": ["cement", "concrete"],
    "Chemicals": ["chemical", "chemicals", "petrochemical"],
    "Construction/ Building Products": ["construction", "building", "builder", "real estate"],
    "Consumer Durables": ["consumer durables", "electronics", "appliances"],
    "Cooperative Societies": ["cooperative", "co-operative", "cooperative society"],
    "Education": ["education", "school", "college", "university", "academic"],
    "EOU/ SEZ/ Exporters": ["exporter", "sez", "special economic zone", "eou"],
    "Explosives": ["explosives", "explosive", "fireworks"],
    "Fertilizers": ["fertilizer", "fertilizers", "urea"],
    "Finance": ["finance", "financial", "nbfc", "lender"],
    "FMCG": ["fmcg", "consumer goods", "packaged goods"],
    "Gems and Jewellery": ["gems", "jewellery", "gold", "diamond"],
    "Healthcare": ["healthcare", "hospital", "medical", "pharmaceutical", "doctor"],
    "Hospitality": ["hospitality", "hotel", "resort", "restaurant"],
    "Importers": ["importer", "import"],
    "Information Technology": ["information technology", "software", "it services", "tech"],
    "Infrastructure": ["infrastructure", "highway", "port", "bridge", "road"],
    "Insurance": ["insurance", "insurer", "policyholder", "claim"],
    "Media and Entertainment": ["media", "entertainment", "broadcasting", "cinema", "film"],
    "Metals": ["metals", "steel", "iron", "aluminium", "copper"],
    "Mines and Minerals": ["mines", "minerals", "mining", "mineral"],
    "Oil and Gas": ["oil", "gas", "petroleum", "refinery"],
    "Paper and Packaging": ["paper", "packaging"],
    "Pharmaceutical": ["pharmaceutical", "pharma", "drug", "medicine"],
    "Power and Energy": ["power", "energy", "electricity", "solar", "wind"],
    "Publishing & Printing": ["publishing", "printing", "press"],
    "Real Estate": ["real estate", "housing", "property", "apartment", "flats"],
    "Retail": ["retail", "store", "supermarket"],
    "Rubber": ["rubber", "tyre", "tire"],
    "Services Sector": ["service", "services"],
    "Shipping": ["shipping", "maritime", "vessel", "port"],
    "Sugar": ["sugar", "sugarcane", "mill"],
    "Telecom": ["telecom", "telecommunications", "cellular", "mobile operator"],
    "Textile": ["textile", "garment", "fabric", "cotton"],
    "Tobacco": ["tobacco", "cigarette", "bidi"],
    "Trading": ["trading", "trader", "merchant"],
    "Transport and Logistics": ["transport", "logistics", "freight", "carrier"],
}


# ============================================================
# MINISTRIES LIST
# ============================================================

MINISTRIES_LIST = [
    "Bureau of Energy Efficiency", "Cabinet Division", "Election Commission of India",
    "Election Commission Secretariat", "Finance", "Home Affairs", "Indian Nursing Council",
    "Industries", "Insolvency and Bankruptcy Board of India", "Insurance Regulatory and Development Authority",
    "International Financial Services Centres Authority", "Labour and Employment", "Law Justice and Parliamentary Affairs",
    "Law Justice and Parliamentary affairs", "LGRD", "Ministry of Agriculture",
    "Ministry of Agriculture and Farmers Welfare", "Ministry of Agriculture and Irrigation", "Ministry of Ayush",
    "Ministry of Chemicals and Fertilizers", "Ministry of Civil Aviation", "Ministry of Civil Supplies Consumer Affairs and Public Distribution",
    "Ministry of Coal", "Ministry of Commerce & Industry", "Ministry of Commerce",
    "Ministry of Commerce and Industry", "Ministry of Commerce Civil Supplies and Co-Operation", "Ministry of Communications",
    "Ministry of Communications and Information Technology", "Ministry of Consumer Affairs Food and Public Distribution", "Ministry of Corporate Affairs",
    "Ministry of Culture", "Ministry of Defence", "Ministry of Education",
    "Ministry of Electronics and Information Technology", "Ministry of Energy", "Ministry of Environment and Forests",
    "Ministry of Environment Forest and Climate Change", "Ministry of External Affairs", "Ministry of Finance",
    "Ministry of Fisheries Animal Husbandry and Dairying", "Ministry of Food Agri. Comm. Dev. and Co-op", "Ministry of Food and Civil Supplies",
    "Ministry of Foreign Trade", "Ministry of Health and Family Welfare", "Ministry of Home Affairs",
    "Ministry of Housing and Urban Affairs", "Ministry of Human Resource Development", "Ministry of Industrial Development",
    "Ministry of Industrial Development and Company Affairs", "Ministry of Industry", "Ministry of Industry and Civil Supplies",
    "Ministry of Industry and Supply", "Ministry of Information and Broadcasting", "Ministry of International Trade",
    "Ministry of Jal Shakti", "Ministry of Labour", "Ministry of Labour and Employment",
    "Ministry of Labour and Rehabilitation", "Ministry of Law", "Ministry of Law and Justice",
    "Ministry of Law Justice and Company Affairs", "Ministry of LawJustice and Company Affairs", "Ministry of Micro Small and Medium Enterprises",
    "Ministry of Mines", "Ministry of Parliamentary Affairs", "Ministry of Personnel Public Grievances and Pension",
    "Ministry of Personnel Public Grievances and Pensions", "Ministry of Petroleum and Chemicals", "Ministry of Petroleum and Natural Gas",
    "Ministry of Petroleum Chemicals and Fertilizers", "Ministry of Ports Shipping and Waterways", "Ministry of Power",
    "Ministry of Railways", "Ministry of Road Transport and Highways", "Ministry of Rural Development",
    "Ministry of Science and Technology", "Ministry of Shipping", "Ministry of Shipping and Transport",
    "Ministry of Shipping Road Transport and Highways", "Ministry of Social Justice and Empowerment", "Ministry of Statistics and Programme Implementation",
    "Ministry of Steel", "Ministry of Steel and Mines", "Ministry of Surface Transport",
    "Ministry of Textiles", "Ministry of Transport", "Ministry of Urban Development",
    "Ministry of Water Resources River Development and Ganga Rejuvenation", "Ministry of Welfare", "Ministry of Women and Child Development",
    "Ministry of Works and Housing", "National Board of Revenue (NBR)", "National Parliament of Bangladesh",
    "Pension Fund Regulatory and Development Authority", "Public Administration", "Reserve Bank of India",
    "Road Transport and Bridges", "Securities and Exchange Board of India", "Telecom Regulatory Authority of India",
]


# ============================================================
# DEPARTMENTS LIST
# ============================================================

DEPARTMENTS_LIST = [
    "Agriculture & Cooperation", "Agriculture", "Agriculture and Cooperation",
    "Agriculture Dairy Development Animal Husbandry and Fisheries", "Animal Husbandry Dairy Development & Fisheries",
    "Backward Classes Welfare", "CBDT", "CBEC Customs", "Central Board of Excise and Customs",
    "Central Board of Indirect Taxes and Customs", "Chemicals and Petrochemicals", "Commerce",
    "Commercial Tax", "Commercial Taxes", "Company Affairs", "Consumer Affairs",
    "Consumer Affairs Food and Civil Supplies", "Co-operation Textiles and Marketing",
    "Department of Banking Operations and Development", "Department of Regulation", "DGFT",
    "Directorate General of Trade Remedies", "Economic Affairs", "Education", "Election",
    "Energy", "Environment Forest Science and Technology", "Exchange Control Department",
    "Excise and Taxation", "Finance", "Financial Markets Regulation Department", "Financial Services",
    "Forest", "General Administration", "Health", "Health and Family Welfare",
    "Health Medical and Family Welfare", "Health Medical and family welfare", "High Court",
    "Higher and Technical Education", "Higher Education", "Home", "Housing and Urban Development",
    "Industrial Development (Patents)", "Industrial Policy and Promotion", "Industries",
    "Industries and Commerce", "Industries Energy and Labour", "Information Technology and Communications",
    "Investment Management Department", "Irrigation and CAD", "Irrigation and Water Resources",
    "Labour", "Labour Employment Training and Factories", "Law", "Law and Judiciary",
    "Legal Affairs Department", "Legislative", "Local Self Government", "Market Regulation Department",
    "Minorities Welfare", "Municipal Administration and Urban Development", "Panchayat Raj and Rural Development",
    "Personnel", "Personnel and Administrative Reforms", "Personnel and Training", "Pharmaceuticals",
    "PIB", "Planning", "Press Information Bureau", "Press Release", "Public Enterprises",
    "Public Health", "Public Works", "RBI", "RBI A.P. DIR (Series)", "Revenue",
    "Revenue and Banking", "Revenue and Forest", "Road Transport and Highways", "Rural Development",
    "School Education", "School Education and Sports", "SEBI", "Social Justice and Special Assistance",
    "Social Welfare", "Telecom Regulatory Authority of India", "Telecommunications", "Transport",
    "Transport Roads and Building", "Tribal Development", "Urban development", "Urban Development",
    "Water Resources", "Water Supply and Sanitation", "Women and Child Development",
    "Women Development Child and Disabled Welfare", "Youth Advancement Tourism & Culture",
    "Youth Advancement Tourism and Culture",
]


# ============================================================
# BAD ADVOCATE NAME PATTERNS
# ============================================================

BAD_NAME_PATTERNS = [
    r"^adv\.?$",
    r"^mr\.?$",
    r"^ms\.?$",
    r"^mrs\.?$",
    r"^dr\.?$",
    r"^nemo\.?$",
    r"^advocates?\.?$",
    r"^advs?\.?$",
    r"^senior panel counsel.*",
    r"^standing counsel.*",
    r"^spc.*",
    r"^cgsc.*",
    r"^aor.*",
]


# ============================================================
# GENERAL HELPER FUNCTIONS
# ============================================================

def clean_text(text: Any) -> str:
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xad", "")
    text = text.replace("\xa0", " ")
    text = text.replace("\t", " ")

    # Repair line-break hyphenation
    text = re.sub(r"(\w+)-\n(\w+)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def normalize_date(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None

    # Strip ordinals
    value = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", value, flags=re.IGNORECASE)
    value = value.replace(",", "").strip()

    formats = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return value


def unique_preserve(values: list[str]) -> list[str]:
    output = []
    seen = set()
    for value in values:
        value = value.strip()
        if not value:
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            output.append(value)
    return output


COMPANY_MARKERS = [
    "M/S",
    "MS.",
    "PRIVATE LIMITED",
    "PVT LTD",
    "LIMITED",
    "LTD",
    "LLP",
    "CORPORATION",
    "COMPANY",
    "CO.",
    "APARTMENTS",
    "PROPERTIES",
    "BUILDERS",
    "DEVELOPERS",
    "ENTERPRISES",
    "TRUST",
    "SOCIETY",
    "ASSOCIATION",
    "FIRM",
    "INFRASTRUCTURE",
    "AUTHORITY",
    "COMMISSION",
]


def infer_party_type(name: str) -> str:
    name_upper = name.upper()

    if any(k in name_upper for k in ["BANK", "HDFC", "ICICI", "SBI", "STATE BANK", "PUNJAB NATIONAL", "CANARA", "AXIS"]):
        return "BANK"

    if any(k in name_upper for k in ["UNION OF INDIA", "GOVT", "GOVERNMENT", "COMMISSIONER OF", "DIRECTORATE OF"]):
        return "GOVERNMENT"

    if any(k in name_upper for k in ["STATE OF", "STATE (", "STATE NCT"]):
        return "STATE"

    if any(marker in name_upper for marker in COMPANY_MARKERS):
        return "COMPANY"

    return "INDIVIDUAL"


def normalize_disposal(raw_value: str | None) -> str | None:
    if not raw_value:
        return None

    normalized = clean_text(raw_value).upper()

    mapping = {
        "DISPOSED OF": "DISPOSED",
        "DISPOSED": "DISPOSED",
        "CLOSED": "CLOSED",
        "DISMISSED": "DISMISSED",
        "ALLOWED": "ALLOWED",
        "PARTLY ALLOWED": "PARTLY_ALLOWED",
        "BAIL GRANTED": "BAIL_GRANTED",
        "BAIL REJECTED": "BAIL_REJECTED",
    }

    return mapping.get(normalized, normalized)


# ============================================================
# CASE METADATA EXTRACTOR
# ============================================================

class MetadataExtractor:
    def __init__(
        self,
        raw_text: str,
        pdf_path: str,
        scraper_record: dict,
        is_scanned: bool = False,
    ):
        self.raw_text = raw_text
        self.text = clean_text(raw_text)
        self.pdf_path = pdf_path
        self.scraper_record = scraper_record
        self.is_scanned = is_scanned
        self.missing_fields: list[str] = []

    # --------------------------------------------------------
    # CNR / Diary Number
    # --------------------------------------------------------

    def extract_cnr(self) -> str | None:
        scraper_cnr = self.scraper_record.get("cnr") or self.scraper_record.get("diary_number")
        if scraper_cnr:
            return str(scraper_cnr).upper()

        patterns = [
            r"(?:CNR\s*(?:No\.?|Number)?|Case\s+CNR|Unique\s+Case\s+ID|Diary\s+No\.?)\s*:?\s*([A-Z0-9\-/]{8,20})",
            r"\b([A-Z]{4}[0-9]{12})\b",
        ]

        for pattern in patterns:
            match = re.search(pattern, self.text, re.IGNORECASE)
            if match:
                return match.group(1).upper()

        self.missing_fields.append("cnr")
        return None

    # --------------------------------------------------------
    # Neutral citation
    # --------------------------------------------------------

    def extract_neutral_citation(self) -> str | None:
        scraper_citation = self.scraper_record.get("neutral_citation")
        if scraper_citation:
            clean_c = re.sub(r"\(English\)", "", str(scraper_citation), flags=re.IGNORECASE).strip()
            clean_c = re.sub(r"\s+", ":", clean_c)
            return clean_c

        patterns = [
            r"Neutral\s+Citation\s*(?:No\.?|Number)?\s*:?\s*([0-9]{4}\s*[:/_-]?\s*INSC\s*[:/_-]?\s*\d+(?:-[A-Z0-9]+)?)",
            r"\b([0-9]{4}\s*:\s*INSC\s*:\s*\d+)\b",
            r"\b([0-9]{4}\s+INSC\s+\d+)\b",
        ]

        for pattern in patterns:
            match = re.search(pattern, self.text, re.IGNORECASE)
            if match:
                citation = match.group(1).strip()
                citation = re.sub(r"[\s/]+", ":", citation)
                return citation

        self.missing_fields.append("neutral_citation")
        return None

    # --------------------------------------------------------
    # Case number
    # --------------------------------------------------------

    def extract_case_number(self) -> dict:
        scraper_case = self.scraper_record.get("case_number")

        patterns = [
            r"\b([A-Z][A-Z0-9.\-()/]*)\s*(?:No\.?\s*)?(\d+(?:-\d+)?\s*/\s*\d{4})\b",
            r"\b([A-Z][A-Z0-9.\-()/]*)\s+(\d+\s+of\s+\d{4})\b",
        ]

        for pattern in patterns:
            match = re.search(pattern, self.text, re.IGNORECASE)
            if not match:
                continue

            type_code = match.group(1).strip()
            raw_number = match.group(2).strip()

            normalized_number = raw_number.replace(" of ", "/")
            parts = normalized_number.split("/")

            try:
                num_str = parts[0].strip().split("-")[0]
                number_value = int(num_str)
            except (ValueError, IndexError):
                number_value = None

            try:
                year_value = int(parts[1].strip())
            except (ValueError, IndexError):
                year_value = None

            type_name = CASE_TYPES.get(type_code, CASE_TYPES.get(type_code.replace(" ", ""), type_code))

            return {
                "display": f"{type_code} {raw_number}",
                "type_code": type_code,
                "type_name": type_name,
                "number": number_value,
                "year": year_value,
            }

        # Fallback to scraper JSON
        if scraper_case:
            display = clean_text(scraper_case)

            match = re.search(r"([A-Za-z.]+)\s*No\.-?(\d+)(?:-\d+)?\s*-\s*(\d{4})", display)
            if match:
                type_code = match.group(1).strip()
                try:
                    number_val = int(match.group(2))
                except ValueError:
                    number_val = None
                try:
                    year_val = int(match.group(3))
                except ValueError:
                    year_val = None

                return {
                    "display": display,
                    "type_code": type_code,
                    "type_name": CASE_TYPES.get(type_code, type_code),
                    "number": number_val,
                    "year": year_val,
                }

            return {
                "display": display,
                "type_code": None,
                "type_name": None,
                "number": None,
                "year": None,
            }

        self.missing_fields.append("case_number")
        return {
            "display": None,
            "type_code": None,
            "type_name": None,
            "number": None,
            "year": None,
        }

    # --------------------------------------------------------
    # Case category
    # --------------------------------------------------------

    def extract_case_category(self, type_code: str | None, type_name: str | None) -> str | None:
        candidates = []
        if type_code and type_code.strip():
            candidates.append(type_code.strip())
        if type_name and type_name.strip():
            candidates.append(type_name.strip())

        scraper_case = self.scraper_record.get("case_number")
        if scraper_case and scraper_case.strip():
            candidates.append(str(scraper_case).strip())

        # Sort keys by length descending to match longer specific prefixes first
        sorted_keys = sorted(CASE_CATEGORY_MAP.keys(), key=lambda k: len(k), reverse=True)

        for cand in candidates:
            cand_upper = cand.upper()
            for key in sorted_keys:
                if key.upper() in cand_upper:
                    return CASE_CATEGORY_MAP[key]

        ref = type_name or type_code or scraper_case
        if ref:
            clean_cat = re.sub(r"[^A-Za-z0-9]", "_", str(ref).upper())
            clean_cat = re.sub(r"_+", "_", clean_cat).strip("_")
            return clean_cat if clean_cat else None

        return None

    def extract_section_snippets(
        self,
        context_before: int = 1,
        context_after: int = 2,
        max_snippets: int = 30,
    ) -> list[str]:
        """
        Extract small context windows around legal provision references.

        Targets:
            under Section ...
            Section ...
            Sections ...
            Sec. ...
            S. ...
            Rule ...
            Rules ...
            Article ...
            Articles ...
            u/s ...

        We intentionally do NOT match the word 'Act' by itself because
        ordinary legal text contains it very frequently.
        """
        raw_lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in self.text.splitlines()
            if line.strip()
        ]

        if not raw_lines:
            return []

        trigger_pattern = re.compile(
            r"""
            \b
            (?:
                under\s+(?:section|sections|sec\.|s\.|rule|rules|article|articles)
                |
                (?:section|sections|sec\.|s\.)\s*
                    \d+[A-Za-z]?(?:\s*(?:\([A-Za-z0-9]+\))*)?
                |
                (?:rule|rules)\s*
                    \d+[A-Za-z]?(?:\s*(?:\([A-Za-z0-9]+\))*)?
                |
                (?:article|articles)\s*
                    \d+[A-Za-z]?(?:\s*(?:\([A-Za-z0-9]+\))*)?
                |
                u/s\s*
                    \d+[A-Za-z]?(?:\s*(?:\([A-Za-z0-9]+\))*)?
            )
            """,
            re.IGNORECASE | re.VERBOSE,
        )

        snippets = []
        seen = set()

        for index, line in enumerate(raw_lines):
            if not trigger_pattern.search(line):
                continue

            start = max(0, index - context_before)
            end = min(len(raw_lines), index + context_after + 1)

            snippet_lines = raw_lines[start:end]
            snippet = " ".join(snippet_lines)
            snippet = clean_text(snippet)

            if len(snippet) < 50:
                continue

            snippet = re.sub(
                r"\b\d+\s+of\s+\d+\b",
                " ",
                snippet,
                flags=re.IGNORECASE,
            )

            snippet = re.sub(r"\s{2,}", " ", snippet).strip()

            key = snippet.casefold()
            if key not in seen:
                seen.add(key)
                snippets.append(snippet)

            if len(snippets) >= max_snippets:
                break

        return snippets

    def generate_case_note_ai(
        self,
        case_category: str | None,
        provisions: list[dict],
        result_text: str | None,
    ) -> str | None:
        section_snippets = self.extract_section_snippets(
            context_before=1,
            context_after=2,
            max_snippets=20,
        )

        prompt_parts = []
        case_number = clean_text(self.scraper_record.get("case_number"))
        party_name = clean_text(self.scraper_record.get("party_name"))
        petitioner = clean_text(self.scraper_record.get("petitioner"))
        respondent = clean_text(self.scraper_record.get("respondent"))

        if case_number:
            prompt_parts.append(f"CASE NUMBER:\n{case_number}")

        if party_name:
            prompt_parts.append(f"PARTIES:\n{party_name}")

        if case_category:
            prompt_parts.append(f"CASE CATEGORY:\n{case_category}")

        provision_strings = [
            f"Section {p['section']} of {p['act_name']}"
            for p in provisions
            if p.get("section") and p.get("act_name")
        ]

        if provision_strings:
            prompt_parts.append(
                "LEGAL PROVISIONS:\n"
                + "\n".join(f"- {item}" for item in unique_preserve(provision_strings))
            )

        if result_text:
            prompt_parts.append(f"DISPOSITION:\n{result_text}")

        if section_snippets:
            prompt_parts.append(
                "RELEVANT LEGAL SECTION CONTEXT:\n"
                + "\n\n".join(
                    f"[Snippet {i}]\n{snippet}"
                    for i, snippet in enumerate(section_snippets, start=1)
                )
            )
        else:
            # Use beginning/main text excerpt from judgment if no specific section snippets
            excerpt = self.text[:2500].strip()
            if excerpt:
                prompt_parts.append(f"JUDGMENT EXCERPT:\n{excerpt}")

        prompt = "\n\n".join(prompt_parts)

        print(f"    [LLM] Requesting CaseNote AI summary...")
        ai_result = call_groq_api(prompt)

        if isinstance(ai_result, dict):
            case_note = ai_result.get("case_note")
            if isinstance(case_note, str) and case_note.strip():
                return clean_text(case_note)

        # High quality fallback summary if LLM API is unavailable
        p_name = party_name or (f"{petitioner} vs {respondent}" if petitioner and respondent else "the parties")
        c_num = case_number or "the petition"
        disp = result_text or "disposed of in accordance with law"
        prov_text = f" with reference to {', '.join(provision_strings[:3])}" if provision_strings else ""

        fallback_note = (
            f"In this matter involving {p_name} ({c_num}), the Supreme Court considered the legal submissions{prov_text}. "
            f"Following examination of the record and relevant statutory principles, the proceeding was {disp}."
        )
        return clean_text(fallback_note)

    def extract_case_age(self, decision_date_str: str | None, case_number_dict: dict) -> dict:
        start_date = None
        latest_judgment_date = decision_date_str

        scraper_reg = self.scraper_record.get("registration_date") or self.scraper_record.get("filing_date")
        if scraper_reg:
            start_date = normalize_date(scraper_reg)

        if not start_date:
            case_year = case_number_dict.get("year")
            if not case_year:
                diary_no = self.scraper_record.get("diary_number")
                if diary_no and "/" in str(diary_no):
                    try:
                        case_year = int(str(diary_no).split("/")[-1])
                    except ValueError:
                        pass

            if case_year and 1900 <= case_year <= 2100:
                start_date = f"{case_year}-01-01"

        age_days = None
        if start_date and latest_judgment_date:
            try:
                dt_start = datetime.strptime(start_date, "%Y-%m-%d")
                dt_end = datetime.strptime(latest_judgment_date, "%Y-%m-%d")
                delta = (dt_end - dt_start).days
                if delta >= 0:
                    age_days = delta
            except ValueError:
                pass

        return {
            "start_date": start_date,
            "latest_judgment_date": latest_judgment_date,
            "age_days": age_days,
        }

    # --------------------------------------------------------
    # Dates
    # --------------------------------------------------------

    def extract_decision_date(self) -> str | None:
        scraper_date = normalize_date(self.scraper_record.get("decision_date"))
        if scraper_date:
            return scraper_date

        patterns = [
            r"(?:Date\s+of\s+Decision|Decision\s+Date|Dated)\s*:?\s*([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})",
            r"(?:Pronounced\s+on|Delivered\s+on|Judgment\s+Delivered\s+on)\s*:?\s*([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})",
        ]

        for pattern in patterns:
            match = re.search(pattern, self.text, re.IGNORECASE)
            if match:
                return normalize_date(match.group(1))

        self.missing_fields.append("decision_date")
        return None

    def extract_pronounced_on(self) -> str | None:
        patterns = [
            r"(?:Pronounced\s+on|Delivered\s+on|Judgment\s+Delivered\s+on)\s*:?\s*([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})",
            r"(?:Pronounced\s+on|Delivered\s+on)\s*:?\s*([0-9]{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+,?\s+[0-9]{4})",
        ]

        for pattern in patterns:
            match = re.search(pattern, self.text, re.IGNORECASE)
            if match:
                return normalize_date(match.group(1))

        return normalize_date(self.scraper_record.get("decision_date"))

    # --------------------------------------------------------
    # Judges
    # --------------------------------------------------------

    def extract_judges(self) -> list[dict]:
        judges = []
        raw_names = []

        # 1. From scraper JSON bench / judge fields
        scraper_bench = clean_text(self.scraper_record.get("bench"))
        scraper_judge = clean_text(self.scraper_record.get("judge"))

        if scraper_bench:
            matches = re.finditer(
                r"(?:HON'BLE\s+)?(?:MR\.|MS\.|MRS\.|DR\.)?\s*JUSTICE\s+([A-Z][A-Z\s\.\'\-]{2,})",
                scraper_bench,
                re.IGNORECASE,
            )
            for m in matches:
                name = m.group(1).strip()
                name = re.sub(r"\s+", " ", name).strip()
                if len(name) >= 3 and name not in raw_names:
                    raw_names.append(name)

        if not raw_names and scraper_judge:
            match = re.search(
                r"(?:HON'BLE\s+)?(?:MR\.|MS\.|MRS\.|DR\.)?\s*JUSTICE\s+([A-Z][A-Z\s\.\'\-]{2,})",
                scraper_judge,
                re.IGNORECASE,
            )
            if match:
                name = match.group(1).strip()
                name = re.sub(r"\s+", " ", name).strip()
                if len(name) >= 3:
                    raw_names.append(name)

        # 2. PDF text extraction fallback
        if not raw_names:
            header_block = self.text[:2500]
            matches = re.finditer(
                r"(?:HON'BLE\s+)?(?:MR\.|MS\.|MRS\.|DR\.)?\s*JUSTICE\s+([A-Z][A-Z\s\.\'\-]{2,})",
                header_block,
                re.IGNORECASE,
            )
            for m in matches:
                raw_name = m.group(1).strip()
                clean_n = re.split(
                    r"\b(SUPREME|COURT|PRESENT|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|DATED|ORDER|JUDGMENT|OF|INDIA|NEW|DELHI)\b|\n",
                    raw_name,
                    flags=re.IGNORECASE,
                )[0].strip(" ,.;:")
                clean_n = re.sub(r"\s+", " ", clean_n).strip()
                if len(clean_n) >= 3 and clean_n not in raw_names:
                    raw_names.append(clean_n)

        for index, name in enumerate(raw_names):
            judge_id = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_")).strip("_")
            judges.append(
                {
                    "judge_id": judge_id or "unknown_judge",
                    "name": name,
                    "role": "PRESIDING" if index == 0 else "COMPANION",
                }
            )

        if not judges:
            self.missing_fields.append("judges")

        return judges

    # --------------------------------------------------------
    # Parties
    # --------------------------------------------------------

    def extract_parties(self) -> list[dict]:
        parties = []

        petitioner = clean_text(self.scraper_record.get("petitioner"))
        respondent = clean_text(self.scraper_record.get("respondent"))

        if petitioner:
            parties.append({
                "name": petitioner,
                "role": "PETITIONER",
                "party_type": infer_party_type(petitioner)
            })

        if respondent:
            parties.append({
                "name": respondent,
                "role": "RESPONDENT",
                "party_type": infer_party_type(respondent)
            })

        if parties:
            return parties

        case_party = clean_text(self.scraper_record.get("party_name"))

        if case_party and re.search(r"\bVs\b|\bVersus\b", case_party, re.IGNORECASE):
            left, right = re.split(r"\s+(?:Vs|Versus)\s+", case_party, maxsplit=1, flags=re.IGNORECASE)
            left = clean_text(left)
            right = clean_text(right)
            if left:
                parties.append({"name": left, "role": "PETITIONER", "party_type": infer_party_type(left)})
            if right:
                parties.append({"name": right, "role": "RESPONDENT", "party_type": infer_party_type(right)})
            return parties

        self.missing_fields.append("parties")
        return parties

    # --------------------------------------------------------
    # Advocates
    # --------------------------------------------------------

    def extract_advocates(self) -> list[dict]:
        advocates = []
        seen = set()

        scraper_adv = clean_text(self.scraper_record.get("advocate"))
        if scraper_adv:
            names = re.split(r",|&|\band\b|\bwith\b", scraper_adv, flags=re.IGNORECASE)
            for raw_name in names:
                name = clean_text(raw_name).strip(" ,.;:")
                if not name or len(name) < 3 or any(re.match(p, name, re.IGNORECASE) for p in BAD_NAME_PATTERNS):
                    continue

                title = None
                title_m = re.match(r"^(SRI\.|SRI|SMT\.|SMT|MR\.|MS\.|MRS\.|DR\.|ADV\.)\s*", name, re.IGNORECASE)
                if title_m:
                    title = title_m.group(1).title()
                    name = name[len(title_m.group(0)):].strip()

                designation = "Advocate"
                if "SENIOR ADVOCATE" in name.upper() or "SR. ADV" in name.upper():
                    designation = "Senior Advocate"
                    name = re.sub(r"SENIOR ADVOCATE|SR\.?\s*ADV\.?", "", name, flags=re.IGNORECASE).strip()

                if name and name.casefold() not in seen:
                    seen.add(name.casefold())
                    advocates.append({
                        "name": name,
                        "title": title,
                        "designation": designation,
                        "for_party_role": "ADVOCATE"
                    })

        # PDF fallback search
        if not advocates:
            blocks = re.findall(r"Through:\s*(.*?)(?=\n\s*(?:versus|CORAM:|\+|\n\n[A-Z]))", self.text, re.DOTALL | re.IGNORECASE)
            roles = ["PETITIONER", "RESPONDENT"]
            for index, block in enumerate(blocks):
                role = roles[index] if index < len(roles) else "OTHER"
                cleaned = re.sub(r"\s+", " ", block)
                cleaned = re.sub(r"Advocates?\s+for.*$", "", cleaned, flags=re.IGNORECASE)
                names = re.split(r",|&|\band\b|\bwith\b", cleaned, flags=re.IGNORECASE)
                for raw_name in names:
                    name = raw_name.strip()
                    if not name or len(name) < 3 or any(re.match(p, name, re.IGNORECASE) for p in BAD_NAME_PATTERNS):
                        continue
                    clean_name = re.sub(r"^(?:Mr\.|Ms\.|Mrs\.|Dr\.|Adv\.|Shri|Smt\.)\s*", "", name, flags=re.IGNORECASE).strip()
                    if len(clean_name) >= 3 and clean_name.casefold() not in seen:
                        seen.add(clean_name.casefold())
                        advocates.append({
                            "name": clean_name,
                            "title": None,
                            "designation": "Advocate",
                            "for_party_role": role,
                        })

        if not advocates:
            self.missing_fields.append("advocates")

        return advocates

    # --------------------------------------------------------
    # Legal provisions & Acts
    # --------------------------------------------------------

    def extract_acts_and_provisions(self) -> tuple[list[dict], list[dict]]:
        provisions = []
        acts = []
        detected_acts = set()

        for alias, full_name in ACT_ALIASES.items():
            if re.search(r"\b" + re.escape(alias) + r"\b", self.text, re.IGNORECASE):
                detected_acts.add(full_name)

        section_patterns = [
            r"(?:Sections?|u/s|Sec\.|S\.)\s*([\dA-Za-z,\-/ &]{1,20})\s+(?:of\s+(?:the\s+)?)?([A-Z][A-Za-z0-9\s]{2,45}?(?:Act|Code|Rules|Sanhita|Adhiniyam))\b",
            r"(?:Sections?|u/s|Sec\.|S\.)\s*([\dA-Za-z,\-/ &]{1,20})\s+(IPC|I\.P\.C|CrPC|Cr\.P\.C|CPC|C\.P\.C|BNS|BNSS|BSA|NI Act)\b",
            r"(?:Articles?|Art\.)\s*([\dA-Za-z,\-/ &]{1,20})\s+(?:of\s+(?:the\s+)?)?(Constitution|Constitution of India)\b",
        ]

        seen_provisions = set()

        for pattern in section_patterns:
            matches = re.findall(pattern, self.text, re.IGNORECASE)
            for section_text, act_text in matches:
                act_clean = clean_text(act_text)
                if not act_clean or "\n" in act_clean or len(act_clean) > 50:
                    continue
                act_key = act_clean.lower()
                act_name = ACT_ALIASES.get(act_key, act_clean) or "General Statute"
                section_parts = re.split(r"[/,&\s]+", section_text)
                for section in section_parts:
                    section = section.strip()
                    if not section or not re.match(r"^\d+[A-Za-z]?$", section):
                        continue
                    pair = (act_name.casefold(), section.casefold())
                    if pair in seen_provisions:
                        continue
                    seen_provisions.add(pair)
                    provisions.append({"act_name": act_name, "section": section})
                    detected_acts.add(act_name)

        for act_name in sorted(detected_acts):
            if not any(act_name.casefold() == existing["act_name"].casefold() for existing in acts):
                acts.append({"act_name": act_name, "short_name": None})

        if not provisions:
            self.missing_fields.append("provisions")

        return acts, provisions

    # --------------------------------------------------------
    # Constitutional articles
    # --------------------------------------------------------

    def extract_articles(self) -> list[str]:
        articles = re.findall(r"\bArticle\s+(\d+[A-Z]?)\b", self.text, re.IGNORECASE)
        return unique_preserve([f"Article {val.upper()}" for val in articles])

    # --------------------------------------------------------
    # Outcome
    # --------------------------------------------------------

    def extract_sections(self, provisions: list[dict]) -> list[str]:
        sections = []
        seen = set()
        for prov in provisions:
            sec = prov.get("section")
            act = prov.get("act_name")
            if sec and act:
                label = f"Section {sec} of {act}"
                if label.casefold() not in seen:
                    seen.add(label.casefold())
                    sections.append(label)
            elif sec:
                label = f"Section {sec}"
                if label.casefold() not in seen:
                    seen.add(label.casefold())
                    sections.append(label)
        return sections

    # --------------------------------------------------------
    # Outcome & Disposition
    # --------------------------------------------------------

    def extract_outcome(self) -> tuple[str | None, str | None, str | None]:
        paragraphs = [p.strip() for p in self.text.split("\n\n") if p.strip()]
        if not paragraphs or len(paragraphs) <= 1:
            paragraphs = [p.strip() for p in self.text.split("\n") if len(p.strip()) > 25]

        tail_text = " ".join(paragraphs[-5:]) if len(paragraphs) >= 5 else self.text
        tail_lower = tail_text.lower()
        full_lower = self.text.lower()

        disposal_nature = None
        raw_disposal = None

        if "partly allowed" in tail_lower or "partially allowed" in tail_lower:
            disposal_nature = "PARTLY_ALLOWED"
        elif "appeal is allowed" in tail_lower or "appeals are allowed" in tail_lower or "petition is allowed" in tail_lower or "appeal stands allowed" in tail_lower:
            disposal_nature = "ALLOWED"
        elif "appeal stands dismissed" in tail_lower or "appeal is dismissed" in tail_lower or "petition is dismissed" in tail_lower or "appeals stand dismissed" in tail_lower or "petition stands dismissed" in tail_lower:
            disposal_nature = "DISMISSED"
        elif "application stands disposed of" in tail_lower or "stands disposed of" in tail_lower or "disposed of" in tail_lower:
            disposal_nature = "DISPOSED"
        elif "bail is granted" in tail_lower or "granted bail" in tail_lower:
            disposal_nature = "BAIL_GRANTED"
        elif "bail application is rejected" in tail_lower or "bail is rejected" in tail_lower:
            disposal_nature = "BAIL_REJECTED"
        elif "appeal is allowed" in full_lower or "petition is allowed" in full_lower:
            disposal_nature = "ALLOWED"
        elif "appeal stands dismissed" in full_lower or "appeal is dismissed" in full_lower or "petition is dismissed" in full_lower:
            disposal_nature = "DISMISSED"
        elif "stands disposed of" in full_lower or "disposed of" in full_lower:
            disposal_nature = "DISPOSED"
        elif "bail is granted" in full_lower:
            disposal_nature = "BAIL_GRANTED"

        result_text = None
        res_patterns = [
            r"(?:the\s+)?(?:appeal|petition|application)s?\s+(?:is|are|stands?)\s+(?:hereby\s+)?(?:allowed|dismissed|disposed of|partly allowed|rejected)[^\.\n]*\.",
            r"(?:impugned order|judgment of the high court)\s+is\s+set\s+aside[^\.\n]*\.",
            r"bail\s+is\s+(?:hereby\s+)?(?:granted|rejected)[^\.\n]*\.",
            r"in the result,?\s+[^\.\n]+\.",
        ]

        for pat in res_patterns:
            m = re.search(pat, tail_text, re.IGNORECASE)
            if m:
                result_text = clean_text(m.group(0))
                break

        if not result_text and disposal_nature:
            readable = {
                "ALLOWED": "Appeal / Petition allowed",
                "DISMISSED": "Petition dismissed",
                "DISPOSED": "Petition / Appeal disposed of",
                "PARTLY_ALLOWED": "Appeal partly allowed",
                "BAIL_GRANTED": "Bail granted",
                "BAIL_REJECTED": "Bail rejected",
            }
            result_text = readable.get(disposal_nature, f"Case {disposal_nature.lower()}")

        if not disposal_nature:
            self.missing_fields.append("outcome")

        return disposal_nature, raw_disposal, result_text

    # --------------------------------------------------------
    # Overruled Detection
    # --------------------------------------------------------

    def extract_overruled(self) -> dict:
        found_keywords = []
        text_lower = self.text.lower()

        phrase_map = [
            ("earlier decision is overruled", "earlier decision is overruled"),
            ("overruled the decision", "overruled the decision"),
            ("judgment overruled", "judgment overruled"),
            ("hereby overruled", "hereby overruled"),
            ("case is overruled", "case is overruled"),
            ("is overruled", "overruled"),
            ("was overruled", "overruled"),
            ("are overruled", "overruled"),
            ("overruled", "overruled"),
        ]

        for phrase, kw in phrase_map:
            if phrase in text_lower:
                if kw not in found_keywords:
                    found_keywords.append(kw)

        return {
            "present": len(found_keywords) > 0,
            "keywords": sorted(found_keywords),
        }

    # --------------------------------------------------------
    # Reporter citations
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Reporter citations & Reporting status
    # --------------------------------------------------------

    def extract_candidate_reporter_citations(self, text_segment: str) -> list[dict]:
        citations = []
        seen = set()

        # 1. Year (Volume) Reporter Page format, e.g. 2025 (1) SCALE 123
        pat1 = r"\b(\d{4})\s*\(\s*(\d+)\s*\)\s*(SCALE|Scale|SCC|SCR|JT)\s+(\d+)\b"
        for match in re.finditer(pat1, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            volume = int(match.group(2))
            reporter_raw = match.group(3).upper()
            page = int(match.group(4))

            full_cit = f"{year} ({volume}) {reporter_raw} {page}"
            key = (reporter_raw, year, volume, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter_raw,
                    "citation": full_cit,
                    "year": year,
                    "volume": volume,
                    "page": page,
                })

        # 2. (Year) Volume Reporter Page format, e.g. (2025) 1 SCC 123
        pat2 = r"\(\s*(\d{4})\s*\)\s*(\d+)\s*(SCC|SCALE|Scale|SCR|JT)\s+(\d+)\b"
        for match in re.finditer(pat2, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            volume = int(match.group(2))
            reporter_raw = match.group(3).upper()
            page = int(match.group(4))

            full_cit = f"({year}) {volume} {reporter_raw} {page}"
            key = (reporter_raw, year, volume, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter_raw,
                    "citation": full_cit,
                    "year": year,
                    "volume": volume,
                    "page": page,
                })

        # 3. AIR Year SC Page, e.g. AIR 2025 SC 123
        pat3 = r"\bAIR\s+(\d{4})\s+SC\s+(\d+)\b"
        for match in re.finditer(pat3, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            page = int(match.group(2))
            full_cit = f"AIR {year} SC {page}"
            key = ("AIR SC", year, None, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": "AIR SC",
                    "citation": full_cit,
                    "year": year,
                    "volume": None,
                    "page": page,
                })

        # 4. SCC OnLine SC format, e.g. 2025 SCC OnLine SC 123
        pat4 = r"\b(\d{4})\s+SCC\s+OnLine\s+SC\s+(\d+)\b"
        for match in re.finditer(pat4, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            page = int(match.group(2))
            full_cit = f"{year} SCC OnLine SC {page}"
            key = ("SCC Online SC", year, None, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": "SCC Online SC",
                    "citation": full_cit,
                    "year": year,
                    "volume": None,
                    "page": page,
                })

        # 5. Simple Year Reporter Page, e.g. 2025 SCC 123 or 2025 SCALE 123
        pat5 = r"\b(\d{4})\s+(SCC|SCR|JT|Scale|SCALE)\s+(\d+)\b"
        for match in re.finditer(pat5, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            reporter_raw = match.group(2).upper()
            page = int(match.group(3))
            full_cit = f"{year} {reporter_raw} {page}"
            key = (reporter_raw, year, None, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter_raw,
                    "citation": full_cit,
                    "year": year,
                    "volume": None,
                    "page": page,
                })

        return citations

    def validate_candidate_citation(self, citation: dict, header_text: str) -> bool:
        cit_str = citation.get("citation", "")
        if not cit_str:
            return False

        # 1. Explicit citation label anywhere in text
        if re.search(r"(?:Reported\s+in|Equivalent\s+Citation|Law\s+Report\s+Citation|Citation)\s*:?\s*" + re.escape(cit_str), self.text, re.IGNORECASE):
            return True

        # Extract party names & tokens
        petitioner = clean_text(self.scraper_record.get("petitioner"))
        respondent = clean_text(self.scraper_record.get("respondent"))
        party_name = clean_text(self.scraper_record.get("party_name"))

        if not petitioner and not respondent and party_name:
            parts = re.split(r"\s+(?:Vs|Versus)\s+", party_name, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                petitioner = clean_text(parts[0])
                respondent = clean_text(parts[1])
            else:
                petitioner = party_name

        case_num = clean_text(self.scraper_record.get("case_number"))
        cnr = clean_text(self.scraper_record.get("cnr") or self.scraper_record.get("diary_number"))

        stop_words = {"THE", "AND", "OTHERS", "ANR", "ORS", "STATE", "INDIA", "UNION", "SUPREME", "COURT", "LIMITED", "LTD", "DEPT", "DEPARTMENT"}
        pet_tokens = [t.upper() for t in re.findall(r"\w+", petitioner) if len(t) > 2 and t.upper() not in stop_words]
        resp_tokens = [t.upper() for t in re.findall(r"\w+", respondent) if len(t) > 2 and t.upper() not in stop_words]

        num_digits = re.findall(r"\d+", case_num) if case_num else []

        # 2. Position & proximity check in header_text
        match_cit = re.search(re.escape(cit_str), header_text, re.IGNORECASE)
        if match_cit:
            pos = match_cit.start()
            window = header_text[max(0, pos - 350): min(len(header_text), pos + 350)].upper()

            if any(t in window for t in pet_tokens):
                return True
            if any(t in window for t in resp_tokens):
                return True
            if case_num and case_num.upper() in window:
                return True
            if cnr and cnr.upper() in window:
                return True
            if num_digits and len(num_digits) >= 2 and all(d in window for d in num_digits):
                return True

        return False

    def extract_reporting_status(self, source_tag: str = "SUPREME_COURT_PDF") -> dict:
        header_end = 1200
        caption_match = re.search(
            r"\n\s*(?:BEFORE|CORAM|PRESENT|Crl\.A|C\.A\.|SLP|IN THE SUPREME COURT|JUDGMENT|ORDER)\b",
            self.text,
            re.IGNORECASE,
        )
        if caption_match and caption_match.start() > 100:
            header_end = min(1500, caption_match.start() + 200)

        header_text = self.text[:header_end]

        explicit_match = re.search(
            r"(?:Reported\s+in|Equivalent\s+Citation|Law\s+Report\s+Citation|Citation)\s*:?\s*([^\n]+)",
            self.text,
            re.IGNORECASE,
        )

        header_candidates = self.extract_candidate_reporter_citations(header_text)
        explicit_candidates = []
        if explicit_match:
            explicit_candidates = self.extract_candidate_reporter_citations(explicit_match.group(0))

        candidates = header_candidates + explicit_candidates

        validated_citations = []
        seen = set()
        for cand in candidates:
            cit_str = cand.get("citation")
            if cit_str and cit_str not in seen:
                if self.validate_candidate_citation(cand, header_text):
                    seen.add(cit_str)
                    validated_citations.append(cand)

        if not self.text or len(self.text.strip()) == 0:
            status = "UNKNOWN"
            is_reported = False
        elif len(validated_citations) > 0:
            status = "DETECTED"
            is_reported = True
        else:
            status = "NOT_DETECTED"
            is_reported = False

        return {
            "status": status,
            "is_reported": is_reported,
            "reporter_citations": validated_citations,
            "source": source_tag,
        }

    def extract_reporter_citations(self) -> list[dict]:
        status = self.extract_reporting_status(source_tag="SUPREME_COURT_PDF")
        return status["reporter_citations"]

    # --------------------------------------------------------
    # Impugned order
    # --------------------------------------------------------

    def extract_impugned_order(self) -> dict:
        match = re.search(r"(?:impugned|challenged)\s+(?:order|judgment)\s+dated\s+([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})", self.text, re.IGNORECASE)
        if match:
            return {"date": normalize_date(match.group(1))}
        return {"date": None}

    # --------------------------------------------------------
    # Catchwords & Keywords
    # --------------------------------------------------------

    def extract_catchwords(self) -> list[str]:
        words = self.text.lower()
        counts = Counter()
        for keyword in LEGAL_KEYWORDS:
            freq = len(re.findall(r"\b" + re.escape(keyword) + r"\b", words))
            if freq > 0:
                counts[keyword] = freq
        return [keyword for keyword, _ in counts.most_common(10)]

    def extract_keywords(self) -> list[str]:
        words = self.text.lower()
        counts = Counter()
        for keyword in LEGAL_KEYWORDS:
            freq = len(re.findall(r"\b" + re.escape(keyword) + r"\b", words))
            if freq > 0:
                counts[keyword] = freq
        return [keyword for keyword, _ in counts.most_common(15)]

    def extract_document_type(self) -> str:
        header_text = self.text[:1500].lower()
        if "notification" in header_text or "circular" in header_text:
            if "state" in header_text:
                return "State Notifications & Circulars"
            return "Central Notifications & Circulars"
        if "bare act" in header_text or "act & rules" in header_text or "rules & regulations" in header_text:
            if "state" in header_text:
                return "State BareActs & Rules"
            return "Central BareActs & Rules"
        if "tribunal" in header_text:
            return "Tribunals"
        if "commission report" in header_text or "ordinance" in header_text or "stamp duty" in header_text:
            return "Comm Reports, Drafts, Stamp Duty, Ordinance"
        if "form" in header_text:
            return "Forms"
        if "business policy" in header_text:
            return "Absence Business Policy"
        if "taxation" in header_text or "corporate law" in header_text:
            return "Corporate Laws & Taxation"
        return "Caselaws"

    # --------------------------------------------------------
    # Industry
    # --------------------------------------------------------

    def extract_industry(self) -> list[str]:
        words = self.text.lower()
        matched = []
        for industry, kws in INDUSTRY_KEYWORDS.items():
            for kw in kws:
                if re.search(r"\b" + re.escape(kw) + r"\b", words):
                    matched.append(industry)
                    break
        return unique_preserve(matched)

    # --------------------------------------------------------
    # Ministry
    # --------------------------------------------------------

    def extract_ministry(self) -> list[str]:
        words = self.text.lower()
        matched = []
        for ministry in MINISTRIES_LIST:
            if re.search(r"\b" + re.escape(ministry.lower()) + r"\b", words):
                matched.append(ministry)
        return unique_preserve(matched)

    # --------------------------------------------------------
    # Department
    # --------------------------------------------------------

    def extract_department(self) -> list[str]:
        words = self.text.lower()
        matched = []
        for dept in DEPARTMENTS_LIST:
            if re.search(r"\b" + re.escape(dept.lower()) + r"\b", words):
                matched.append(dept)
        return unique_preserve(matched)

    # --------------------------------------------------------
    # Judge Bench Strength Category
    # --------------------------------------------------------

    def extract_judge_bench(self, bench_strength: int) -> str:
        if bench_strength >= 5:
            return "5 and more"
        elif bench_strength == 4:
            return "4"
        elif bench_strength == 3:
            return "3"
        elif bench_strength == 2:
            return "2"
        else:
            return "1"

    # --------------------------------------------------------
    # Subject matter
    # --------------------------------------------------------

    def extract_subject_matter(self) -> list[str]:
        words = self.text.lower()
        scores = {}
        MIN_SUBJECT_SCORE = 1

        for subject, keywords in SUBJECT_KEYWORDS.items():
            score = 0
            for keyword in keywords:
                score += len(re.findall(r"\b" + re.escape(keyword) + r"\b", words))
            if score >= MIN_SUBJECT_SCORE:
                scores[subject] = score

        if not scores:
            return []

        maximum = max(scores.values())
        selected = [subj for subj, score in scores.items() if score >= maximum * 0.5 and score >= MIN_SUBJECT_SCORE]
        return sorted(selected)

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    def generate_long_summary(self) -> str | None:
        header_marker = re.search(
            r"\n\s*(?:JUDGMENT|ORDER|ORAL JUDGMENT|O R D E R|J U D G M E N T)\s*\n",
            self.text,
            re.IGNORECASE,
        )

        body_text = self.text[header_marker.end():] if header_marker else self.text

        raw_paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()]
        if not raw_paragraphs or len(raw_paragraphs) <= 1:
            raw_paragraphs = [p.strip() for p in body_text.split("\n") if len(p.strip()) > 30]

        filtered_paragraphs = []
        for p in raw_paragraphs:
            cleaned_p = clean_text(p)
            if len(cleaned_p) < 30:
                continue
            if re.match(
                r"^(?:CORAM|Through|versus|Date of Decision|CM APPL|SUPREME COURT|IN THE SUPREME COURT|Sd/-|\d+\s*$)",
                cleaned_p,
                re.IGNORECASE,
            ):
                continue
            filtered_paragraphs.append(cleaned_p)

        if filtered_paragraphs:
            summary = " ".join(filtered_paragraphs)
            if len(summary) > 2000:
                summary = summary[:2000].rsplit(" ", 1)[0] + "..."
            return summary

        self.missing_fields.append("summary")
        return None

    # --------------------------------------------------------
    # Document Content Info
    # --------------------------------------------------------

    def extract_content(self) -> dict:
        return {
            "language": "en",
            "is_scanned": self.is_scanned,
        }

    # --------------------------------------------------------
    # Extract All
    # --------------------------------------------------------

    def extract(self) -> dict:
        cnr = self.extract_cnr()
        neutral_citation = self.extract_neutral_citation()
        case_number = self.extract_case_number()
        case_category = self.extract_case_category(
            case_number.get("type_code"), case_number.get("type_name")
        )

        decision_date = self.extract_decision_date()
        pronounced_on = self.extract_pronounced_on()
        case_age = self.extract_case_age(decision_date or pronounced_on, case_number)

        judges = self.extract_judges()
        parties = self.extract_parties()
        advocates = self.extract_advocates()
        acts, provisions = self.extract_acts_and_provisions()
        sections = self.extract_sections(provisions)
        articles = self.extract_articles()
        disposal_nature, raw_disposal, result_text = self.extract_outcome()

        overruled = self.extract_overruled()
        reporting_status = self.extract_reporting_status(source_tag="SUPREME_COURT_PDF")
        reporter_citations = reporting_status["reporter_citations"]
        impugned_order = self.extract_impugned_order()
        catchwords = self.extract_catchwords()
        subject_matter = self.extract_subject_matter()
        industry = self.extract_industry()
        ministry = self.extract_ministry()
        department = self.extract_department()
        judge_bench = self.extract_judge_bench(len(judges))
        content = self.extract_content()

        case_note_ai = self.generate_case_note_ai(
            case_category, provisions, result_text
        )

        expected_fields = [
            "cnr",
            "neutral_citation",
            "case_number",
            "decision_date",
            "judges",
            "parties",
            "advocates",
            "provisions",
            "outcome",
        ]

        missing = set(self.missing_fields)
        extracted_count = sum(1 for field in expected_fields if field not in missing)
        confidence = round((extracted_count / len(expected_fields)) * 100, 2)

        doc_type = self.extract_document_type()
        keywords = self.extract_keywords()

        return {
            "cnr": cnr,
            "neutral_citation": neutral_citation,
            "case_number": case_number,
            "case_category": case_category,
            "case_note_ai": case_note_ai,
            "case_age": case_age,
            "document_type": doc_type,
            "dates": {
                "pronounced_on": pronounced_on,
            },
            "coram": {
                "strength": len(judges),
                "judge_bench": judge_bench,
                "judges": judges,
            },
            "parties": parties,
            "counsel": advocates,
            "acts": acts,
            "provisions": provisions,
            "sections": sections,
            "constitutional_articles": articles,
            "outcome": {
                "disposal_nature": disposal_nature,
                "raw_disposal_nature": raw_disposal,
                "result_text": result_text,
            },
            "overruled": overruled,
            "reporting": reporting_status,
            "impugned_order": impugned_order,
            "catchwords": catchwords,
            "keywords": keywords,
            "subject_matter": subject_matter,
            "industry": industry,
            "ministry": ministry,
            "department": department,
            "judge_bench": judge_bench,
            "content": content,
            "confidence_score": confidence,
            "missing_fields": sorted(set(self.missing_fields)),
        }


# ============================================================
# PDF TEXT EXTRACTION
# ============================================================

def is_pdf_scanned(document: fitz.Document, text: str) -> bool:
    if text and len(text.strip()) > 50:
        return False

    for page in document:
        if page.get_images(full=True):
            return True

    return False


def extract_pdf_text(pdf_path: Path) -> tuple[str, bool]:
    document = fitz.open(str(pdf_path))
    text_parts = []
    try:
        for page in document:
            text_parts.append(page.get_text())
        full_text = "\n".join(text_parts)
        is_scanned = is_pdf_scanned(document, full_text)
    finally:
        document.close()

    return full_text, is_scanned


# ============================================================
# FIND PDF PATH
# ============================================================

def resolve_pdf_path(record: dict) -> Path | None:
    raw_path = record.get("pdf_path")
    if raw_path:
        path = Path(str(raw_path))
        if path.exists():
            return path
        candidate = PDF_DIR / path.name
        if candidate.exists():
            return candidate

    diary_no = clean_text(record.get("diary_number"))
    if diary_no:
        safe_diary = diary_no.replace("/", "_")
        for pdf_file in PDF_DIR.glob("*.pdf"):
            if safe_diary in pdf_file.name:
                return pdf_file

    case_num = clean_text(record.get("case_number"))
    if case_num:
        safe_num = re.sub(r"[^\w\-]", "_", case_num)
        for pdf_file in PDF_DIR.glob("*.pdf"):
            if safe_num in pdf_file.name:
                return pdf_file

    return None


# ============================================================
# BUILD STANDARDIZED RECORD
# ============================================================

def build_standardized_record(
    scraper_record: dict,
    pdf_metadata: dict,
) -> dict:
    court_name = "Supreme Court of India"
    cnr = pdf_metadata.get("cnr") or scraper_record.get("diary_number") or ""
    neutral_citation = pdf_metadata.get("neutral_citation") or scraper_record.get("neutral_citation")

    pdf_url = scraper_record.get("pdf_url")
    pdf_path = scraper_record.get("pdf_path")

    decision_date = pdf_metadata.get("dates", {}).get("pronounced_on") or normalize_date(scraper_record.get("decision_date"))

    advocates_list = pdf_metadata.get("advocates", [])
    judges_list = pdf_metadata.get("bench", {}).get("judges", [])
    bench_strength = pdf_metadata.get("bench", {}).get("strength", 0)

    return {
        "schema_version": SCHEMA_VERSION,
        "court": {
            "court_id": "SCIN",
            "name": court_name,
            "court_name": court_name,
            "type": "SUPREME_COURT",
            "state_code": "IN",
            "state_name": "India",
            "bench": "Supreme Court of India",
            "bench_seat": "New Delhi",
        },
        "case": {
            "case_id": cnr,
            "cnr": cnr,
            "case_number": pdf_metadata.get("case_number"),
            "case_category": pdf_metadata.get("case_category"),
            "case_age": pdf_metadata.get("case_age"),
            "date_of_judgment": decision_date,
            "registration_date": pdf_metadata.get("case_age", {}).get("start_date"),
            "decision": {
                "date_of_judgment": decision_date,
                "decision_date": decision_date,
                "pronounced_on": pdf_metadata.get("dates", {}).get("pronounced_on"),
                "disposal_nature": pdf_metadata.get("outcome", {}).get("disposal_nature"),
                "raw_disposal_nature": pdf_metadata.get("outcome", {}).get("raw_disposal_nature"),
                "result_text": pdf_metadata.get("outcome", {}).get("result_text"),
            },
            "neutral_citation": neutral_citation,
            "parties": pdf_metadata.get("parties", []),
            "coram": {
                "bench_strength": bench_strength,
                "judge_bench": pdf_metadata.get("judge_bench"),
                "judges": judges_list,
            },
            "bench": {
                "strength": bench_strength,
                "judge_bench": pdf_metadata.get("judge_bench"),
                "judges": judges_list,
            },
            "counsel": advocates_list,
            "advocates": advocates_list,
        },
        "legal_information": {
            "acts": pdf_metadata.get("acts", []),
            "provisions": pdf_metadata.get("provisions", []),
            "constitutional_articles": pdf_metadata.get("constitutional_articles", []),
            "sections": pdf_metadata.get("sections", []),
            "subject": pdf_metadata.get("subject_matter", []),
            "subject_matter": pdf_metadata.get("subject_matter", []),
            "industry": pdf_metadata.get("industry", []),
            "ministry": pdf_metadata.get("ministry", []),
            "department": pdf_metadata.get("department", []),
        },
        "overruled": pdf_metadata.get("overruled", {"present": False, "keywords": []}),
        "outcome": {
            "disposal_nature": pdf_metadata.get("outcome", {}).get("disposal_nature"),
            "decision": pdf_metadata.get("outcome", {}).get("disposal_nature"),
            "result_text": pdf_metadata.get("outcome", {}).get("result_text"),
            "disposition": {
                "type": pdf_metadata.get("outcome", {}).get("disposal_nature"),
                "result": pdf_metadata.get("outcome", {}).get("result_text"),
            },
        },
        "reporting": pdf_metadata.get(
            "reporting",
            {
                "status": "NOT_DETECTED",
                "is_reported": False,
                "reporter_citations": [],
                "source": "SUPREME_COURT_PDF",
            },
        ),
        "citations": {
            "neutral_citation": neutral_citation,
            "impugned_order": pdf_metadata.get("impugned_order", {}),
        },
        "document": {
            "doc_type": pdf_metadata.get("document_type", "Caselaws"),
            "document_type": pdf_metadata.get("document_type", "Caselaws"),
            "language": pdf_metadata.get("content", {}).get("language", "en"),
            "is_scanned": pdf_metadata.get("content", {}).get("is_scanned", False),
            "case_note_ai": pdf_metadata.get("case_note_ai"),
            "keywords": pdf_metadata.get("keywords", []),
        },
        "processing": {
            "status": "SUCCESS",
            "pdf_found": True,
            "text_extracted": True,
            "error": None,
        },
        "source": {
            "scraper": {
                "serial_no": scraper_record.get("serial_no"),
                "source_page": scraper_record.get("source_page"),
                "pdf_source": scraper_record.get("pdf_url"),
            },
            "pdf": {
                "pdf_url": pdf_url,
                "pdf_path": pdf_path,
                "original_pdf_path": pdf_path,
            },
            "metadata_extraction": {
                "parsed_at": datetime.now().isoformat(),
                "extractor_version": EXTRACTOR_VERSION,
            },
        },
        "quality": {
            "confidence_score": pdf_metadata.get("confidence_score", 0.0),
            "missing_fields": pdf_metadata.get("missing_fields", []),
        },
    }


# ============================================================
# PROCESS SINGLE CASE
# ============================================================

def process_case(scraper_record: dict) -> dict | None:
    pdf_path = resolve_pdf_path(scraper_record)

    if not pdf_path or not pdf_path.exists():
        print(f"  [SKIP] PDF file not found for: {scraper_record.get('case_number')}")
        return None

    print(f"  [PDF] {pdf_path.name}")

    try:
        raw_text, is_scanned = extract_pdf_text(pdf_path)
        extractor = MetadataExtractor(
            raw_text=raw_text,
            pdf_path=str(pdf_path),
            scraper_record=scraper_record,
            is_scanned=is_scanned,
        )
        pdf_metadata = extractor.extract()
        standardized = build_standardized_record(scraper_record, pdf_metadata)
        return standardized
    except Exception as error:
        print(f"  [ERROR] Failed to extract metadata: {error}")
        return None


# ============================================================
# MAIN EXECUTION
# ============================================================

def main():
    print("=" * 80)
    print("SUPREME COURT OF INDIA")
    print("PDF + SCRAPER JSON METADATA EXTRACTOR")
    print("=" * 80)

    if not INPUT_JSON.exists():
        print(f"\nInput JSON not found:\n{INPUT_JSON}")
        return

    if not PDF_DIR.exists():
        print(f"\nPDF folder not found:\n{PDF_DIR}")
        return

    with open(INPUT_JSON, "r", encoding="utf-8") as file:
        scraper_data = json.load(file)

    if not isinstance(scraper_data, dict):
        print('\nERROR: Expected scraper JSON format: {"Supreme Court of India": [ ... ]}')
        return

    all_records = []
    total_records = 0
    skipped_records = 0

    for court_name, records in scraper_data.items():
        print(f"\nCourt group: {court_name}")

        if not isinstance(records, list):
            continue

        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                continue

            total_records += 1
            print("\n" + "-" * 70)
            print(f"Record {index}/{len(records)}")
            print(f"Case: {record.get('case_number')}")
            print(f"Diary No: {record.get('diary_number')}")

            standardized = process_case(record)
            if standardized is None:
                skipped_records += 1
                continue

            all_records.append(standardized)
            print("  [OK] Metadata created")
            print(f"  Confidence: {standardized['quality']['confidence_score']}%")

    output = {
        "Supreme Court of India": all_records
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as file:
        json.dump(output, file, indent=4, ensure_ascii=False)

    print("\n" + "=" * 80)
    print("COMPLETED METADATA EXTRACTION")
    print("=" * 80)
    print(f"Total scraper records : {total_records}")
    print(f"Standardized records  : {len(all_records)}")
    print(f"Skipped records       : {skipped_records}")
    print(f"Output Metadata JSON  : {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
