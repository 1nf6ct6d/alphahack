import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    token = os.environ.get("ALFA_SMOKE_TOKEN", "demo-test-a")
    other = os.environ.get("ALFA_SMOKE_OTHER_TOKEN", "demo-test-b")

    def call(path, *, method="GET", body=None, credential=token, expected=200):
        headers = {"Authorization": "Bearer " + credential}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(base + path, headers=headers, data=data, method=method)
        try:
            with urlopen(request, timeout=70) as response:
                status = response.status
                payload = json.loads(response.read())
        except HTTPError as error:
            status = error.code
            payload = json.loads(error.read())
        if status != expected:
            raise RuntimeError(f"{method} {path}: HTTP {status}, expected {expected}; code={payload.get('code')}")
        print(f"OK {method} {path} [{status}]")
        return payload

    call("/health/ready")
    companies = call("/companies")["items"]
    if [item["id"] for item in companies] != ["U_5639"]:
        raise RuntimeError("Smoke expects the isolated synthetic TEST_A dataset")
    call("/companies/U_5639")
    call("/companies/U_5639", credential=other, expected=404)
    overview = call("/companies/U_5639/overview/refresh", method="POST", body={"analytic_focus": "CFO"})
    if not overview["items"]:
        raise RuntimeError("Synthetic overview has no candidates")
    candidate = overview["items"][0]
    insight = call("/companies/U_5639/insights", method="POST", body={"analytic_focus": "CFO", "event_id": candidate["event_id"]})
    path = "/insights/" + insight["insight_id"]
    repeated = call("/companies/U_5639/insights", method="POST", body={"analytic_focus": "CFO", "event_id": candidate["event_id"]})
    if repeated["insight_id"] != insight["insight_id"]:
        raise RuntimeError("Insight creation is not idempotent")
    call(path)
    call(path, credential=other, expected=404)
    answer = call(path + "/questions", method="POST", body={"question": "На каких признаках основан вывод?"})
    if not answer["evidence_ids"]:
        raise RuntimeError("Answer has no evidence references")
    call(path + "/feedback", method="PUT", body={"rating": "useful", "reason": "relevant"})
    print("PASS: full synthetic HTTP scenario, tenant isolation and repeated insight.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, URLError, ValueError) as error:
        raise SystemExit(str(error)) from error
