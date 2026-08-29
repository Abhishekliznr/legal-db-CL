from court_manager import run_court_scraper


def main():
    print("=" * 70)
    print("HIGH COURT JUDGMENT SCRAPER")
    print("=" * 70)

    court_name = input(
        "\nEnter High Court name: "
    ).strip()

    from_date = input(
        "Enter starting date (YYYY-MM-DD): "
    ).strip()

    to_date = input(
        "Enter ending date (YYYY-MM-DD): "
    ).strip()

    run_court_scraper(
        court_name=court_name,
        from_date=from_date,
        to_date=to_date,
    )


if __name__ == "__main__":
    main()