# -*- coding: utf-8 -*-
"""
介護記録CSVから状態変化と確認優先度を整理するPoC
Care Plan & Record Review Support PoC

訪問介護計画書に書かれた予定支援と、日々の介護記録・モニタリングに残された
実際の状態を比較し、状態変化・計画とのずれ・次に確認すべきことを整理する
現場業務体験PoC。既存の分析（notebook/, src/, models/, outputs/）はそのまま
保持しているが、公開画面ではモデル関連の表示は行わない。

このアプリは医療診断アプリではありません。
AIが診断や支援内容・ケアプラン変更を確定するものではなく、抽出結果と候補を
職員・専門職が確認して判断します。
"""

import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from rules import (
    PERIOD_CHOICES,
    SHARE_CONFIRMER_ROLE,
    STABLE_CASE_ACTION,
    STABLE_CASE_OFFICE,
    STABLE_CASE_SHARE_NOTE,
    STABLE_CASE_VISIT,
    _compute_plan_record_comparison,
    _finalize_decisions,
    build_confirmed_observations_df,
    build_priority_reason,
    build_review_priority_results_df,
    collect_substantive_texts_csv,
    compute_input_status,
    compute_input_status_detail,
    compute_operational_category_csv,
    compute_operational_category_from_tracks,
    compute_record_state_level_from_texts,
    extract_observations_for_user_csv,
    has_unreviewed_decisions,
    resolve_period_range,
    resolve_share_candidates,
)

# ============================================================
# 基本設定
# ============================================================

st.set_page_config(
    page_title="介護記録CSVから状態変化と確認優先度を整理するPoC",
    page_icon="🧭",
    layout="wide",
)

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"

REQUIRED_FILES = {
    # 現行の計画書・記録レビューPoC（公開2画面）が実際に読み込むファイルのみを起動必須条件とする。
    # 旧・認知症リスク予測モデルの学習資産（models/, outputs/）はこのPoCの起動には不要なため含めない。
    "列名マッピング定義": DATA_DIR / "column_aliases.json",
    "サンプルCSV（利用者基本情報）": DATA_DIR / "sample_users.csv",
    "サンプルCSV（訪問介護計画書）": DATA_DIR / "sample_care_plans.csv",
    "サンプルCSV（日々の介護記録）": DATA_DIR / "sample_daily_records.csv",
    "サンプルCSV（モニタリング記録）": DATA_DIR / "sample_monitoring_records.csv",
    "CSV取込確認用データ（利用者基本情報）": DATA_DIR / "upload_demo" / "users.csv",
    "CSV取込確認用データ（訪問介護計画書）": DATA_DIR / "upload_demo" / "care_plans.csv",
    "CSV取込確認用データ（日々の介護記録）": DATA_DIR / "upload_demo" / "daily_records.csv",
    "CSV取込確認用データ（モニタリング記録）": DATA_DIR / "upload_demo" / "monitoring_records.csv",
}

# 運用上の確認区分（3区分）
OPERATIONAL_CATEGORIES = ["優先確認", "追加情報確認", "経過観察"]
OPERATIONAL_CATEGORY_ORDER = {name: i for i, name in enumerate(OPERATIONAL_CATEGORIES)}


TEAL = "#0d9488"
TEAL_DARK = "#0f766e"


# ============================================================
# 次回確認・対応・共有候補の表示（業務ロジックはrules.pyを参照）
# ============================================================


def _render_share_disclaimer():
    st.markdown(
        """
        <div class="warn-box">
        表示される内容は、記録上の観察事項をもとにした確認・対応・共有の候補です。
        個別の支援内容、訪問回数、医療対応、ケアプラン変更を決定するものではありません。
        本人の状態・希望・現在のケアプランを踏まえて、職員・専門職が判断してください。
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_share_candidates_section(approved_observations: list, key_suffix: str, record_state_level: str = None):
    """次回訪問で確認すること／訪問時の対応候補／事業所内で共有すること／関係職種への共有を検討する条件、の4区分で表示する。"""
    st.subheader("次回確認・対応・共有候補")

    visit, action, office, share_when, external, case = resolve_share_candidates(approved_observations, record_state_level)

    for title, items in [
        ("次回訪問で確認すること", visit),
        ("訪問時の対応候補", action),
        ("事業所内で共有すること", office),
    ]:
        if items:
            st.markdown(f"**{title}**")
            st.markdown('<div class="app-card">' + "".join(f"・{i}<br>" for i in items) + "</div>", unsafe_allow_html=True)

    if case == "stable":
        st.markdown("**関係職種への共有を検討する条件**")
        st.markdown(f'<div class="app-card">{share_when[0]}</div>', unsafe_allow_html=True)
    elif share_when or external:
        st.markdown("**関係職種への共有を検討する条件**")
        lines = "".join(f"・{i}<br>" for i in share_when)
        if external:
            share_targets = "・".join(r for r in external if r != SHARE_CONFIRMER_ROLE) or "関係職種"
            lines += f"上記が続く場合は、{SHARE_CONFIRMER_ROLE}が内容を確認し、{share_targets}等への共有を検討します。"
        st.markdown(f'<div class="app-card">{lines}</div>', unsafe_allow_html=True)

    _render_share_disclaimer()


# ============================================================
# 共通スタイル
# ============================================================

def inject_css():
    st.markdown(
        f"""
        <style>
        .stApp {{ background-color: #f8fafc; }}
        .app-card {{
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 1.25rem 1.5rem;
            margin-bottom: 1rem;
        }}
        .metric-card {{
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-top: 4px solid {TEAL};
            border-radius: 10px;
            padding: 1rem 1.1rem;
            text-align: center;
        }}
        .metric-card .label {{ font-size: 0.85rem; color: #475569; margin-bottom: 0.25rem; }}
        .metric-card .value {{ font-size: 1.6rem; font-weight: 700; color: {TEAL_DARK}; }}
        .notice-box {{
            background-color: #f0fdfa;
            border: 1px solid #99f6e4;
            border-left: 5px solid {TEAL};
            border-radius: 8px;
            padding: 0.9rem 1.1rem;
            margin-bottom: 1rem;
            color: #134e4a;
            font-size: 0.92rem;
        }}
        .warn-box {{
            background-color: #fffbeb;
            border: 1px solid #fde68a;
            border-left: 5px solid #b45309;
            border-radius: 8px;
            padding: 0.9rem 1.1rem;
            margin-bottom: 1rem;
            color: #78350f;
            font-size: 0.92rem;
        }}
        .risk-badge {{
            display: inline-block; padding: 0.35rem 0.9rem; border-radius: 999px;
            font-weight: 700; font-size: 1rem; border: 1px solid transparent;
        }}
        .risk-low {{ background-color: #ecfdf5; color: #065f46; border-color: #6ee7b7; }}
        .risk-medium {{ background-color: #fffbeb; color: #92400e; border-color: #fcd34d; }}
        .risk-high {{ background-color: #fef2f2; color: #991b1b; border-color: #fca5a5; }}
        .ref-badge {{
            display: inline-block; padding: 0.3rem 0.8rem; border-radius: 999px;
            font-weight: 700; font-size: 0.88rem; border: 1px solid transparent;
        }}
        .ref-高 {{ background-color: #eef2ff; color: #3730a3; border-color: #a5b4fc; }}
        .ref-中 {{ background-color: #f1f5f9; color: #334155; border-color: #cbd5e1; }}
        .ref-低 {{ background-color: #f8fafc; color: #64748b; border-color: #cbd5e1; }}
        .ref-参考外 {{ background-color: #fef2f2; color: #991b1b; border-color: #fca5a5; border-style: dashed; }}
        .op-badge {{
            display: inline-block; padding: 0.3rem 0.8rem; border-radius: 999px;
            font-weight: 700; font-size: 0.88rem; border: 1px solid transparent;
        }}
        .op-優先確認 {{ background-color: #fef2f2; color: #991b1b; border-color: #fca5a5; }}
        .op-追加情報確認 {{ background-color: #fff7ed; color: #9a3412; border-color: #fdba74; }}
        .op-経過観察 {{ background-color: #eff6ff; color: #1e40af; border-color: #93c5fd; }}
        .metric-explain {{ font-size: 0.85rem; color: #475569; margin: 0.35rem 0 1rem 0; line-height: 1.6; }}
        .threshold-callout {{
            background-color: #f0fdfa; border: 2px solid {TEAL}; border-radius: 12px;
            padding: 1.25rem 1.5rem; margin-bottom: 1.25rem;
        }}
        .threshold-callout-title {{ font-size: 1.3rem; font-weight: 800; color: {TEAL_DARK}; margin-bottom: 0.35rem; }}
        .threshold-callout-body {{ color: #134e4a; margin-bottom: 0.9rem; }}
        .role-grid {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
        .role-card {{ flex: 1 1 240px; background-color: #ffffff; border-radius: 8px; padding: 0.75rem 1rem; border: 1px solid #e2e8f0; }}
        .role-detect {{ border-left: 5px solid {TEAL_DARK}; }}
        .role-priority {{ border-left: 5px solid #b91c1c; }}
        .role-card-title {{ font-weight: 700; margin-bottom: 0.2rem; }}
        .role-detect .role-card-title {{ color: {TEAL_DARK}; }}
        .role-priority .role-card-title {{ color: #b91c1c; }}
        .role-card-desc {{ font-size: 0.88rem; color: #334155; }}
        .missed-highlight {{ background-color: #fee2e2; color: #991b1b; font-weight: 700; padding: 0.1rem 0.4rem; border-radius: 4px; }}
        .future-box {{
            background-color: #f1f5f9; border: 2px dashed #94a3b8; border-radius: 12px;
            padding: 1.25rem 1.5rem; margin-bottom: 0.75rem; color: #334155;
        }}
        .future-box ul {{ margin: 0.3rem 0 0.8rem 1.2rem; }}
        .future-badge {{
            display: inline-block; background-color: #64748b; color: #ffffff; font-size: 0.75rem;
            font-weight: 700; padding: 0.2rem 0.6rem; border-radius: 999px; margin-bottom: 0.6rem;
        }}
        .future-title {{ font-size: 1.1rem; font-weight: 700; margin: 0.3rem 0 0.6rem 0; color: #334155; }}
        .future-flow-card {{
            background-color: #f1f5f9; border: 1px dashed #94a3b8; border-radius: 8px; padding: 0.6rem;
            text-align: center; font-size: 0.82rem; color: #334155; min-height: 70px;
            display: flex; align-items: center; justify-content: center; margin-bottom: 0.75rem;
        }}
        .flow-card {{
            background-color: #ffffff; border: 1px solid #e2e8f0; border-top: 3px solid {TEAL};
            border-radius: 10px; padding: 0.8rem; text-align: center; font-size: 0.85rem;
            min-height: 80px; display: flex; align-items: center; justify-content: center; margin-bottom: 0.75rem;
        }}
        .flow-arrow {{
            display: flex; align-items: center; justify-content: center; height: 80px;
            font-size: 1.4rem; color: {TEAL_DARK}; font-weight: 700; margin-bottom: 0.75rem;
        }}
        .stat-card {{ background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1rem 1.1rem; text-align: center; }}
        .stat-card .label {{ font-size: 0.82rem; color: #475569; margin-bottom: 0.25rem; }}
        .stat-card .value {{ font-size: 1.5rem; font-weight: 800; color: #1e293b; }}
        .obs-card {{
            background-color: #ffffff; border: 1px solid #e2e8f0; border-left: 4px solid {TEAL};
            border-radius: 10px; padding: 0.9rem 1.1rem; margin-bottom: 0.6rem; font-size: 0.92rem;
        }}
        .compare-card {{
            background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
            padding: 0.9rem 1.1rem; margin-bottom: 0.75rem; font-size: 0.92rem; height: 100%;
        }}
        .compare-plan {{ border-left: 5px solid #2563eb; }}
        .compare-actual {{ border-left: 5px solid {TEAL_DARK}; }}
        .compare-gap {{ border-left: 5px solid #b45309; }}
        .compare-label {{ font-size: 0.78rem; font-weight: 700; color: #475569; margin-bottom: 0.3rem; text-transform: uppercase; letter-spacing: 0.02em; }}
        .challenge-card {{
            background-color: #ffffff; border: 1px solid #e2e8f0; border-top: 4px solid #b45309;
            border-radius: 10px; padding: 1rem 1.1rem; margin-bottom: 0.75rem; height: 100%;
        }}
        .challenge-card-title {{ font-weight: 700; margin-bottom: 0.35rem; color: #78350f; }}
        .challenge-card-desc {{ font-size: 0.88rem; color: #334155; }}
        .effect-card {{
            background-color: #ffffff; border: 1px solid #e2e8f0; border-top: 4px solid {TEAL};
            border-radius: 10px; padding: 1rem 1.1rem; margin-bottom: 0.75rem; height: 100%;
        }}
        .effect-card-title {{ font-weight: 700; margin-bottom: 0.35rem; color: {TEAL_DARK}; }}
        .effect-card-desc {{ font-size: 0.88rem; color: #334155; }}
        .source-badge {{
            display: inline-block; background-color: {TEAL}; color: #ffffff; font-weight: 700;
            padding: 0.3rem 0.85rem; border-radius: 999px; font-size: 0.88rem; margin-bottom: 0.4rem;
        }}
        section[data-testid="stSidebar"] .sidebar-footer {{
            font-size: 0.78rem; color: #475569; border-top: 1px solid #e2e8f0;
            padding-top: 0.75rem; margin-top: 0.75rem;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_flow_arrows(steps: list):
    n = len(steps)
    weights = []
    for i in range(n):
        weights.append(4)
        if i < n - 1:
            weights.append(1)
    cols = st.columns(weights)
    idx = 0
    for i, step in enumerate(steps):
        cols[idx].markdown(f'<div class="flow-card">{i + 1}. {step}</div>', unsafe_allow_html=True)
        idx += 1
        if i < n - 1:
            cols[idx].markdown('<div class="flow-arrow">→</div>', unsafe_allow_html=True)
            idx += 1


# ============================================================
# データ読み込み
# ============================================================

def check_required_files():
    missing = [name for name, path in REQUIRED_FILES.items() if not path.exists()]
    if missing:
        st.error(
            "デモに必要なファイルが見つかりません。\n\n"
            + "\n".join(f"- {name}" for name in missing)
            + "\n\nリポジトリの `data/` 配下のサンプルCSV・列名マッピング定義が"
            "揃っているか確認してから再度お試しください。"
        )
        st.stop()


@st.cache_data(show_spinner=False)
def load_sample_bundle():
    """「サンプルデータで試す」モード用に、サンプルCSV4種を正規化済みDataFrameとして読み込む。

    CSVアップロードモードと同じ列構成・日付型で返すことで、以降の処理
    （統合プレビュー・観察事項抽出・計画とのずれ生成）を共通化する。
    """
    users = pd.read_csv(DATA_DIR / "sample_users.csv", dtype=str)
    plans = pd.read_csv(DATA_DIR / "sample_care_plans.csv", dtype=str)
    daily = pd.read_csv(DATA_DIR / "sample_daily_records.csv", dtype=str)
    monitoring = pd.read_csv(DATA_DIR / "sample_monitoring_records.csv", dtype=str)
    daily["record_date"] = pd.to_datetime(daily["record_date"], errors="coerce")
    monitoring["monitoring_date"] = pd.to_datetime(monitoring["monitoring_date"], errors="coerce")
    plans["plan_start_date"] = pd.to_datetime(plans["plan_start_date"], errors="coerce")
    plans["plan_end_date"] = pd.to_datetime(plans["plan_end_date"], errors="coerce")
    return users, plans, daily, monitoring


def render_operational_badge(category: str) -> str:
    return f'<span class="op-badge op-{category}">確認優先度：{category}</span>'


def render_demo_data_disclaimer():
    st.caption("※本デモの利用者・記録・数値はすべて架空データです。実在利用者の情報は使用していません。")


# ============================================================
# CSVアップロード（介護ソフトCSV連携を想定した共通フォーマットPoC）
# 特定製品との正式連携ではない。カイポケ等の専用API連携は将来構想。
# ============================================================

CSV_ENCODINGS = ["utf-8", "utf-8-sig", "cp932", "shift_jis"]

CSV_SCHEMAS = {
    "users": {
        "label": "利用者基本情報",
        "filename_hint": "users.csv",
        "standard_columns": ["user_id", "user_name", "age", "care_level", "medical_history", "language_support", "notes"],
        "required_columns": ["user_id"],
        "date_columns": [],
    },
    "care_plans": {
        "label": "訪問介護計画書",
        "filename_hint": "care_plans.csv",
        "standard_columns": [
            "user_id", "plan_start_date", "plan_end_date", "goal",
            "support_content", "precautions", "medication_support", "mobility_support",
        ],
        "required_columns": ["user_id", "support_content"],
        "date_columns": ["plan_start_date", "plan_end_date"],
    },
    "daily_records": {
        "label": "日々の介護記録",
        "filename_hint": "daily_records.csv",
        "standard_columns": ["user_id", "record_date", "service_type", "record_text", "special_notes", "staff_id"],
        "required_columns": ["user_id", "record_date", "record_text"],
        "date_columns": ["record_date"],
    },
    "monitoring_records": {
        "label": "モニタリング記録",
        "filename_hint": "monitoring_records.csv",
        "standard_columns": ["user_id", "monitoring_date", "current_status", "change_from_previous", "issues", "reviewer_id"],
        "required_columns": ["user_id", "monitoring_date", "current_status"],
        "date_columns": ["monitoring_date"],
    },
}


@st.cache_data(show_spinner=False)
def load_column_aliases():
    with open(DATA_DIR / "column_aliases.json", encoding="utf-8") as f:
        return json.load(f)


def read_csv_with_encoding_fallback(uploaded_file):
    """UTF-8 / UTF-8(BOM付き) / CP932 / Shift_JISの順に読み込みを試す。

    すべて失敗した場合は、技術的なスタックトレースではなく
    利用者向けのエラーメッセージを返す。
    """
    raw = uploaded_file.getvalue()
    for enc in CSV_ENCODINGS:
        try:
            df = pd.read_csv(io.BytesIO(raw), encoding=enc, dtype=str)
            return df, enc, None
        except Exception:
            continue
    return None, None, "CSVの文字コードまたは形式を確認できませんでした。UTF-8またはShift_JIS形式で保存して再度お試しください。"


def auto_map_columns(actual_columns: list, standard_columns: list, aliases: dict) -> dict:
    mapping = {}
    for std_col in standard_columns:
        if std_col in actual_columns:
            mapping[std_col] = std_col
            continue
        for alias in aliases.get(std_col, []):
            if alias in actual_columns:
                mapping[std_col] = alias
                break
    return mapping


def render_csv_upload_section(schema_key: str, aliases: dict, other_normalized: dict):
    """1種類のCSVについて、アップロード→文字コード判定→列名マッピング→検証を行う。

    存在しない列を推測で補うことはしない。必須列が自動認識できない場合は
    手動マッピングUIを表示する。
    """
    schema = CSV_SCHEMAS[schema_key]
    st.markdown(f"**{schema['label']}**（推奨ファイル名: {schema['filename_hint']}）")
    uploaded = st.file_uploader(
        f"{schema['label']}のCSVを選択", type=["csv"], key=f"upload_{schema_key}", label_visibility="collapsed",
    )
    if uploaded is None:
        st.caption("未アップロードです。")
        return None

    raw_df, encoding_used, err = read_csv_with_encoding_fallback(uploaded)
    if err:
        st.error(f"{schema['filename_hint']}：{err}")
        return None

    raw_df.columns = [str(c).strip() for c in raw_df.columns]
    mapping = auto_map_columns(list(raw_df.columns), schema["standard_columns"], aliases)

    missing_required = [c for c in schema["required_columns"] if c not in mapping]
    if missing_required:
        st.warning(
            f"{schema['filename_hint']}：必須項目（{', '.join(missing_required)}）に対応する列を"
            "自動認識できませんでした。該当する列を選択してください。"
        )
        options = ["（該当なし）"] + list(raw_df.columns)
        for col in missing_required:
            choice = st.selectbox(f"「{col}」に対応する列", options, key=f"manualmap_{schema_key}_{col}")
            if choice != "（該当なし）":
                mapping[col] = choice
        missing_required = [c for c in schema["required_columns"] if c not in mapping]

    if missing_required:
        st.error(f"{schema['filename_hint']}：読込不可。必須項目（{', '.join(missing_required)}）が見つかりません。")
        return None

    normalized = pd.DataFrame()
    for std_col in schema["standard_columns"]:
        normalized[std_col] = raw_df[mapping[std_col]] if std_col in mapping else None

    messages = []
    empty_uid = normalized["user_id"].isna() | (normalized["user_id"].astype(str).str.strip() == "")
    if empty_uid.any():
        messages.append(f"user_idが空の行が{int(empty_uid.sum())}件あります（除外して読み込みます）。")
        normalized = normalized[~empty_uid].reset_index(drop=True)

    for date_col in schema["date_columns"]:
        original = normalized[date_col]
        parsed = pd.to_datetime(original, errors="coerce")
        bad = parsed.isna() & original.notna() & (original.astype(str).str.strip() != "")
        if bad.any():
            messages.append(f"{date_col}を日付として読み込めない行が{int(bad.sum())}件あります。")
        normalized[date_col] = parsed

    full_dup = normalized.duplicated()
    if full_dup.any():
        messages.append(f"完全重複行が{int(full_dup.sum())}件あります（重複分は除外します）。")
        normalized = normalized[~full_dup].reset_index(drop=True)

    text_cols_to_check = [c for c in schema["required_columns"] if c in ("record_text", "support_content", "current_status")]
    for col in text_cols_to_check:
        empty_text = normalized[col].isna() | (normalized[col].astype(str).str.strip() == "")
        if empty_text.any():
            messages.append(f"{col}が空の行が{int(empty_text.sum())}件あります。")

    if schema_key == "daily_records":
        subset_dup = normalized.duplicated(subset=["user_id", "record_date", "record_text"])
        if subset_dup.any():
            messages.append(f"同じ利用者・記録日・記録内容の重複が{int(subset_dup.sum())}件あります。")
    if schema_key == "monitoring_records":
        subset_dup = normalized.duplicated(subset=["user_id", "monitoring_date", "current_status"])
        if subset_dup.any():
            messages.append(f"同じ利用者・モニタリング日・内容の重複が{int(subset_dup.sum())}件あります。")

    if schema_key != "users" and other_normalized.get("users") is not None:
        known_ids = set(other_normalized["users"]["user_id"].astype(str))
        this_ids = set(normalized["user_id"].astype(str).unique())
        unknown = sorted(this_ids - known_ids)
        if unknown:
            messages.append(f"利用者基本情報に存在しないuser_idが{len(unknown)}件あります（例: {', '.join(unknown[:5])}）。")

    st.success(f"{schema['filename_hint']}：{len(normalized)}件を読み込みました（文字コード: {encoding_used}）。")
    for msg in messages:
        st.warning(msg)

    return normalized


def build_integration_preview(users_df, plans_df, daily_df, monitoring_df) -> pd.DataFrame:
    """利用者ID単位でCSVを統合し、データ統合プレビューを作成する。"""
    all_ids = set()
    for df in [users_df, plans_df, daily_df, monitoring_df]:
        if df is not None:
            all_ids.update(df["user_id"].astype(str).unique())

    today = pd.Timestamp.now().normalize()
    rows = []
    for uid in sorted(all_ids):
        has_basic = users_df is not None and uid in set(users_df["user_id"].astype(str))
        has_plan = plans_df is not None and uid in set(plans_df["user_id"].astype(str))
        daily_count = 0 if daily_df is None else int((daily_df["user_id"].astype(str) == uid).sum())
        monitoring_count = 0 if monitoring_df is None else int((monitoring_df["user_id"].astype(str) == uid).sum())

        dates = []
        if daily_df is not None:
            dates += daily_df.loc[daily_df["user_id"].astype(str) == uid, "record_date"].dropna().tolist()
        if monitoring_df is not None:
            dates += monitoring_df.loc[monitoring_df["user_id"].astype(str) == uid, "monitoring_date"].dropna().tolist()
        last_date = max(dates) if dates else None
        last_record_days = (today - last_date).days if last_date is not None else None

        input_status = compute_input_status(has_basic, has_plan, daily_count, monitoring_count, last_record_days)
        status_detail = compute_input_status_detail(has_basic, has_plan, daily_count, monitoring_count, last_record_days)
        rows.append(
            {
                "user_id": uid,
                "has_basic_info": has_basic,
                "has_care_plan": has_plan,
                "daily_count": daily_count,
                "monitoring_count": monitoring_count,
                "last_record_date": last_date,
                "last_record_days": last_record_days,
                "input_status": input_status,
                "import_insufficient": status_detail["insufficient"],
                "import_detail": status_detail["detail"],
            }
        )
    return pd.DataFrame(rows)


def render_session_storage_notice():
    st.caption(
        "アップロードしたデータと職員確認内容は、このデモセッション内のみ保持されます。"
        "ページを再読み込みすると内容が失われる場合があります。"
    )


# ============================================================
# サイドバー
# ============================================================

PAGES = ["計画書・記録レビュー", "利用者一覧・詳細"]


def render_sidebar():
    st.sidebar.title("Care Plan & Record Review")
    st.sidebar.caption("計画書・記録レビューPoC")
    page = st.sidebar.radio("画面を選択", PAGES, label_visibility="collapsed")
    st.sidebar.markdown(
        """
        <div class="sidebar-footer">
        本デモは医療診断を目的としたものではありません。AIが診断や支援内容を確定するものではなく、
        抽出結果と候補を職員・専門職が確認して判断します。
        </div>
        """,
        unsafe_allow_html=True,
    )
    return page


# ============================================================
# 画面1: 記録から確認優先度まで（中心画面）
# ============================================================


def _summarize_main_change(approved: list):
    non_info = [o for o in approved if o["category"] != "情報不足"]
    if non_info:
        top = non_info[0]
        text = top.get("confirmed_text", top["evidence"])
        return f"{top['category']}：{text[:30]}", top["source_type"], str(top.get("record_date", "-"))
    if approved:
        top = approved[0]
        return "情報不足", top["source_type"], str(top.get("record_date", "-"))
    return "特に確認すべき記載なし", "-", "-"


def _render_observation_cards_and_form(extracted: list, form_key: str, key_prefix: str):
    st.caption(
        "この確認は、元の介護記録を修正するものではありません。"
        "抽出された観察候補が、今回の確認対象として適切かを確認します。"
        "すべての観察候補について職員判断を行ってください。"
    )
    decisions = []
    for i, obs in enumerate(extracted):
        st.markdown(
            f'<div class="obs-card"><b>観察カテゴリ</b>：{obs["category"]}<br>'
            f'<b>抽出された内容</b>：{obs["evidence"]}<br>'
            f'<b>記録日</b>：{obs.get("record_date", "-")}／<b>情報源</b>：{obs.get("source_type", "-")}</div>',
            unsafe_allow_html=True,
        )
        with st.expander("抽出根拠を見る"):
            matched = obs.get("matched_keyword")
            confirmed_expression = f"「{matched}」" if matched else "特定の表現ではなく、記載内容が短いことから抽出"
            st.markdown(
                f"**記録内で確認した表現：** {confirmed_expression}  \n"
                f"**適用した確認カテゴリ：** 「{obs['category']}」  \n"
                f"**抽出方法：** キーワード・業務ルールによる候補抽出"
            )
        decision = st.radio(
            f"扱い（{i + 1}件目）", ["未確認", "採用", "内容を整えて採用", "対象外"],
            horizontal=True, key=f"{key_prefix}_decision_{i}",
        )
        corrected_text = None
        if decision == "内容を整えて採用":
            corrected_text = st.text_area("整えた内容", value=obs["evidence"], key=f"{key_prefix}_correction_{i}")
        decisions.append((obs, decision, corrected_text))
    return decisions


def get_user_name(user_id: str, users_df) -> str:
    """利用者IDに対応する氏名をusers_dfから取得する。氏名列がない・未入力の場合は空文字を返す。"""
    if users_df is None or "user_name" not in users_df.columns:
        return ""
    matches = users_df.loc[users_df["user_id"].astype(str) == str(user_id), "user_name"]
    if matches.empty:
        return ""
    name = matches.iloc[0]
    if name is None or (isinstance(name, float) and pd.isna(name)) or str(name).strip() == "" or str(name).strip().lower() == "nan":
        return ""
    return str(name).strip()


def render_target_user_header(user_id: str, users_df, *, is_sample: bool):
    """「確認優先度と次回確認・対応・共有候補」の直前で、対象利用者の氏名とIDを明示する。"""
    name = get_user_name(user_id, users_df)
    fiction_suffix = "・架空データ" if is_sample else ""
    if name:
        sub_line = f"{user_id}{fiction_suffix}"
    else:
        sub_line = f"利用者名はCSVに含まれていません{('（' + fiction_suffix.lstrip('・') + '）') if fiction_suffix else ''}"
    st.markdown(
        f'<div class="app-card"><b>対象利用者</b>：{name or user_id}<br>'
        f'<span style="color:#64748b;font-size:0.85rem;">{sub_line}</span></div>',
        unsafe_allow_html=True,
    )
    if is_sample:
        st.caption("表示される利用者名・記録内容はすべて架空データです。")


def _render_plan_record_review_body(selected_user, plans_df, daily_df, monitoring_df, users_df=None, *, import_detail=None):
    """計画書・記録レビューの本体（ケアプラン概要〜確認優先度・共有候補）。

    サンプルモードでは import_detail=None とし、入力情報の充足状況という概念を出さない。
    CSVアップロードモードでは、選択中の利用者の取込充足状況（compute_input_status_detail の結果）を渡す。
    """
    plan_row, plan_matches = None, None
    if plans_df is not None:
        plan_matches = plans_df[plans_df["user_id"].astype(str) == selected_user]
        if len(plan_matches):
            plan_row = plan_matches.iloc[0]

    daily_user = daily_df[daily_df["user_id"].astype(str) == selected_user].sort_values("record_date") if daily_df is not None else pd.DataFrame()
    monitoring_user = monitoring_df[monitoring_df["user_id"].astype(str) == selected_user].sort_values("monitoring_date") if monitoring_df is not None else pd.DataFrame()

    review_target_name = get_user_name(selected_user, users_df)
    if review_target_name:
        st.caption(f"レビュー対象：{review_target_name}（{selected_user}）")
    else:
        st.caption(f"レビュー対象：{selected_user}")

    st.markdown("##### ケアプラン概要")
    if plan_row is not None:
        c1, c2, c3 = st.columns(3)
        c1.markdown(f'<div class="app-card"><b>支援目標</b><br>{plan_row.get("goal", "-")}</div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="app-card"><b>計画された支援内容</b><br>{plan_row.get("support_content", "-")}</div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="app-card"><b>観察・留意事項</b><br>{plan_row.get("precautions", "-")}</div>', unsafe_allow_html=True)
        with st.expander("計画書の詳細を見る"):
            st.dataframe(plan_matches, use_container_width=True, hide_index=True)
    else:
        st.info("この利用者の計画書は未アップロードです。")

    with st.expander(f"日々の記録・モニタリングを見る（{len(daily_user) + len(monitoring_user)}件）"):
        for _, r in daily_user.iterrows():
            date_str = r["record_date"].date().isoformat() if pd.notna(r["record_date"]) else "日付不明"
            st.markdown(f"**{date_str}｜日々の介護記録**  \n{r.get('record_text', '')}")
            if str(r.get("special_notes", "")).strip() and str(r.get("special_notes")).lower() != "nan":
                st.markdown(f"（特記事項）{r['special_notes']}")
            st.markdown("---")
        for _, r in monitoring_user.iterrows():
            date_str = r["monitoring_date"].date().isoformat() if pd.notna(r["monitoring_date"]) else "日付不明"
            st.markdown(f"**{date_str}｜モニタリング記録**  \n{r.get('current_status', '')}")
            st.markdown("---")

    planned_support, actual_summary, change_text, gap_text = _compute_plan_record_comparison(
        selected_user, plan_row, daily_df, monitoring_df
    )

    st.markdown("##### 計画内容と実際の記録を比較")
    r1c1, r1c2 = st.columns(2)
    r1c1.markdown(
        f'<div class="compare-card compare-plan"><div class="compare-label">計画された支援</div>{planned_support}</div>',
        unsafe_allow_html=True,
    )
    r1c2.markdown(
        f'<div class="compare-card compare-actual"><div class="compare-label">記録された実際の状態</div>{actual_summary}</div>',
        unsafe_allow_html=True,
    )
    r2c1, r2c2 = st.columns(2)
    r2c1.markdown(
        f'<div class="compare-card compare-actual"><div class="compare-label">前回状態からの変化</div>{change_text}</div>',
        unsafe_allow_html=True,
    )
    r2c2.markdown(
        f'<div class="compare-card compare-gap"><div class="compare-label">計画とのずれ・確認が必要な点</div>{gap_text}</div>',
        unsafe_allow_html=True,
    )
    st.caption("「計画とのずれ」は、ルールベースで生成したデモ用の比較結果です。")

    st.markdown("---")
    st.subheader("抽出結果の確認")
    st.caption("この抽出はキーワード・ルールベースの処理です（AI解析ではありません）。抽出候補は、職員確認後に確認優先度へ反映されます。")
    st.caption("※期間指定は、この「観察事項を抽出」にのみ適用されます。上の「計画内容と実際の記録を比較」は、常に全期間の記録をもとにした参考表示です。")

    extract_key = f"extracted_{selected_user}"
    approved_key = f"approved_{selected_user}"
    period_result_key = f"extracted_period_{selected_user}"

    period_choice = st.selectbox("対象期間", PERIOD_CHOICES, key=f"period_choice_{selected_user}")
    custom_start, custom_end = None, None
    if period_choice == "期間を指定":
        pcol1, pcol2 = st.columns(2)
        custom_start = pcol1.date_input("開始日", key=f"period_start_{selected_user}")
        custom_end = pcol2.date_input("終了日", key=f"period_end_{selected_user}")
    start_date, end_date = resolve_period_range(period_choice, custom_start, custom_end)

    if st.button("観察事項を抽出", type="primary", key=f"extract_btn_{selected_user}"):
        st.session_state[extract_key] = extract_observations_for_user_csv(
            selected_user, daily_df, monitoring_df, start_date=start_date, end_date=end_date
        )
        st.session_state[period_result_key] = (start_date, end_date)
        st.session_state.pop(approved_key, None)

    extracted = st.session_state.get(extract_key)
    if extracted is None:
        st.info("「観察事項を抽出」ボタンを押すと、この利用者の記録から観察事項の候補が抽出されます。")
        return

    result_start, result_end = st.session_state.get(period_result_key, (None, None))
    if result_start is None and result_end is None:
        st.caption("現在表示中の抽出結果の対象期間：すべての期間")
    else:
        st.caption(f"現在表示中の抽出結果の対象期間：{result_start:%Y/%m/%d} ～ {result_end:%Y/%m/%d}")

    if not extracted:
        st.markdown(
            '<div class="notice-box">今回の抽出ルールでは、状態変化を示す観察候補は検出されませんでした。</div>',
            unsafe_allow_html=True,
        )
        st.caption("記録には、前回から大きな変化は見られていないと記載されています。抽出結果は、状態の安定や安全を保証するものではありません。")
        st.session_state[approved_key] = []
    else:
        with st.form(key=f"review_form_{selected_user}"):
            decisions = _render_observation_cards_and_form(extracted, f"review_form_{selected_user}", selected_user)
            submitted = st.form_submit_button("確認を完了して優先度を算出", type="primary")
        if submitted:
            if has_unreviewed_decisions(decisions):
                st.error("未確認の候補があります。すべての観察候補について、採用・内容を整えて採用・対象外のいずれかを選択してください。")
            else:
                st.session_state[approved_key] = _finalize_decisions(decisions)

    approved = st.session_state.get(approved_key)
    if approved is None:
        st.info("すべての観察候補について採否を選択し、「確認を完了して優先度を算出」を押すと、確認優先度が表示されます。")
        return

    st.markdown("---")
    st.subheader("確認優先度と次回確認・対応・共有候補")

    render_target_user_header(selected_user, users_df, is_sample=import_detail is None)

    texts = collect_substantive_texts_csv(selected_user, daily_df, monitoring_df)
    record_state_level = compute_record_state_level_from_texts(texts, approved)
    input_insufficient = bool(import_detail["insufficient"]) if import_detail is not None else False
    if import_detail is not None:
        operational_category = compute_operational_category_csv(record_state_level, None, input_insufficient)
    else:
        operational_category = compute_operational_category_from_tracks(record_state_level, None)
    reason_text = build_priority_reason(approved, input_insufficient, record_state_level)

    st.markdown(render_operational_badge(operational_category), unsafe_allow_html=True)
    st.markdown(
        f'<div class="app-card"><b>主な判定理由</b>：{reason_text}<br>'
        f'<b>職員確認済み</b>：{len(approved)}件</div>',
        unsafe_allow_html=True,
    )
    st.caption("確認優先度は、職員確認済みの観察事項・記録の継続性・安全面などの懸念から判定するデモ用の業務ルールです。")

    render_share_candidates_section(approved, key_suffix=selected_user, record_state_level=record_state_level)

    main_change, main_source_type, main_source_date = _summarize_main_change(approved)
    reviewed_entry = {
        "operational_category": operational_category,
        "record_state_level": record_state_level,
        "priority_reason": reason_text,
        "approved_observations": approved,
        "main_change": main_change,
        "main_source_type": main_source_type,
        "main_source_date": main_source_date,
        "review_status": "確認済み",
        "planned_support": planned_support,
        "plan_goal": plan_row.get("goal", "-") if plan_row is not None else "-",
        "plan_precautions": plan_row.get("precautions", "-") if plan_row is not None else "-",
        "actual_record_summary": actual_summary,
        "plan_record_gap": gap_text,
        "change_from_previous": change_text,
        "user_name": get_user_name(selected_user, users_df),
    }
    if import_detail is not None:
        reviewed_entry["input_status"] = import_detail.get("input_status", "")
        reviewed_entry["import_detail"] = import_detail.get("detail", "")
    st.session_state["reviewed_users"][selected_user] = reviewed_entry


def _reset_mode_session_state():
    """入力方法（内蔵サンプル／CSVアップロード）切替時に、前のモードの処理結果を持ち越さないようにする。"""
    st.session_state["reviewed_users"] = {}
    st.session_state.pop("review_user_select", None)
    st.session_state.pop("csv_review_started", None)
    st.session_state.pop("last_prediction", None)
    for key in list(st.session_state.keys()):
        if key.startswith(("extracted_", "approved_")):
            del st.session_state[key]


@st.cache_data(show_spinner=False)
def _build_upload_demo_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in ["users.csv", "care_plans.csv", "daily_records.csv", "monitoring_records.csv"]:
            path = DATA_DIR / "upload_demo" / fname
            if path.exists():
                zf.write(path, arcname=fname)
    return buf.getvalue()


def page_plan_record_review():
    st.title("介護記録CSVから状態変化と確認優先度を整理するPoC")
    st.caption("訪問介護計画書と日々の記録を比較し、見落としやすい状態変化と、次回確認・共有すべき内容を職員が整理するための意思決定支援デモです。")

    st.markdown("##### 解決したい現場課題")
    cc1, cc2, cc3 = st.columns(3)
    for col, title, desc in [
        (cc1, "記録が分散している", "計画書、日々の介護記録、モニタリングを個別に読む必要があり、利用者の変化を横断的に把握しにくい。"),
        (cc2, "確認方法が属人化している", "職員の経験によって、記録から読み取る内容や次回確認の方法に差が生じる。"),
        (cc3, "引継ぎ内容にばらつきがある", "職員の入替えや多様な人材の登用が進む中で、何を確認し、誰へ共有するかを整理する必要がある。"),
    ]:
        col.markdown(
            f'<div class="challenge-card"><div class="challenge-card-title">{title}</div>'
            f'<div class="challenge-card-desc">{desc}</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("##### このPoCで行うこと")
    render_flow_arrows(["CSV取込", "計画と記録を比較", "職員が抽出内容を確認", "確認優先度・確認候補を表示"])
    st.caption("抽出結果と候補は職員が確認し、アプリが医療判断や個別の支援内容を確定するものではありません。")

    st.markdown("##### 期待する効果")
    st.caption("以下は本PoCで検証したい効果仮説です。")
    ec1, ec2, ec3 = st.columns(3)
    for col, title, desc in [
        (ec1, "状態変化の見落とし防止", "複数の記録を横断し、前回状態からの変化を確認しやすくする。"),
        (ec2, "確認業務の効率化", "確認対象と確認内容を整理し、優先的に見るべき利用者を把握しやすくする。"),
        (ec3, "支援・共有方法の標準化", "次回確認や事業所内共有の論点を整理し、職員ごとのばらつきを抑える。"),
    ]:
        col.markdown(
            f'<div class="effect-card"><div class="effect-card-title">{title}</div>'
            f'<div class="effect-card-desc">{desc}</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")
    st.markdown("##### デモを開始")
    aliases = load_column_aliases()
    mode = st.radio("入力方法", ["内蔵サンプルデータで試す", "CSVをアップロードして試す"], horizontal=True, key="input_mode")

    if st.session_state.get("_prev_input_mode") != mode:
        _reset_mode_session_state()
        st.session_state["_prev_input_mode"] = mode

    if mode == "内蔵サンプルデータで試す":
        st.session_state["active_data_source_label"] = "内蔵サンプルデータ"
        st.markdown('<span class="source-badge">使用中のデータ：内蔵サンプルデータ</span>', unsafe_allow_html=True)
        st.caption("架空利用者 A001〜D001 の4ケースを使用しています。")
        render_demo_data_disclaimer()

        users_df, plans_df, daily_df, monitoring_df = load_sample_bundle()
        user_ids = sorted(set().union(*[
            set(df["user_id"].astype(str)) for df in [users_df, plans_df, daily_df, monitoring_df] if df is not None
        ]))

        st.markdown("---")
        st.subheader("計画書・記録レビュー")
        selected_user = st.selectbox(
            "利用者を選択", user_ids, key="review_user_select",
            format_func=lambda uid: f"{get_user_name(uid, users_df)}（{uid}）" if get_user_name(uid, users_df) else uid,
        )
        _render_plan_record_review_body(selected_user, plans_df, daily_df, monitoring_df, users_df, import_detail=None)
    else:
        st.session_state["active_data_source_label"] = "アップロードしたCSV"
        st.markdown('<span class="source-badge">使用中のデータ：アップロードしたCSV</span>', unsafe_allow_html=True)

        with st.expander("サンプルCSVをダウンロード（そのまま再アップロードして試せます）"):
            st.caption("すべて架空データです。実在利用者の情報は使用していません。")
            dl_cols = st.columns(4)
            sample_files = [
                ("users", "sample_users.csv"),
                ("care_plans", "sample_care_plans.csv"),
                ("daily_records", "sample_daily_records.csv"),
                ("monitoring_records", "sample_monitoring_records.csv"),
            ]
            for col, (schema_key, fname) in zip(dl_cols, sample_files):
                path = DATA_DIR / fname
                if path.exists():
                    col.download_button(
                        CSV_SCHEMAS[schema_key]["label"], data=path.read_bytes(), file_name=fname,
                        mime="text/csv", key=f"dl_{schema_key}",
                    )
        with st.expander("CSV取込確認用データをダウンロード"):
            st.caption("内蔵サンプルとは異なる架空利用者3名のCSVです。CSVアップロード機能の確認に使用できます。")
            ud_cols = st.columns(4)
            upload_demo_files = [
                ("users", "users.csv"), ("care_plans", "care_plans.csv"),
                ("daily_records", "daily_records.csv"), ("monitoring_records", "monitoring_records.csv"),
            ]
            for col, (schema_key, fname) in zip(ud_cols, upload_demo_files):
                path = DATA_DIR / "upload_demo" / fname
                if path.exists():
                    col.download_button(
                        CSV_SCHEMAS[schema_key]["label"], data=path.read_bytes(), file_name=fname,
                        mime="text/csv", key=f"dl_upload_demo_{schema_key}",
                    )
            st.download_button(
                "4ファイルをまとめてダウンロード（ZIP）", data=_build_upload_demo_zip(),
                file_name="upload_demo_csv.zip", mime="application/zip", key="dl_upload_demo_zip",
            )
        st.markdown(
            '<div class="notice-box">最低限、日々の介護記録（daily_records.csv）があれば観察事項の抽出を実行できます。'
            '他のCSVが未アップロードの場合は、下の取込結果の確認で不足情報として表示されます。'
            'これは介護ソフトCSV連携を想定した共通フォーマットPoCであり、特定製品との正式連携ではありません。</div>',
            unsafe_allow_html=True,
        )
        normalized = {}
        normalized["users"] = render_csv_upload_section("users", aliases, normalized)
        normalized["care_plans"] = render_csv_upload_section("care_plans", aliases, normalized)
        normalized["daily_records"] = render_csv_upload_section("daily_records", aliases, normalized)
        normalized["monitoring_records"] = render_csv_upload_section("monitoring_records", aliases, normalized)
        if normalized["daily_records"] is None:
            st.info("daily_records.csvをアップロードすると、取込結果の確認と計画・記録レビューを行えます。")
            st.markdown("---")
            render_export_section()
            render_session_storage_notice()
            _render_bottom_expanders()
            return
        users_df = normalized["users"]
        plans_df = normalized["care_plans"]
        daily_df = normalized["daily_records"]
        monitoring_df = normalized["monitoring_records"]

        st.markdown("---")
        st.subheader("取込結果の確認")
        preview = build_integration_preview(users_df, plans_df, daily_df, monitoring_df)

        total_users = len(preview)
        total_daily = 0 if daily_df is None else len(daily_df)
        total_monitoring = 0 if monitoring_df is None else len(monitoring_df)
        needs_attention = int(preview["import_insufficient"].sum())

        def _render_import_summary_detail():
            m1, m2, m3, m4 = st.columns(4)
            for col, label, value in [
                (m1, "取込利用者数", f"{total_users}人"),
                (m2, "日々の記録件数", f"{total_daily}件"),
                (m3, "モニタリング件数", f"{total_monitoring}件"),
                (m4, "取込時のデータ不足", f"{needs_attention}人"),
            ]:
                col.markdown(f'<div class="stat-card"><div class="label">{label}</div><div class="value">{value}</div></div>', unsafe_allow_html=True)
            st.caption(
                "この数値はCSV取込時のデータ不足を示します。状態変化の確認優先度は、職員確認後に別途判定されます。"
            )
            with st.expander("利用者別の取込内容を見る"):
                display_preview = pd.DataFrame(
                    {
                        "利用者ID": preview["user_id"],
                        "基本情報": preview["has_basic_info"].map({True: "あり", False: "なし"}),
                        "計画書": preview["has_care_plan"].map({True: "あり", False: "なし"}),
                        "日々の記録件数": preview["daily_count"],
                        "モニタリング件数": preview["monitoring_count"],
                        "最終記録日": preview["last_record_date"].apply(lambda d: d.date().isoformat() if pd.notna(d) else "-"),
                        "取込時の確認事項": preview["import_detail"],
                    }
                )
                st.dataframe(display_preview, use_container_width=True, hide_index=True)

        if not st.session_state.get("csv_review_started", False):
            _render_import_summary_detail()
            st.markdown(
                '<div class="notice-box">取込結果を確認 → 利用者を選択 → 観察事項を抽出 → 職員確認 → 確認優先度を表示</div>',
                unsafe_allow_html=True,
            )
            if st.button("アップロードしたデータのレビューを開始", type="primary", key="start_csv_review"):
                st.session_state["csv_review_started"] = True
                st.rerun()
            st.markdown("---")
            render_export_section()
            render_session_storage_notice()
            _render_bottom_expanders()
            return

        st.caption(f"使用中のデータ：アップロードCSV｜利用者{total_users}人｜日々の記録{total_daily}件｜モニタリング{total_monitoring}件")
        with st.expander("取込結果をもう一度見る"):
            _render_import_summary_detail()

        st.markdown("---")
        st.subheader("計画書・記録レビュー")
        selected_user = st.selectbox(
            "レビューする利用者を選択", preview["user_id"].tolist(), key="review_user_select",
            format_func=lambda uid: f"{get_user_name(uid, users_df)}（{uid}）" if get_user_name(uid, users_df) else uid,
        )
        user_preview_row = preview[preview["user_id"] == selected_user].iloc[0]
        import_detail = {
            "insufficient": bool(user_preview_row["import_insufficient"]),
            "detail": user_preview_row["import_detail"],
            "input_status": user_preview_row["input_status"],
        }
        if import_detail["insufficient"]:
            st.warning(f"この利用者はレビューに必要な記録が不足しています（{import_detail['detail']}）。")
        _render_plan_record_review_body(selected_user, plans_df, daily_df, monitoring_df, users_df, import_detail=import_detail)

    st.markdown("---")
    render_export_section()
    render_session_storage_notice()
    _render_bottom_expanders()


def _render_bottom_expanders():
    with st.expander("処理の詳しい仕組みを見る"):
        st.caption("現在の自動抽出は生成AIによる自由推論ではなく、キーワードと業務ルールを用いたルールベース処理です。")
        st.markdown(
            """
            1. CSVの文字コードを判定して読み込む
            2. 異なる列名をアプリ内の標準列へ変換する
            3. 必須項目や日付、利用者IDを確認する
            4. 利用者ID単位で計画書・記録・モニタリングを統合する
            5. キーワードと業務ルールで状態変化候補を抽出する
            6. 職員が採用・修正・対象外を判断する
            7. 職員確認済みの情報から確認優先度を整理する
            8. 次回確認・共有候補と結果CSVを出力する
            """
        )
    st.markdown("## このPoCの対象範囲と将来像")

    with st.expander("このPoCで判断しないこと"):
        st.markdown(
            """
            - 医療上の診断や判断
            - 個別の支援方法の決定
            - ケアプラン変更の確定
            - 服薬方法や医療対応の指示
            - 抽出結果の自動的な採用・対象外判定
            - 記録や支援内容への最終反映
            """
        )
        st.caption(
            "本PoCは、計画書と介護記録から確認材料を整理する意思決定支援ツールです。"
            "本人の状態・希望・現在のケアプランを踏まえた最終判断は、職員・専門職が行います。"
        )

    with st.expander("実運用に必要だが未実装の基盤"):
        st.caption("これらは実運用・製品化に必要となる基盤機能ですが、今回の業務仮説を検証するPoCには実装していません。")
        st.markdown(
            """
            - データベースへの永続保存
            - ログイン認証
            - 職員ごとの権限管理
            - 操作履歴・監査ログ
            - 個人情報を扱うためのアクセス制御と保管ルール
            - バックアップと障害監視
            - 実利用者データを用いた運用検証
            """
        )
        st.caption("公開デモでは実利用者データを使用せず、架空データのみを使用しています。")

    with st.expander("将来の機能拡張"):
        st.caption("以下は今後の発展案であり、現在の公開PoCには実装していません。")
        st.markdown(
            """
            - 介護ソフトとのAPI連携または定期ファイル連携
            - 介護ソフトごとのCSV形式への対応拡大
            - 利用者・訪問予定・ケアプラン・直近記録に応じた確認項目を、介護記録画面内に表示する機能
            - 外部AIによる記録文章の表現補助
            - 職員確認済み内容の多言語・やさしい日本語表示
            - 職員確認後の内容を業務チャットへ共有する機能
            """
        )
        st.caption(
            "多言語・やさしい日本語表示は、正式な翻訳や職員研修を代替するものではなく、"
            "翻訳後も職員確認を必須とし、介護用語の正確性確認を前提とします。"
            "現在の観察候補抽出はルールベース処理であり、外部AIによる文章補助は将来構想の一つです。"
        )


def render_export_section():
    st.subheader("結果の出力")
    reviewed = st.session_state.get("reviewed_users", {})
    if not reviewed:
        st.info("利用者を確認すると、ここから結果CSVをダウンロードできます。")
        return

    st.caption(
        "処理結果をCSVでダウンロードできます。個人名や生の記録内容が含まれる可能性があるため、"
        "実運用時には個人情報管理（アクセス制限・保管ルールなど）が必要です。"
    )

    priority_df = build_review_priority_results_df(reviewed)
    obs_df = build_confirmed_observations_df(reviewed)

    col1, col2 = st.columns(2)
    col1.download_button(
        "review_priority_results.csv をダウンロード",
        data=priority_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="review_priority_results.csv", mime="text/csv", key="dl_priority_results",
    )
    col2.download_button(
        "confirmed_observations.csv をダウンロード",
        data=obs_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="confirmed_observations.csv", mime="text/csv", key="dl_confirmed_observations",
    )


# ============================================================
# 画面2: 利用者一覧・詳細
# ============================================================

def page_user_list():
    st.title("利用者一覧・詳細")
    st.caption("「計画書・記録レビュー」画面で確認した利用者の一覧です。")
    source_label = st.session_state.get("active_data_source_label")
    if source_label:
        st.caption(f"表示中：{source_label}")

    reviewed = st.session_state.get("reviewed_users", {})
    if not reviewed:
        st.info("まだ確認済みの利用者がいません。「計画書・記録レビュー」画面で利用者を確認してください。")
        return

    rows = []
    for uid, info in reviewed.items():
        rows.append(
            {
                "user_id": uid,
                "user_name": info.get("user_name", ""),
                "operational_category": info.get("operational_category", "経過観察"),
                "main_change": info.get("main_change", ""),
                "plan_record_gap": info.get("plan_record_gap", ""),
                "main_source_type": info.get("main_source_type", "-"),
                "import_detail": info.get("import_detail", ""),
                "review_status": info.get("review_status", "未確認"),
            }
        )
    summary_df = pd.DataFrame(rows)
    summary_df["_cat_order"] = summary_df["operational_category"].map(OPERATIONAL_CATEGORY_ORDER)
    summary_df = summary_df.sort_values("_cat_order")

    st.subheader("利用者一覧")
    display_columns = {
        "利用者名": summary_df["user_name"].replace("", "-"),
        "利用者ID": summary_df["user_id"],
        "確認優先度": summary_df["operational_category"],
        "主な状態変化": summary_df["main_change"],
        "計画とのずれ・確認点": summary_df["plan_record_gap"],
        "根拠となる情報源": summary_df["main_source_type"],
        "確認状況": summary_df["review_status"],
    }
    if summary_df["import_detail"].str.strip().any():
        display_columns["取込時の確認事項"] = summary_df["import_detail"].replace("", "-")
    display_df = pd.DataFrame(display_columns)
    st.dataframe(display_df, use_container_width=True, hide_index=True)
    st.caption(f"表示件数: {len(summary_df)}件（優先確認 → 追加情報確認 → 経過観察の順に表示）")

    st.markdown("---")
    st.subheader("利用者詳細")
    selected_user = st.selectbox(
        "利用者を選択", summary_df["user_id"].tolist(),
        format_func=lambda uid: f"{reviewed[uid].get('user_name')}（{uid}）" if reviewed[uid].get("user_name") else uid,
    )
    info = reviewed[selected_user]

    if info.get("user_name"):
        st.markdown(f"#### {info['user_name']}")
        st.caption(f"利用者ID：{selected_user}")
    else:
        st.markdown(f"#### {selected_user}")
        st.caption("利用者名はCSVに含まれていません")

    st.markdown("##### ケアプラン概要")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f'<div class="app-card"><b>支援目標</b><br>{info.get("plan_goal", "-")}</div>', unsafe_allow_html=True)
    c2.markdown(f'<div class="app-card"><b>計画された支援内容</b><br>{info.get("planned_support", "-")}</div>', unsafe_allow_html=True)
    c3.markdown(f'<div class="app-card"><b>観察・留意事項</b><br>{info.get("plan_precautions", "-")}</div>', unsafe_allow_html=True)

    st.markdown("##### 計画内容と実際の記録を比較")
    r1c1, r1c2 = st.columns(2)
    r1c1.markdown(
        f'<div class="compare-card compare-actual"><div class="compare-label">実際の記録</div>{info.get("actual_record_summary", "-")}</div>',
        unsafe_allow_html=True,
    )
    r1c2.markdown(
        f'<div class="compare-card compare-gap"><div class="compare-label">計画とのずれ</div>{info.get("plan_record_gap", "-")}</div>',
        unsafe_allow_html=True,
    )

    st.markdown(render_operational_badge(info["operational_category"]), unsafe_allow_html=True)
    detail_lines = [
        f"<b>主な判定理由</b>：{info.get('priority_reason', '-')}",
        f"<b>職員確認済み</b>：{len(info.get('approved_observations', []))}件",
        f"<b>確認状況</b>：{info.get('review_status', '未確認')}",
    ]
    if info.get("import_detail"):
        detail_lines.append(f"<b>取込時の確認事項</b>：{info['import_detail']}")
    st.markdown(f'<div class="app-card">{"<br>".join(detail_lines)}</div>', unsafe_allow_html=True)

    st.markdown("##### 職員確認済みの観察事項")
    obs = info.get("approved_observations", [])
    if obs:
        obs_display = pd.DataFrame(
            [
                {
                    "観察カテゴリ": o["category"],
                    "確認済み内容": o.get("confirmed_text", o["evidence"]),
                    "根拠文章": o.get("evidence", ""),
                    "記録日": str(o.get("record_date", "-")),
                    "情報源": o.get("source_type", "-"),
                    "職員判断": o.get("staff_decision", "-"),
                }
                for o in obs
            ]
        )
        st.dataframe(obs_display, use_container_width=True, hide_index=True)
    else:
        st.caption("職員確認済みの観察事項はありません。")

    st.markdown("---")
    render_share_candidates_section(obs, key_suffix=f"list_{selected_user}", record_state_level=info.get("record_state_level"))


# ============================================================
# メイン
# ============================================================

def main():
    inject_css()
    check_required_files()
    st.session_state.setdefault("reviewed_users", {})

    page = render_sidebar()

    if page == PAGES[0]:
        page_plan_record_review()
    elif page == PAGES[1]:
        page_user_list()


if __name__ == "__main__":
    main()
