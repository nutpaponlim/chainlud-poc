import csv
import json

MAX_ROWS_TO_SEND = 50
MAX_CHARS_TO_SEND = 40_000
MAX_COLS_TO_SHOW = 80

def csv_to_agent_payload(csv_path: str, filename: str) -> str:
    rows = []
    total_rows = 0

    with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        fieldnames = reader.fieldnames or []
        # cap columns shown in summary if extremely wide
        shown_fieldnames = fieldnames[:MAX_COLS_TO_SHOW]
        truncated_cols = len(fieldnames) - len(shown_fieldnames)

        non_empty_counts = {c: 0 for c in shown_fieldnames}

        for row in reader:
            total_rows += 1

            # count non-empty only for shown columns (keeps it cheap)
            for c in shown_fieldnames:
                val = row.get(c)
                if isinstance(val, str):
                    val = val.strip()
                if val not in (None, "", []):
                    non_empty_counts[c] += 1

            if len(rows) < MAX_ROWS_TO_SEND:
                # keep only shown columns in sample rows to avoid huge width
                rows.append({c: row.get(c) for c in shown_fieldnames})

    summary = {
        "filename": filename,
        "total_rows": total_rows,
        "columns_shown": shown_fieldnames,
        "columns_total": len(fieldnames),
        "columns_truncated": max(0, truncated_cols),
        "non_empty_counts": non_empty_counts,
        "sample_rows_sent": len(rows),
        "note": f"Only first {MAX_ROWS_TO_SEND} rows are included to limit size."
    }

    payload_obj = {"csv_summary": summary, "csv_sample_rows": rows}
    payload_text = json.dumps(payload_obj, ensure_ascii=False, indent=2)

    # If too big, drop sample rows entirely (best fallback)
    if len(payload_text) > MAX_CHARS_TO_SEND:
        payload_obj = {"csv_summary": summary, "csv_sample_rows": [], "note": "Sample rows omitted due to size limits."}
        payload_text = json.dumps(payload_obj, ensure_ascii=False, indent=2)

    # Still too big? send just summary
    if len(payload_text) > MAX_CHARS_TO_SEND:
        payload_obj = {"csv_summary": summary, "note": "Payload trimmed to summary only due to size limits."}
        payload_text = json.dumps(payload_obj, ensure_ascii=False, indent=2)

    # Absolute last resort: truncate text (rare now)
    if len(payload_text) > MAX_CHARS_TO_SEND:
        payload_text = payload_text[:MAX_CHARS_TO_SEND] + "\n...[TRUNCATED]"

    return "CSV parsed (summary + sample):\n" + payload_text
