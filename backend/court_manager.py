from pathlib import Path
import importlib.util


# ============================================================
# PROJECT PATH
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR / "app"


# ============================================================
# COURT FILE MAPPING
# ============================================================

COURT_FILES = {
    # Supreme Court
    "supreme court": "supreme_court.py",
    "supreme court of india": "supreme_court.py",
    "sci": "supreme_court.py",

    # Allahabad
    "allahabad high court": "allahabad_high_court.py",
    "high court of allahabad": "allahabad_high_court.py",

    # Andhra Pradesh
    "andhra pradesh high court": "andhra_pradesh_high_court.py",
    "high court of andhra pradesh": "andhra_pradesh_high_court.py",

    # Bombay
    "bombay high court": "bombay_high_court.py",
    "high court of bombay": "bombay_high_court.py",

    # Chhattisgarh
    "chhattisgarh high court": "chhattisgarh_high_court.py",
    "high court of chhattisgarh": "chhattisgarh_high_court.py",

    # Calcutta
    "calcutta high court": "culcutta_high_court.py",
    "culcutta high court": "culcutta_high_court.py",
    "high court of calcutta": "culcutta_high_court.py",

    # Delhi
    "delhi high court": "delhi_high_court.py",
    "high court of delhi": "delhi_high_court.py",

    # Gauhati
    "gauhati high court": "gauhati_high_court.py",
    "high court of gauhati": "gauhati_high_court.py",

    # Gujarat
    "gujarat high court": "gujrat_high_court.py",
    "gujrat high court": "gujrat_high_court.py",
    "high court of gujarat": "gujrat_high_court.py",

    # Himachal Pradesh
    "himachal pradesh high court": "himachal_pradesh_high_court.py",
    "high court of himachal pradesh": "himachal_pradesh_high_court.py",

    # Jammu Kashmir Ladakh
    "jammu kashmir ladakh high court": "jammu_kashmir_ladakh_high_court.py",
    "jammu and kashmir ladakh high court": "jammu_kashmir_ladakh_high_court.py",
    "high court of jammu and kashmir and ladakh":
        "jammu_kashmir_ladakh_high_court.py",

    # Jharkhand
    "jharkhand high court": "jharkhand_high_court.py",
    "high court of jharkhand": "jharkhand_high_court.py",

    # Karnataka
    "karnataka high court": "karnataka_high_court.py",
    "high court of karnataka": "karnataka_high_court.py",

    # Kerala
    "kerala high court": "kerala_high_court.py",
    "high court of kerala": "kerala_high_court.py",

    # Madhya Pradesh
    "madhya pradesh high court": "madhya_pradesh_high_court.py",
    "high court of madhya pradesh": "madhya_pradesh_high_court.py",

    # Madras
    "madras high court": "madras_high_court.py",
    "high court of madras": "madras_high_court.py",

    # Manipur
    "manipur high court": "manipur_high_court.py",
    "high court of manipur": "manipur_high_court.py",

    # Meghalaya
    "meghalaya high court": "meghalaya_high_court.py",
    "high court of meghalaya": "meghalaya_high_court.py",

    # Orissa
    "orissa high court": "orissa_high_court.py",
    "odisha high court": "orissa_high_court.py",
    "high court of orissa": "orissa_high_court.py",

    # Patna
    "patna high court": "patna_high_court.py",
    "high court of patna": "patna_high_court.py",

    # Punjab and Haryana
    "punjab haryana high court": "punjab_haryana_high_court.py",
    "punjab and haryana high court": "punjab_haryana_high_court.py",
    "high court of punjab and haryana": "punjab_haryana_high_court.py",

    # Rajasthan
    "rajasthan high court": "rajasthan_high_court.py",
    "high court of rajasthan": "rajasthan_high_court.py",

    # Sikkim
    "sikkim high court": "sikkim_high_court.py",
    "high court of sikkim": "sikkim_high_court.py",

    # Telangana
    "telangana high court": "telangana_high_court.py",
    "high court for the state of telangana":
        "telangana_high_court.py",
    "high court of telangana": "telangana_high_court.py",

    # Tripura
    "tripura high court": "tripura_high_court.py",
    "high court of tripura": "tripura_high_court.py",

    # Uttarakhand
    "uttarakhand high court": "utarakhand_high_court.py",
    "high court of uttarakhand": "utarakhand_high_court.py",

    # Common spelling mistake
    "utarakhand high court": "utarakhand_high_court.py",
}


# ============================================================
# FIND SCRAPER FILE
# ============================================================

def find_scraper_file(filename):
    """
    Search recursively inside app/ for the requested Python file.

    This avoids depending on exact folder names.
    """

    matches = list(APP_DIR.rglob(filename))

    if not matches:
        return None

    return matches[0]


# ============================================================
# LOAD SCRAPER MODULE
# ============================================================

def load_scraper(filename):
    """
    Dynamically import the requested scraper file.
    """

    file_path = find_scraper_file(filename)

    if file_path is None:
        raise FileNotFoundError(
            f"Scraper file not found inside app/: {filename}"
        )

    module_name = (
        file_path.stem
        + "_dynamic"
    )

    spec = importlib.util.spec_from_file_location(
        module_name,
        file_path,
    )

    if spec is None or spec.loader is None:
        raise ImportError(
            f"Could not create import specification for: {file_path}"
        )

    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    if not hasattr(module, "run_scraper"):
        raise AttributeError(
            f"{file_path} does not contain run_scraper()."
        )

    return module


# ============================================================
# RUN COURT SCRAPER
# ============================================================

def run_court_scraper(
    court_name,
    from_date,
    to_date,
):
    """
    Find and execute the correct court scraper.
    """

    key = (
        court_name
        .strip()
        .lower()
    )

    filename = COURT_FILES.get(key)

    if filename is None:
        print(
            f"\nCourt not found: {court_name}"
        )

        print(
            "\nSupported High Courts:"
        )

        main_names = [
            "Supreme Court of India",
            "Allahabad High Court",
            "Andhra Pradesh High Court",
            "Bombay High Court",
            "Chhattisgarh High Court",
            "Calcutta High Court",
            "Delhi High Court",
            "Gauhati High Court",
            "Gujarat High Court",
            "Himachal Pradesh High Court",
            "Jammu and Kashmir and Ladakh High Court",
            "Jharkhand High Court",
            "Karnataka High Court",
            "Kerala High Court",
            "Madhya Pradesh High Court",
            "Madras High Court",
            "Manipur High Court",
            "Meghalaya High Court",
            "Orissa High Court",
            "Patna High Court",
            "Punjab and Haryana High Court",
            "Rajasthan High Court",
            "Sikkim High Court",
            "Telangana High Court",
            "Tripura High Court",
            "Uttarakhand High Court",
        ]

        for name in main_names:
            print(f" - {name}")

        return

    print("\n" + "=" * 80)
    print("HIGH COURT SCRAPER CONTROLLER")
    print("=" * 80)
    print(f"COURT : {court_name}")
    print(f"FILE  : {filename}")
    print(f"FROM  : {from_date}")
    print(f"TO    : {to_date}")
    print("=" * 80)

    try:
        scraper_module = load_scraper(filename)

        print(
            f"\nLoaded scraper:"
            f"\n{find_scraper_file(filename)}"
        )

        scraper_module.run_scraper(
            from_date=from_date,
            to_date=to_date,
        )

    except TypeError as error:
        print(
            "\nERROR: This scraper's run_scraper() "
            "does not accept from_date and to_date."
        )
        print(f"Details: {error}")

    except Exception as error:
        print(
            f"\nERROR while running {court_name}:"
        )
        print(repr(error))