from dotenv import load_dotenv

from firewall.account_data import fetch_open_orders


def main() -> None:
    load_dotenv()
    result = fetch_open_orders({})
    print(f"ok={result.ok}")
    print(f"reason={result.reason}")
    print(f"open_order_count={len(result.orders)}")
    print(f"aggregate_outstanding_notional={result.aggregate_outstanding_notional}")
    for order in result.orders:
        print(order)


if __name__ == "__main__":
    main()
