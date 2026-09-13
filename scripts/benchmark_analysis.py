"""Opt-in synthetic AI-stage benchmark. No database, storage or patient data.

Run from the backend: ./venv/bin/python -m scripts.benchmark_analysis --live
Makes billable calls using configured Gemini credentials. One pair is a smoke
comparison, not a representative latency/clinical-accuracy benchmark.
"""
import argparse
import asyncio
import base64
import json
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from agents.base import ModelClient
from agents.performance import CallPolicy
from app.models import AnalyzeFileIn
from services.analysis import AnalysisPipeline
from services.analysis_types import AnalysisResult

ROWS = [
    ("Haemoglobin", "11.4", "g/dL", "13.0 - 17.0"),
    ("Platelet Count", "240000", "/uL", "150000 - 410000"),
    ("White Blood Cell Count", "7200", "/uL", "4000 - 11000"),
    ("Neutrophils", "60", "%", "40 - 75"),
    ("Lymphocytes", "30", "%", "20 - 45"),
    ("Eosinophils", "3", "%", "1 - 6"),
    ("Fasting Glucose", "106", "mg/dL", "70 - 100"),
    ("Creatinine", "0.9", "mg/dL", "0.7 - 1.3"),
    ("Total Cholesterol", "210", "mg/dL", "< 200"),
    ("Triglycerides", "140", "mg/dL", "< 150"),
    ("Sodium", "139", "mmol/L", "135 - 145"),
    ("Potassium", "4.2", "mmol/L", "3.5 - 5.1"),
]


def fixture(directory):
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 28)
    except OSError:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 28)
        except OSError:
            font = ImageFont.load_default(size=28)
    files = []
    for page in range(2):
        img = Image.new("RGB", (1700, 1050), "white")
        draw = ImageDraw.Draw(img)
        for y, text in [(60, "Folin Test Laboratory - Laboratory Results"),
                        (120, "Patient: Sample Person    Age: 35    Sex: Male"),
                        (175, f"Report date: 2026-09-01    Sample: ABC-123    Page {page + 1} of 2")]:
            draw.text((45, y), text, fill="black", font=font)
        for x, title in zip((45, 650, 900, 1150), ("Test", "Result", "Unit", "Reference range")):
            draw.text((x, 270), title, fill="black", font=font)
        for i, row in enumerate(ROWS[page * 6:(page + 1) * 6]):
            y = 350 + i * 90
            draw.line((40, y - 15, 1650, y - 15), fill="#cccccc")
            for x, value in zip((45, 650, 900, 1150), row):
                draw.text((x, y), value, fill="black", font=font)
        path = Path(directory) / f"page-{page + 1}.png"
        img.save(path)
        files.append(AnalyzeFileIn(filename=path.name, mime_type="image/png",
                                   file_base64=base64.b64encode(path.read_bytes()).decode()))
    return files


class MeasuredClient(ModelClient):
    def __init__(self, baseline):
        super().__init__()
        self.baseline = baseline
        self.measurements = []

    def policy_for(self, meta):
        if self.baseline:
            return CallPolicy("high", 200, 120, 4, 20)
        return super().policy_for(meta)

    async def _log(self, meta, request, model, response_raw, status, error, started):
        if meta:
            meta.model = model
        self.measurements.append({"stage": str(meta.kind) if meta else None, "model": model,
                                  "seconds": round(time.monotonic() - started, 3), "status": status,
                                  "thinking": request.get("thinking_level"),
                                  "usage": request.get("usage", {}),
                                  "failed_attempts": request.get("attempts", [])})


class BenchmarkPipeline(AnalysisPipeline):
    def __init__(self):
        # Measure the full set of quality checks without enqueueing durable jobs.
        super().__init__(defer_verification=False)

    async def _context(self, r):
        r.display_name = "Sample Person"
        r.profile_context = self.extractor.profile_context(r.display_name, {"age": 35, "sex": "male"})

    async def _persist(self, r, report):
        self.checks = {"transcription": r.transcription_status, "verification": r.verification_status}
        return AnalysisResult("synthetic", report.model_dump(), 0, [], {})


def normalize(value):
    return re.sub(r"\s+", "", str(value).lower().replace("µ", "u").replace("μ", "u").replace("–", "-"))


def score(markers):
    actual = {normalize(b["name"]): b for b in markers}
    matches = {key: 0 for key in ("name", "value", "unit", "ref_range")}
    missing = []
    for name, value, unit, ref_range in ROWS:
        row = actual.get(normalize(name))
        if row is None:
            missing.append(name)
            continue
        matches["name"] += 1
        for key, expected in (("value", value), ("unit", unit), ("ref_range", ref_range)):
            matches[key] += normalize(row.get(key)) == normalize(expected)
    return {"expected_rows": len(ROWS), "returned_rows": len(markers), "exact_field_matches": matches,
            "missing_exact_names": missing,
            "flagged_rows": [b["name"] for b in markers if b.get("needs_verification")]}


async def measure(files, baseline):
    client = MeasuredClient(baseline)
    pipeline = BenchmarkPipeline()
    for agent in (pipeline.screener, pipeline.transcriber, pipeline.extractor, pipeline.verifier):
        agent._client = client
    started = time.monotonic()
    result = {"configuration": "previous_high_effort" if baseline else "optimized"}
    try:
        async with asyncio.timeout(200):
            report = await pipeline.run(files, "synthetic", {"preferences": {"plan": "pro"}}, None)
        result.update(status="success", extraction=score(report.report["biomarkers"]), checks=pipeline.checks)
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__)
    finally:
        result.update(seconds=round(time.monotonic() - started, 3), stages=client.measurements)
        await client.close()
    print(json.dumps(result), flush=True)
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="authorize billable synthetic Gemini calls")
    parser.add_argument("--output", help="JSON destination; defaults to a new timestamped benchmark file")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this benchmark makes billable provider calls")
    with tempfile.TemporaryDirectory(prefix="folin-benchmark-") as directory:
        files = fixture(directory)
        results = [await measure(files, baseline) for baseline in (False, True)]
    output = {"created_at": datetime.now(timezone.utc).isoformat(), "synthetic": True,
              "scope": "One two-page fixture; AI stages only; no DB/storage. Both use shared connections and encoding cache. Exact-name matching may undercount aliases. Not clinical validation.",
              "results": results}
    path = Path(args.output or ("docs/benchmarks/analysis-speed-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
