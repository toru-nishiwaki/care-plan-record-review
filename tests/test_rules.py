# -*- coding: utf-8 -*-
"""rules.py（Streamlit非依存の業務ロジック）に対する最低限のユニットテスト。

実行方法: リポジトリルートで `python3 -m unittest discover tests`

大量の網羅テストではなく、今回のブラッシュアップで導入・修正した重要な業務ルールの
回帰を防ぐことを目的とした最小限のテストに絞っている。
"""

import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rules  # noqa: E402


def _daily_df(rows):
    """[(user_id, record_date_str, record_text), ...] からdaily_df相当のDataFrameを作る。"""
    df = pd.DataFrame(rows, columns=["user_id", "record_date", "record_text"])
    df["record_date"] = pd.to_datetime(df["record_date"])
    df["special_notes"] = ""
    return df


def _monitoring_df_empty():
    df = pd.DataFrame(columns=["user_id", "monitoring_date", "current_status", "change_from_previous", "issues"])
    df["monitoring_date"] = pd.to_datetime(df["monitoring_date"])
    return df


class TestExtractionSourceIsRecordOnly(unittest.TestCase):
    """B-2: 観察候補の抽出元は日々の記録・モニタリング記録のみであり、計画書の記載は含めない。"""

    def test_daily_record_keyword_is_extracted(self):
        daily = _daily_df([("A001", "2026-06-05", "薬が数日分残っており、服薬状況の確認が必要な状態だった。")])
        monitoring = _monitoring_df_empty()
        obs = rules.extract_observations_for_user_csv("A001", daily, monitoring)
        categories = [o["category"] for o in obs]
        self.assertIn("服薬管理", categories)

    def test_extraction_function_has_no_plans_df_parameter(self):
        # 計画書（plans_df）はこの関数のシグネチャに存在しない＝抽出対象データとして
        # 渡しようがない構造になっていることを確認する（B-2の構造的な回帰防止）。
        import inspect

        params = list(inspect.signature(rules.extract_observations_for_user_csv).parameters)
        self.assertNotIn("plans_df", params)
        self.assertEqual(params[:3], ["user_id", "daily_df", "monitoring_df"])


class TestPeriodFilter(unittest.TestCase):
    """B-1: 対象期間の指定により、期間外の記録が抽出対象から除外されること。"""

    def setUp(self):
        self.daily = _daily_df([
            ("A001", "2026-06-05", "薬が数日分残っており、服薬状況の確認が必要な状態だった。"),
            ("A001", "2026-07-10", "同じ質問を何度も繰り返す場面が見られた。"),
        ])
        self.monitoring = _monitoring_df_empty()

    def test_no_period_filter_returns_all(self):
        obs = rules.extract_observations_for_user_csv("A001", self.daily, self.monitoring)
        self.assertEqual(len(obs), 2)

    def test_period_filter_excludes_out_of_range_record(self):
        obs = rules.extract_observations_for_user_csv(
            "A001", self.daily, self.monitoring,
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 30),
        )
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["category"], "服薬管理")

    def test_period_filter_with_no_matching_record_returns_empty(self):
        obs = rules.extract_observations_for_user_csv(
            "A001", self.daily, self.monitoring,
            start_date=date(2099, 1, 1), end_date=date(2099, 1, 31),
        )
        self.assertEqual(obs, [])

    def test_resolve_period_range_choices(self):
        self.assertEqual(rules.resolve_period_range("すべての期間（指定なし）"), (None, None))
        start, end = rules.resolve_period_range("今日")
        self.assertEqual(start, end)
        start, end = rules.resolve_period_range("期間を指定", date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual((start, end), (date(2026, 1, 1), date(2026, 1, 31)))


class TestHumanInTheLoopGating(unittest.TestCase):
    """A-1: 「未確認」が1件でも残っている場合は確定できない、という業務ルールの検証。"""

    def test_all_reviewed_can_be_finalized(self):
        decisions = [
            ({"evidence": "x"}, "採用", None),
            ({"evidence": "y"}, "対象外", None),
            ({"evidence": "z"}, "内容を整えて採用", "z（整えた）"),
        ]
        self.assertFalse(rules.has_unreviewed_decisions(decisions))

    def test_one_unreviewed_blocks_finalization(self):
        decisions = [
            ({"evidence": "x"}, "採用", None),
            ({"evidence": "y"}, "未確認", None),
        ]
        self.assertTrue(rules.has_unreviewed_decisions(decisions))

    def test_empty_decisions_does_not_block(self):
        self.assertFalse(rules.has_unreviewed_decisions([]))


class TestPriorityJudgment(unittest.TestCase):
    """D001（状態変化候補ゼロ・経過観察）／U103（情報不足・追加情報確認）相当のケースの判定を検証する。"""

    def test_zero_candidates_with_substantive_text_is_stable_not_info_insufficient(self):
        # D001相当：記録は十分な長さで存在するが、抽出ルールに一致する状態変化候補がない。
        texts = ["前回から大きな変化はなく、いつもどおり落ち着いて過ごされていました。"]
        approved = []
        level = rules.compute_record_state_level_from_texts(texts, approved)
        self.assertEqual(level, "低")
        op_category = rules.compute_operational_category_from_tracks(level, None)
        self.assertEqual(op_category, "経過観察")
        visit, action, office, share_when, external, case = rules.resolve_share_candidates(approved, level)
        self.assertEqual(case, "stable")

    def test_insufficient_records_yields_info_insufficient_not_stable(self):
        # U103相当：記録・モニタリング情報が乏しく、状態変化の有無を判断できない。
        texts = ["いつもと少し違う様子だった。"]  # MIN_SUBSTANTIVE_LENGTH未満
        approved = []
        level = rules.compute_record_state_level_from_texts(texts, approved)
        self.assertEqual(level, "情報不足")
        op_category = rules.compute_operational_category_csv(level, None, False)
        self.assertEqual(op_category, "追加情報確認")
        visit, action, office, share_when, external, case = rules.resolve_share_candidates(approved, level)
        self.assertEqual(case, "info_insufficient")
        # 情報不足ケースでは、具体的な訪問時対応候補・共有条件は無理に生成しない。
        self.assertEqual(action, [])
        self.assertEqual(share_when, [])

    def test_stable_and_info_insufficient_reasons_are_distinct(self):
        stable_reason = rules.build_priority_reason([], False, "低")
        info_insufficient_reason = rules.build_priority_reason([], False, "情報不足")
        self.assertNotEqual(stable_reason, info_insufficient_reason)


class TestCsvColumnAliasBasics(unittest.TestCase):
    """CSV列名マッピングの基本動作を検証する。"""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import app  # noqa: PLC0415
        self.app = app

    def test_exact_standard_column_is_used_as_is(self):
        mapping = self.app.auto_map_columns(["user_id", "user_name"], ["user_id", "user_name"], {})
        self.assertEqual(mapping, {"user_id": "user_id", "user_name": "user_name"})

    def test_alias_column_is_mapped_to_standard_name(self):
        aliases = {"user_id": ["利用者ID", "利用者番号"]}
        mapping = self.app.auto_map_columns(["利用者ID", "その他列"], ["user_id"], aliases)
        self.assertEqual(mapping, {"user_id": "利用者ID"})

    def test_unmatched_standard_column_is_omitted(self):
        mapping = self.app.auto_map_columns(["foo"], ["user_id"], {})
        self.assertNotIn("user_id", mapping)


if __name__ == "__main__":
    unittest.main()
