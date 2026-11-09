#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit & integration tests for BNI Gingui weekly updater and verification gate."""

from __future__ import annotations

import datetime
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import gingui_weekly_updater as updater  # noqa: E402


class GinguiWeeklyUpdaterTests(unittest.TestCase):
    def test_compute_target_friday(self) -> None:
        # Friday 06:00 (before meeting start) -> same day Friday
        dt_before = datetime.datetime.fromisoformat("2026-11-06T06:00:00+08:00")
        self.assertEqual(updater.compute_target_friday(dt_before).isoformat(), "2026-11-06")

        # Friday 12:00 (weekly schedule slot after morning meeting) -> next Friday (+7 days)
        dt_noon = datetime.datetime.fromisoformat("2026-11-06T12:00:00+08:00")
        self.assertEqual(updater.compute_target_friday(dt_noon).isoformat(), "2026-11-13")

        # Monday -> coming Friday
        dt_mon = datetime.datetime.fromisoformat("2026-11-09T09:30:00+08:00")
        self.assertEqual(updater.compute_target_friday(dt_mon).isoformat(), "2026-11-13")

    def test_validate_source_payload_rejects_invalid_and_accepts_valid(self) -> None:
        bad = {"meeting_date": "2026-11-09", "speaker_name": "測試"}  # Monday, missing fields
        errs = updater.validate_source_payload(bad)
        self.assertTrue(any("不是星期五" in e for e in errs))
        self.assertTrue(any("缺少必要欄位" in e for e in errs))

        good = {
            "meeting_date": "2026-11-13",
            "speaker_name": "測試講者",
            "profession": "專業顧問業代表",
            "company": "測試股份有限公司",
            "topic_headline": "把流程做對，讓每一次引薦都算數",
            "topic_svg_line1": "把流程做對",
            "topic_svg_line2": "讓每一次引薦都算數",
            "topic_paragraphs": ["從真實合作案例出發，拆解跨產業協作三個關鍵節點。"],
            "partners": "會計師、律師、品牌設計",
            "ideal_referral": "準備擴編團隊的中小企業主",
            "dream_referral": "連鎖體系總部決策者",
            "reel_url": "https://www.facebook.com/reel/123456789012345/",
            "source_post_url": "https://www.facebook.com/reel/123456789012345/",
            "fetched_at": "2026-11-09T10:00:00+08:00",
        }
        self.assertEqual(updater.validate_source_payload(good), [])

    def test_close_expired_blocks_false_success_and_speaker_update_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            docs_copy = tmp_path / "docs"
            content_copy = tmp_path / "content"
            shutil.copytree(REPO_ROOT / "docs", docs_copy)
            content_copy.mkdir(parents=True, exist_ok=True)

            ref_dt = datetime.datetime.fromisoformat("2026-11-09T10:00:00+08:00")
            target_friday = datetime.date(2026, 11, 13)

            # 1. Verify close_expired state passes local hygiene but DOES NOT pass upcoming_speaker_updated
            updater.apply_close_expired(docs_copy, "2026-11-09")
            status_closed = updater.verify_site_status(
                docs_dir=docs_copy,
                content_dir=content_copy,
                ref_dt=ref_dt,
                target_friday=target_friday,
                run_node_test=False,
            )
            self.assertTrue(status_closed["local_hygiene_ok"], status_closed["hygiene_errors"])
            self.assertFalse(status_closed["upcoming_speaker_updated"])
            self.assertEqual(status_closed["verdict"], "blocked_upcoming_speaker_not_updated")

            # 2. Apply verified next-week speaker update in isolated temp docs_copy
            sample_1113 = {
                "meeting_date": "2026-11-13",
                "speaker_name": "範例講者",
                "profession": "企業管理顧問業代表",
                "company": "金貴範例顧問有限公司",
                "topic_headline": "跨產業商務引薦的實戰方法",
                "topic_svg_line1": "跨產業商務引薦",
                "topic_svg_line2": "從信任到成交的實戰方法",
                "topic_paragraphs": ["這支影片為 11/13 主講的演講前預告，分享跨業合作標準流程。"],
                "partners": "會計師、法律顧問、空間設計",
                "ideal_referral": "台南在地企業負責人",
                "dream_referral": "跨區連鎖品牌創辦人",
                "reel_url": "https://www.facebook.com/reel/999888777666555/",
                "source_post_url": "https://www.facebook.com/reel/999888777666555/",
                "fetched_at": "2026-11-09T10:00:00+08:00",
            }
            (content_copy / "speaker-source-20261113.json").write_text(
                json.dumps(sample_1113, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            updater.apply_speaker_update(docs_copy, sample_1113, "2026-11-09")

            # Verify SVG, index.html, videos.html, join.html, and sitemap.xml
            self.assertTrue((docs_copy / "assets/photos/focus-20261113.svg").exists())
            status_updated = updater.verify_site_status(
                docs_dir=docs_copy,
                content_dir=content_copy,
                ref_dt=ref_dt,
                target_friday=target_friday,
                run_node_test=False,
            )
            self.assertTrue(status_updated["local_hygiene_ok"], status_updated["hygiene_errors"])
            self.assertTrue(status_updated["upcoming_speaker_updated"], status_updated["upcoming_missing_reasons"])
            self.assertEqual(status_updated["verdict"], "verified_ok")


if __name__ == "__main__":
    unittest.main()
