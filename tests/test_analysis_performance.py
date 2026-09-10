"""Offline checks for cache isolation and truthful incomplete-check reporting."""
import asyncio
from pathlib import Path

import pytest
from agents.attachments import attachment_scope, encode_attachment
from agents.base import BadModelReply, ModelUnavailable
from agents.verifier import VerifierAgent
from services.analysis import AnalysisPipeline, _Run
from app.models import Biomarker


def test_encoding_read_once_per_upload(tmp_path, monkeypatch):
    path = tmp_path / "report.png"
    path.write_bytes(b"fixture")
    original = Path.read_bytes
    reads = []
    def read(p):
        reads.append(p)
        return original(p)
    monkeypatch.setattr(Path, "read_bytes", read)
    with attachment_scope():
        first = encode_attachment(str(path), "image/png")
        assert encode_attachment(str(path), "image/png") is first
    with attachment_scope():
        assert encode_attachment(str(path), "image/png") is not first
    assert len(reads) == 2


def test_encoding_scope_resets_after_error(tmp_path):
    path = tmp_path / "report.png"
    path.write_bytes(b"fixture")
    try:
        with attachment_scope():
            first = encode_attachment(str(path), "image/png")
            raise ValueError
    except ValueError:
        pass
    assert encode_attachment(str(path), "image/png") is not first


def test_concurrent_upload_caches_are_separate(tmp_path):
    path = tmp_path / "report.png"
    path.write_bytes(b"fixture")
    async def work():
        with attachment_scope():
            value = encode_attachment(str(path), "image/png")
            await asyncio.sleep(0)
            assert encode_attachment(str(path), "image/png") is value
            return value
    async def check():
        a, b = await asyncio.gather(work(), work())
        assert a is not b
    asyncio.get_event_loop().run_until_complete(check())


def test_unavailable_verifier_flags_values():
    class BrokenVerifier:
        async def run(self, *a, **kw):
            raise ModelUnavailable("timeout")
    pipeline = AnalysisPipeline()
    pipeline.verifier = BrokenVerifier()
    r = _Run(user_id="synthetic", user={"preferences": {"plan": "pro"}}, files=[], member_id="self")
    r.biomarkers = [Biomarker(name="Glucose", value="90", unit="mg/dL", ref_range="70-100", status="normal", category="Blood", explanation="", tip="")]
    asyncio.get_event_loop().run_until_complete(pipeline._verify(r))
    assert r.verification_status == "unavailable"
    assert r.biomarkers[0].needs_verification
    assert "did not complete" in r.biomarkers[0].verification_note


@pytest.mark.parametrize("reply", [{}, {"mismatches": None}, {"mismatches": [{}]}])
def test_malformed_verification_is_not_clean(reply):
    class Client:
        async def call(self, **kw):
            return reply
    verifier = VerifierAgent(client=Client())
    with pytest.raises(BadModelReply):
        asyncio.get_event_loop().run_until_complete(verifier.run([], []))


def test_transcription_failure_keeps_warning_out_of_lab_notes():
    class Client:
        async def call(self, **kw):
            return {"unexpected": "shape"}
    from app.models import AnalyzeFileIn
    from app.enums import ReportType
    pipeline = AnalysisPipeline()
    pipeline.transcriber._client = Client()
    r = _Run(user_id="synthetic", user={}, files=[AnalyzeFileIn(filename="x.png", mime_type="image/png", file_base64="eA==")], member_id="self")
    r.raws = [b"x"]
    r.report_type = ReportType.LAB
    asyncio.get_event_loop().run_until_complete(pipeline._transcribe(r))
    report = pipeline._assemble(r)
    assert "missing results" in report.overall_assessment
    assert report.lab_notes == []
