"""Tests for automatic clipping enqueue client."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from input_service import clipping_client  # noqa: E402


class ClippingClientTests(unittest.TestCase):
    def test_enqueue_success_202(self):
        body = {
            "success": True,
            "job_id": "job_20260520T000000Z_abcd1234",
            "status": "queued",
            "status_url": "/jobs/job_20260520T000000Z_abcd1234",
            "outputs_url": "/jobs/job_20260520T000000Z_abcd1234/outputs",
        }
        resp = mock.MagicMock()
        resp.status = 202
        resp.read.return_value = json.dumps(body).encode("utf-8")
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)

        with mock.patch.dict(os.environ, {"CLIPPING_AUTO_ENQUEUE": "1"}, clear=False):
            with mock.patch("input_service.clipping_client.urlrequest.urlopen", return_value=resp):
                result = clipping_client.enqueue_clipping_job(
                    input_id="input_test_001",
                    funnel_id="business_podcasts_001",
                    pipeline_profile="business_podcasts_001",
                )

        self.assertTrue(result["success"])
        self.assertEqual(result["job_id"], "job_20260520T000000Z_abcd1234")
        self.assertEqual(result["status"], "queued")

    def test_enqueue_disabled(self):
        with mock.patch.dict(os.environ, {"CLIPPING_AUTO_ENQUEUE": "0"}, clear=False):
            result = clipping_client.enqueue_clipping_job(
                input_id="input_test_001",
                funnel_id="business_podcasts_001",
                pipeline_profile="business_podcasts_001",
            )
        self.assertFalse(result["success"])
        self.assertTrue(result.get("skipped"))

    def test_enqueue_http_error(self):
        err_body = json.dumps({"error": "Input video not found"}).encode("utf-8")
        http_err = urlerror.HTTPError(
            "http://127.0.0.1:5050/jobs",
            400,
            "Bad Request",
            hdrs=None,
            fp=io.BytesIO(err_body),
        )
        with mock.patch.dict(os.environ, {"CLIPPING_AUTO_ENQUEUE": "1"}, clear=False):
            with mock.patch(
                "input_service.clipping_client.urlrequest.urlopen",
                side_effect=http_err,
            ):
                result = clipping_client.enqueue_clipping_job(
                    input_id="input_test_001",
                    funnel_id="business_podcasts_001",
                    pipeline_profile="business_podcasts_001",
                )
        self.assertFalse(result["success"])
        self.assertEqual(result.get("http_status"), 400)


if __name__ == "__main__":
    unittest.main()
