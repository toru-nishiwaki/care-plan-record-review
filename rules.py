"""業務ロジック（Streamlit非依存の純粋関数・定数）。

観察事項抽出、優先度判定、次回確認・対応・共有候補の算出、期間指定の解決、
Human-in-the-loopの確定可否判定など、UIを伴わないルールベース処理をまとめる。
app.py（Streamlit UI）から明示的にimportして利用する。
"""

import calendar
from datetime import date, timedelta

import pandas as pd


EXTRACTION_RULES = [
    {"category": "服薬管理", "keywords": [
        "薬が数日分残っ", "薬が残っ", "服薬したかどうか", "服薬したか分から",
        "服薬状況の確認が必要", "飲み忘れ",
    ]},
    {"category": "記憶に関する変化", "keywords": [
        "同じ質問を何度も", "もの忘れが増えた", "少し前の出来事を覚えていない", "思い出せない",
    ]},
    {"category": "ADL・IADLの変化", "keywords": [
        "介助が必要だった", "ふらつきが見られた", "食事の準備が難しく", "移動に介助",
    ]},
    {"category": "以前の状態との差", "keywords": ["以前は自分で", "以前は自立"]},
    {"category": "食事・水分", "keywords": ["食事量が減", "水分摂取が少な", "食欲がない"]},
    {"category": "睡眠", "keywords": ["夜間に何度も", "不眠", "眠れない", "昼夜逆転"]},
    {"category": "行動面の変化", "keywords": ["落ち着かない", "興奮", "大声を出す", "徘徊"]},
    {"category": "安全面の懸念", "keywords": [
        "転倒", "火の始末", "外出後に戻れ", "戻れなくな", "ガスの火",
    ]},
    {"category": "排泄・皮膚の変化", "keywords": [
        "漏れが続いて", "漏れが見られ", "交換回数が増え", "交換の間隔が",
        "発赤が見られ", "かぶれが見られ", "皮膚に傷", "交換時に痛み", "交換を拒否",
    ]},
    {"category": "口腔ケアの変化", "keywords": [
        "口腔ケアを拒否", "歯みがきを拒否", "口の中の痛み", "出血が見られ",
        "食物残渣が", "義歯の管理状況が変わ", "義歯の洗浄ができてい",
    ]},
]


OBSERVATION_CATEGORIES = [r["category"] for r in EXTRACTION_RULES] + ["情報不足"]


MIN_SUBSTANTIVE_LENGTH = 15


def extract_observations_from_text(text, record_date, source_type, allow_info_insufficient: bool = True) -> list:
    """任意のテキストから観察事項候補を抽出する（ルールベース）。

    どのキーワードがどのカテゴリに対応したかを追跡できるよう、
    一致したキーワードをそのまま結果に含める。ケアプランなど「短くて当然」の
    項目についてはallow_info_insufficient=Falseとし、情報不足タグを付けない。
    """
    text = "" if text is None else str(text)
    if not text.strip() or text.strip().lower() == "nan":
        return []
    observations = []
    for rule in EXTRACTION_RULES:
        matched_keyword = next((kw for kw in rule["keywords"] if kw in text), None)
        if matched_keyword:
            observations.append(
                {
                    "category": rule["category"],
                    "matched_keyword": matched_keyword,
                    "evidence": text,
                    "record_date": record_date,
                    "source_type": source_type,
                }
            )
    if not observations and allow_info_insufficient and len(text.strip()) < MIN_SUBSTANTIVE_LENGTH:
        observations.append(
            {
                "category": "情報不足",
                "matched_keyword": None,
                "evidence": text,
                "record_date": record_date,
                "source_type": source_type,
            }
        )
    return observations


def compute_record_state_level_from_texts(texts: list, approved_observations: list) -> str:
    """介護記録上の状態変化レベルを判定するデモ用の単純なルール。

    職員が「採用」または「修正して採用」とした観察事項のみを対象とする。
    医学的に検証されたルールではなく、現場運用を説明するためのものである。
    """
    non_info = [o["category"] for o in approved_observations if o["category"] != "情報不足"]
    if non_info:
        safety_or_medication = any(c in ("安全面の懸念", "服薬管理", "排泄・皮膚の変化") for c in non_info)
        if safety_or_medication or len(set(non_info)) >= 2:
            return "高"
        return "中"
    substantive = [t for t in texts if len(str(t).strip()) >= MIN_SUBSTANTIVE_LENGTH]
    if not substantive:
        return "情報不足"
    return "低"


def compute_operational_category_from_tracks(record_state_level: str, model_risk_level) -> str:
    """系統A（モデルスコア）と系統B（記録上の状態変化）を統合した運用区分の判定（サンプルデータ用）。"""
    if record_state_level == "高":
        return "優先確認"
    if model_risk_level == "High":
        return "優先確認"
    if record_state_level == "情報不足":
        return "追加情報確認"
    return "経過観察"


def compute_operational_category_csv(record_state_level: str, model_risk_level, input_insufficient: bool) -> str:
    """系統A・系統B・取込データの充足状況を統合した運用区分の判定（CSVアップロード用）。

    安全面・服薬管理の懸念や複数カテゴリの変化が確認された場合は優先確認、
    判断に必要な記録が本当に不足している場合のみ追加情報確認とする。
    軽微な取込不足だけでは追加情報確認に落とさない。
    医学的判断ではなく、記録確認を標準化するためのデモ用業務ルールである。
    """
    if record_state_level == "高":
        return "優先確認"
    if model_risk_level == "High":
        return "優先確認"
    if input_insufficient:
        return "追加情報確認"
    if record_state_level == "情報不足":
        return "追加情報確認"
    return "経過観察"


CATEGORY_REASON_PHRASES = {
    "服薬管理": "残薬・服薬状況",
    "記憶に関する変化": "記憶面の変化",
    "ADL・IADLの変化": "生活動作面の変化",
    "以前の状態との差": "以前の状態との差",
    "安全面の懸念": "安全面の懸念",
    "排泄・皮膚の変化": "排泄・皮膚面の変化",
    "口腔ケアの変化": "口腔ケア面の変化",
    "食事・水分": "食事・水分摂取の変化",
    "睡眠": "睡眠状況の変化",
    "行動面の変化": "行動面の変化",
}


def build_priority_reason(
    approved_observations: list,
    input_insufficient: bool = False,
    record_state_level: str = None,
) -> str:
    """確認優先度の「主な判定理由」を、職員確認済みの観察カテゴリから短文で示すデモ用ルール。

    情報不足ケース（例：U103）と、明確な状態変化候補がない安定ケース（例：D001）は
    意味が異なるため、record_state_level を用いて文言を明確に分ける。
    """
    if input_insufficient or record_state_level == "情報不足":
        return "状態変化を判断するための記録・モニタリング情報が不足しているため"
    categories = [o["category"] for o in approved_observations if o["category"] != "情報不足"]
    unique_categories = list(dict.fromkeys(categories))
    phrases = [CATEGORY_REASON_PHRASES.get(c, c) for c in unique_categories]
    if not phrases:
        return "今回の抽出ルールでは、状態変化を示す観察候補が検出されなかったため"
    if len(phrases) >= 2:
        return "、".join(phrases) + "が複数の記録で確認されたため"
    return f"{phrases[0]}が記録で継続して確認されたため"


# 支援・共有候補（職員確認済みの観察カテゴリに基づくルールベース候補）。
# 「次回訪問で確認すること」「訪問時の対応候補」「事業所内で共有すること」
# 「関係職種への共有を検討する条件」の4種類に分けて表示する。
# Random Forestの重要度とは無関係の、説明可能な固定ルールである。外部LLM APIは使用しない。
# ※おむつの種類・製品・サイズの提案や変更指示、医療的な処置の指示は行わない。
# 各カテゴリのキー:
#   visit       次回訪問で確認すること（観察可能な事実を具体的な行動として確認する項目）
#   action      訪問時の対応候補（本人主体の確認方法・現在のケアプラン範囲内の関わり方）
#   office      事業所内で共有すること（記録に含まれる事実、次回確認すべき内容に限定）
#   share_when  関係職種への共有を検討する条件（アプリが共有先・対応を確定するものではない）
#   external    共有を検討する場合の共有先候補
SHARE_CANDIDATES_BY_CATEGORY = {
    "服薬管理": {
        "visit": [
            "残薬の有無と、確認できる場合は日付・数量",
            "服薬カレンダーとのずれ",
            "本人が服薬状況を把握しているか",
            "受け答えや説明理解に前回との差がないか",
        ],
        "action": [
            "本人に服薬状況と困りごとを確認する",
            "現在の計画に沿って必要な声かけ・見守りを行う",
            "残薬や理解状況の変化を、推測せず具体的に記録する",
        ],
        "office": [
            "確認した残薬の日付・数量",
            "本人の服薬状況に関する説明",
            "同様の状態が継続しているか",
        ],
        "share_when": [
            "残薬や飲み忘れの訴えが複数回続く",
            "本人の説明と記録内容の違いが継続する",
            "現在の声かけ・見守りだけでは状況確認が難しい状態が続く",
        ],
        "external": ["サービス提供責任者", "ケアマネジャー", "看護職", "医療職"],
    },
    "記憶に関する変化": {
        "visit": [
            "同じ質問を繰り返す場面",
            "日時や予定の理解",
            "直前の説明を覚えているか",
            "支援手順の理解",
        ],
        "action": [
            "否定や訂正を急がず、本人の説明を確認する",
            "一度に多くの説明をせず、必要な内容を簡潔に伝える",
            "観察した事実と職員の推測を分けて記録する",
        ],
        "office": [
            "同じ質問や説明理解の変化が見られた場面",
            "以前と比べた受け答えの違い",
            "同様の状態が続いているか",
        ],
        "share_when": [
            "同じ質問や理解の変化が複数回続く",
            "日時・予定の理解や支援手順の理解に変化が続く",
            "本人や家族から生活状況について新たな情報がある",
        ],
        "external": ["サービス提供責任者", "ケアマネジャー"],
    },
    "ADL・IADLの変化": {
        "visit": [
            "立ち上がりにかかる時間",
            "ふらつきが見られる場面",
            "手すり等の使用状況",
            "必要な見守り・介助の範囲",
        ],
        "action": [
            "本人のペースを尊重し、自力で行える動作と必要な介助範囲を確認する",
            "現在のケアプランの範囲で必要な見守り・介助を行う",
            "介助量が変化した場面と対応内容を具体的に記録する",
        ],
        "office": [
            "以前は自力で行えていた動作に介助が必要となった場面",
            "ふらつきが見られた日時・場所・動作",
            "前回と比較した介助量の変化",
        ],
        "share_when": [
            "同様の変化や介助量の増加が複数回続く",
            "ふらつきや痛み、不安が継続する",
            "現在のケアプランと実際に必要な支援に差が続く",
        ],
        "external": ["サービス提供責任者", "ケアマネジャー"],
    },
    "以前の状態との差": {
        "visit": ["以前できていた動作との差を確認する"],
        "action": ["前回までの状態と比べた変化を確認する"],
        "office": ["以前と比べて変化が見られた具体的な場面"],
        "share_when": ["同様の変化が複数回続く"],
        "external": ["ケアマネジャー"],
    },
    "安全面の懸念": {
        "visit": ["転倒しそうになった場面", "火の始末や外出時の様子"],
        "action": ["急がせず、本人の動作や様子を見守る", "気づいた場面を具体的に記録する"],
        "office": ["安全面の懸念が見られた具体的な場面と状況"],
        "share_when": ["安全面の懸念が複数回続く", "現在の支援内容では対応が難しい状態が続く"],
        "external": ["サービス提供責任者", "看護職", "医療職"],
    },
    "排泄・皮膚の変化": {
        "visit": [
            "漏れが起きた時間帯",
            "交換回数や交換間隔の変化",
            "発赤、かぶれ、傷の有無",
            "交換時の痛みや拒否",
        ],
        "action": [
            "本人へ痛みや不快感の有無を確認する",
            "現在のケアプランに沿って排泄支援を行う",
            "皮膚状態や本人の訴えの変化を、推測せず具体的に記録する",
        ],
        "office": [
            "発赤・かぶれ・傷など皮膚状態の変化",
            "漏れや交換間隔の変化が続いているか",
            "交換時の痛みや拒否の有無",
        ],
        "share_when": [
            "発赤・かぶれ・傷など皮膚状態の変化が続く",
            "漏れや交換間隔の変化が複数回続く",
            "交換時の痛みや拒否が継続する",
        ],
        "external": ["サービス提供責任者", "ケアマネジャー", "看護職", "医療職"],
    },
    "口腔ケアの変化": {
        "visit": [
            "口腔内の汚れ",
            "出血、痛みの有無",
            "義歯の状態",
            "口腔ケアへの拒否",
        ],
        "action": [
            "本人へ痛みや不快感を確認する",
            "本人が可能な部分は本人に行ってもらう",
            "急がせず、説明しながら現在の計画に沿って支援する",
        ],
        "office": [
            "以前と比べて口腔ケアへの拒否や介助量が変化した場面",
            "出血や痛みなど口腔内の状態変化",
            "同様の状態が続いているか",
        ],
        "share_when": [
            "出血や痛みが継続する",
            "口腔ケアへの拒否が複数回続く",
            "現在の支援方法では対応が難しい状態が続く",
        ],
        "external": ["サービス提供責任者", "ケアマネジャー", "看護職", "医療職", "歯科関係職"],
    },
    "食事・水分": {
        "visit": ["食事量と水分摂取の状況", "食欲や飲み込みの変化"],
        "action": ["本人のペースで食事・水分摂取を促す", "摂取量の変化を具体的に記録する"],
        "office": ["食事量・水分摂取量の変化が見られた場面"],
        "share_when": ["食事・水分摂取量の低下が複数回続く"],
        "external": ["サービス提供責任者", "ケアマネジャー"],
    },
    "睡眠": {
        "visit": ["夜間の睡眠状況", "日中の様子への影響"],
        "action": ["本人へ睡眠状況を確認する", "生活リズムの変化を具体的に記録する"],
        "office": ["睡眠状況・生活リズムの変化が見られた場面"],
        "share_when": ["睡眠状況の変化が複数回続く"],
        "external": ["ケアマネジャー"],
    },
    "行動面の変化": {
        "visit": ["行動面の変化が見られた状況・時間帯", "きっかけとなった出来事の有無"],
        "action": ["急がせず、本人の様子を見守る", "状況・時間帯を具体的に記録する"],
        "office": ["行動面の変化が見られた具体的な場面と状況"],
        "share_when": ["行動面の変化が複数回続く", "現在の支援内容では対応が難しい状態が続く"],
        "external": ["サービス提供責任者", "ケアマネジャー", "医療職"],
    },
    # 情報不足ケース（例：U103）専用。判断できない状態を正確に表現し、
    # 具体的な訪問時対応候補や共有条件は無理に生成しない。
    "情報不足": {
        "visit": [
            "現在の記録だけでは状態変化を判断する情報が不足しています",
            "次回訪問時に、具体的な変化の内容と本人の訴えを確認する",
        ],
        "action": [],
        "office": ["不足している記録やモニタリングの有無を確認する"],
        "share_when": [],
        "external": [],
    },
}


# 職員確認済みの観察事項が一件もない（今回の抽出ルールでは状態変化候補が検出されなかった）場合の表示。
# 不必要に不安をあおる候補を作らず、通常の訪問時確認を促す落ち着いた内容とする。
# ただし「安定している」「安全である」と断定せず、あくまで今回の抽出結果であることが伝わる表現にとどめる。
STABLE_CASE_VISIT = [
    "本人の状態や希望に変化がないか、通常の訪問時に確認する",
    "変化が見られた場合は、具体的な場面と内容を記録する",
]


STABLE_CASE_ACTION = [
    "現在のケアプランに沿った支援を継続する",
    "本人の状態や希望を確認しながら支援する",
]


STABLE_CASE_OFFICE = ["現時点では、追加共有を急ぐ状態変化は記録されていない"]


STABLE_CASE_SHARE_NOTE = "状態変化が継続して確認された場合に共有を検討する"


# 「関係職種への共有を検討する条件」の文面で、まず内容を確認する内部の役割として固定的に用いる。
# アプリが共有先や対応を確定するものではなく、あくまで確認・検討の入り口を示す表現とする。
SHARE_CONFIRMER_ROLE = "サービス提供責任者"


# 「計画とのずれ」表示用のルールベース定型文（観察カテゴリ単位）。
# ケアプラン変更や医学的判断を確定するものではない。
PLAN_GAP_TEMPLATES = {
    "服薬管理": "現在の声かけ・見守りだけでは、服薬状況を十分に把握できていない可能性があります。残薬状況と本人の管理方法を再確認する必要があります。",
    "記憶に関する変化": "もの忘れ等の記憶面の変化が、計画作成時から進んでいる可能性があります。",
    "ADL・IADLの変化": "計画作成時と比べて、生活動作の自立度が変化している可能性があります。",
    "以前の状態との差": "計画作成時の状態と、現在の状態に差が生じている可能性があります。",
    "安全面の懸念": "安全面について、計画作成時には想定していなかった懸念が生じている可能性があります。",
    "排泄・皮膚の変化": "排泄支援の方法や頻度が、現在の状態に合っていない可能性があります。",
    "口腔ケアの変化": "口腔ケアの方法が、本人の現在の状態に合っていない可能性があります。",
    "食事・水分": "食事・水分摂取の状況が、計画作成時と変化している可能性があります。",
    "睡眠": "睡眠の状況が、計画作成時と変化している可能性があります。",
    "行動面の変化": "行動面について、計画作成時には見られなかった変化が生じている可能性があります。",
    "情報不足": "記録の情報量が少なく、計画との比較が難しい状態です。",
}


# カテゴリの優先度ランク（数字が小さいほど「主な判定理由」の主要カテゴリになりやすい）。
# 「以前の状態との差」のような汎用・横断的カテゴリより、服薬管理・ADL等の
# 領域固有カテゴリを優先して主要カテゴリに選ぶための重み付け。
CATEGORY_PRIORITY_RANK = {
    "服薬管理": 1,
    "排泄・皮膚の変化": 1,
    "口腔ケアの変化": 1,
    "ADL・IADLの変化": 1,
    "記憶に関する変化": 1,
    "安全面の懸念": 1,
    "食事・水分": 2,
    "睡眠": 2,
    "行動面の変化": 2,
    "以前の状態との差": 3,
}


# 結果画面の各区分で表示する候補の最大数（読み切れる量に絞るための上限）。
SECTION_LIMITS = {"visit": 4, "action": 3, "office": 3, "share_when": 3}


# 主要カテゴリ以外（副次カテゴリ）から補う候補は、区分ごとに最大この件数までとする。
SECONDARY_CATEGORY_LIMIT = 1


def _determine_primary_category(categories: list):
    """複数の観察カテゴリから、「主な判定理由」に対応する主要カテゴリを1つ選ぶ。

    検出件数が多いカテゴリを優先しつつ、件数が同数の場合はCATEGORY_PRIORITY_RANKで
    領域固有カテゴリを優先し、それでも同順位なら記録内での出現順で決める。
    """
    if not categories:
        return None
    counts, first_seen = {}, {}
    for idx, c in enumerate(categories):
        counts[c] = counts.get(c, 0) + 1
        first_seen.setdefault(c, idx)
    unique_categories = list(counts.keys())
    unique_categories.sort(key=lambda c: (CATEGORY_PRIORITY_RANK.get(c, 2), -counts[c], first_seen[c]))
    return unique_categories[0]


def get_share_candidates_grouped(categories: list):
    """職員確認済みの観察カテゴリから、次回確認・対応・共有候補をルールベースで集約する。

    「主な判定理由」と対応する主要カテゴリの候補を優先して採用し、区分ごとの表示上限
    （SECTION_LIMITS）を超えないようにする。他の観察カテゴリ（副次カテゴリ）は、
    主要カテゴリの候補だけでは枠が埋まらない場合に限り、区分ごとに最大
    SECONDARY_CATEGORY_LIMIT件まで補う（似た意味の候補が重複表示されるのを防ぐため）。

    戻り値: (visit, action, office, share_when, external)
    """
    unique_categories = list(dict.fromkeys(categories))
    if not unique_categories:
        return [], [], [], [], []

    primary = _determine_primary_category(categories)
    secondary = [c for c in unique_categories if c != primary]

    visit, action, office, share_when, external = [], [], [], [], []
    limited_buckets = [("visit", visit), ("action", action), ("office", office), ("share_when", share_when)]

    primary_data = SHARE_CANDIDATES_BY_CATEGORY.get(primary, {})
    for bucket, target in limited_buckets:
        limit = SECTION_LIMITS[bucket]
        for item in primary_data.get(bucket, []):
            if len(target) >= limit:
                break
            if item not in target:
                target.append(item)

    for c in secondary:
        d = SHARE_CANDIDATES_BY_CATEGORY.get(c, {})
        for bucket, target in limited_buckets:
            limit = SECTION_LIMITS[bucket]
            added = 0
            for item in d.get(bucket, []):
                if added >= SECONDARY_CATEGORY_LIMIT or len(target) >= limit:
                    break
                if item not in target:
                    target.append(item)
                    added += 1

    for c in [primary] + secondary:
        for item in SHARE_CANDIDATES_BY_CATEGORY.get(c, {}).get("external", []):
            if item not in external:
                external.append(item)

    return visit, action, office, share_when, external


def resolve_share_candidates(approved_observations: list, record_state_level: str = None):
    """次回確認・対応・共有候補を解決する。画面表示とCSV出力の両方から共通で呼び出し、内容を一致させる。

    record_state_level が「情報不足」の場合（例：U103）は、状態変化の有無を判断する情報
    そのものが不足していることを示す専用の候補とし、具体的な訪問時対応候補や関係職種への
    共有条件は無理に生成しない。これは、明確な状態変化候補が見つからない安定ケース
    （例：D001）とは意味が異なるため、区別して扱う。

    戻り値: (visit, action, office, share_when, external, case)
    case は "info_insufficient" / "stable" / "normal" のいずれか。
    """
    approved_categories = [o["category"] for o in approved_observations if o["category"] != "情報不足"]

    if record_state_level == "情報不足":
        info_data = SHARE_CANDIDATES_BY_CATEGORY["情報不足"]
        return list(info_data["visit"]), [], list(info_data["office"]), [], [], "info_insufficient"

    if not approved_categories:
        return (
            list(STABLE_CASE_VISIT),
            list(STABLE_CASE_ACTION),
            list(STABLE_CASE_OFFICE),
            [STABLE_CASE_SHARE_NOTE],
            [],
            "stable",
        )

    visit, action, office, share_when, external = get_share_candidates_grouped(approved_categories)
    return visit, action, office, share_when, external, "normal"


def compute_input_status(has_basic_info: bool, has_care_plan: bool, daily_count: int, monitoring_count: int, last_record_days) -> str:
    """入力情報の状態（旧データ参考度）を、記録・計画書の充足度から判定するデモ用ルール。

    医学的な信頼度指標ではなく、現場運用を検討するためのデモ用業務指標である。
    """
    total = daily_count + monitoring_count
    if total < 2:
        return "判断材料不足"
    points = int(has_basic_info) + int(has_care_plan)
    if last_record_days is not None and last_record_days <= 30:
        points += 1
    if total >= 3:
        points += 1
    if points >= 4:
        return "情報は十分"
    if points >= 2:
        return "一部不足"
    return "更新が必要"


def compute_input_status_detail(has_basic_info: bool, has_care_plan: bool, daily_count: int, monitoring_count: int, last_record_days) -> dict:
    """取込データの不足について、具体的な理由と「判断不能」かどうかを示すデモ用ルール。

    抽象的なラベルだけでなく、必ず具体的な不足理由を併記するために使う。
    日々の記録・モニタリング記録がどちらも0件の場合のみ、レビュー不能（insufficient）とする。
    それ以外の軽微な不足はレビュー継続可能とし、取込確認画面にのみ表示する。
    """
    reasons = []
    if not has_basic_info:
        reasons.append("利用者基本情報の一部項目が未入力です")
    if not has_care_plan:
        reasons.append("訪問介護計画書を確認できません")
    if daily_count == 0:
        reasons.append("日々の介護記録がありません")
    if monitoring_count == 0:
        reasons.append("モニタリング記録がありません")
    if daily_count > 0 and last_record_days is not None and last_record_days > 90:
        reasons.append("日々の介護記録はありますが、最終記録日が古い状態です")

    insufficient = daily_count == 0 and monitoring_count == 0
    if insufficient:
        detail = "要確認：" + (reasons[0] if reasons else "記録が確認できません")
    elif reasons:
        detail = "／".join(reasons)
    else:
        detail = "特になし"
    return {"reasons": reasons, "insufficient": insufficient, "detail": detail}


PERIOD_CHOICES = ["すべての期間（指定なし）", "今日", "過去7日間", "今月", "前月", "期間を指定"]


def resolve_period_range(choice: str, custom_start=None, custom_end=None):
    """観察事項抽出の対象期間選択肢から (start_date, end_date) を解決する。

    「すべての期間（指定なし）」の場合は (None, None) を返し、無制限（全期間）を表す。
    「今月」「前月」は暦月の初日〜末日、「過去7日間」は本日を含む直近7日間とする。
    """
    today = date.today()
    if choice == "今日":
        return today, today
    if choice == "過去7日間":
        return today - timedelta(days=6), today
    if choice == "今月":
        last_day = calendar.monthrange(today.year, today.month)[1]
        return date(today.year, today.month, 1), date(today.year, today.month, last_day)
    if choice == "前月":
        last_month_end = date(today.year, today.month, 1) - timedelta(days=1)
        return date(last_month_end.year, last_month_end.month, 1), last_month_end
    if choice == "期間を指定":
        return custom_start, custom_end
    return None, None


def _within_date_range(date_series, start_date, end_date):
    """日付列（datetime64）が [start_date, end_date] の範囲内（両端含む）にあるかを示す真偽値Seriesを返す。

    start_date/end_dateがNoneの側は無制限として扱う。日付が欠損（NaT）の行はFalseとする。
    """
    mask = date_series.notna()
    if start_date is not None:
        mask &= date_series.dt.date >= start_date
    if end_date is not None:
        mask &= date_series.dt.date <= end_date
    return mask


def extract_observations_for_user_csv(user_id: str, daily_df, monitoring_df, start_date=None, end_date=None) -> list:
    """利用者1名分の観察事項候補を、日々の介護記録・モニタリング記録のみから抽出する。

    訪問介護計画書（plans_df）は「計画内容と実際の記録を比較」の比較基準として別途扱い、
    ここでは抽出対象に含めない。計画書に書かれた留意事項・支援方針は、
    実際に起きた出来事（観察事実）ではないため。
    start_date/end_date を指定すると、その期間（両端含む）に含まれる記録のみを対象にする。
    """
    observations = []
    if daily_df is not None:
        sub = daily_df[daily_df["user_id"].astype(str) == user_id]
        if start_date is not None or end_date is not None:
            sub = sub[_within_date_range(sub["record_date"], start_date, end_date)]
        for _, r in sub.iterrows():
            date_str = r["record_date"].date().isoformat() if pd.notna(r["record_date"]) else "日付不明"
            observations += extract_observations_from_text(r.get("record_text"), date_str, "日々の介護記録")
            if str(r.get("special_notes")).strip() and str(r.get("special_notes")).lower() != "nan":
                observations += extract_observations_from_text(r.get("special_notes"), date_str, "日々の介護記録（特記事項）")
    if monitoring_df is not None:
        sub = monitoring_df[monitoring_df["user_id"].astype(str) == user_id]
        if start_date is not None or end_date is not None:
            sub = sub[_within_date_range(sub["monitoring_date"], start_date, end_date)]
        for _, r in sub.iterrows():
            date_str = r["monitoring_date"].date().isoformat() if pd.notna(r["monitoring_date"]) else "日付不明"
            for field, label in [
                ("current_status", "モニタリング記録"),
                ("change_from_previous", "モニタリング記録（前回からの変化）"),
                ("issues", "モニタリング記録（課題）"),
            ]:
                val = r.get(field)
                if val is not None and str(val).strip() and str(val).lower() != "nan":
                    observations += extract_observations_from_text(val, date_str, label)
    return observations


def collect_substantive_texts_csv(user_id: str, daily_df, monitoring_df) -> list:
    texts = []
    if daily_df is not None:
        texts += daily_df.loc[daily_df["user_id"].astype(str) == user_id, "record_text"].astype(str).tolist()
    if monitoring_df is not None:
        texts += monitoring_df.loc[monitoring_df["user_id"].astype(str) == user_id, "current_status"].astype(str).tolist()
    return texts


def build_review_priority_results_df(reviewed_users: dict) -> pd.DataFrame:
    rows = []
    for uid, info in reviewed_users.items():
        approved_observations = info.get("approved_observations", [])
        record_state_level = info.get("record_state_level")
        visit, action, office, share_when, external, case = resolve_share_candidates(
            approved_observations, record_state_level
        )
        if case == "normal" and external:
            share_targets = "・".join(r for r in external if r != SHARE_CONFIRMER_ROLE) or "関係職種"
            external_share_conditions = "／".join(share_when) + f"（確認：{SHARE_CONFIRMER_ROLE}／共有検討先：{share_targets}）"
        elif case == "stable":
            external_share_conditions = share_when[0]
        else:
            external_share_conditions = "／".join(share_when)
        rows.append(
            {
                "user_id": uid,
                "user_name": info.get("user_name") or "氏名未登録",
                "operational_priority": info.get("operational_category", ""),
                "priority_reason": info.get("priority_reason", ""),
                "main_change": info.get("main_change", ""),
                "source_type": info.get("main_source_type", ""),
                "source_date": info.get("main_source_date", ""),
                "input_information_status": info.get("input_status", ""),
                "missing_information_detail": info.get("import_detail", ""),
                "review_status": info.get("review_status", ""),
                "planned_support": info.get("planned_support", ""),
                "actual_record_summary": info.get("actual_record_summary", ""),
                "plan_record_gap": info.get("plan_record_gap", ""),
                "next_visit_checks": "／".join(visit),
                "visit_action_candidates": "／".join(action),
                "office_share_items": "／".join(office),
                "external_share_conditions": external_share_conditions,
            }
        )
    return pd.DataFrame(rows)


def build_confirmed_observations_df(reviewed_users: dict) -> pd.DataFrame:
    rows = []
    for uid, info in reviewed_users.items():
        for obs in info.get("approved_observations", []):
            rows.append(
                {
                    "user_id": uid,
                    "user_name": info.get("user_name") or "氏名未登録",
                    "observation_category": obs.get("category", ""),
                    "confirmed_observation": obs.get("confirmed_text", obs.get("evidence", "")),
                    "evidence_text": obs.get("evidence", ""),
                    "record_date": obs.get("record_date", ""),
                    "source_type": obs.get("source_type", ""),
                    "staff_decision": obs.get("staff_decision", ""),
                }
            )
    return pd.DataFrame(rows)


def has_unreviewed_decisions(decisions: list) -> bool:
    """Human-in-the-loopの中核ルール：観察候補に「未確認」が1件でも残っていれば確定不可とする。

    decisions は (obs, decision, corrected_text) のタプル列。UIに依存しない純粋関数。
    """
    return any(decision == "未確認" for _, decision, _ in decisions)


def _finalize_decisions(decisions: list) -> list:
    approved = []
    for obs, decision, corrected_text in decisions:
        if decision == "対象外":
            continue
        final_obs = dict(obs)
        final_obs["confirmed_text"] = corrected_text if (decision == "内容を整えて採用" and corrected_text) else obs["evidence"]
        final_obs["staff_decision"] = decision
        approved.append(final_obs)
    return approved


def _compute_plan_record_comparison(selected_user, plan_row, daily_df, monitoring_df):
    """計画内容と実際の記録の比較（職員確認前のルールベース速報プレビュー）を作成する。

    ここで示す「計画とのずれ」は確定情報ではなく、次の「抽出結果の確認」で
    職員が採否を判断したうえで初めて確認優先度に反映される。
    常に全期間の記録を対象とする（下部の「観察事項を抽出」の期間指定はここには適用されない）。
    """
    raw_observations = extract_observations_for_user_csv(selected_user, daily_df, monitoring_df)
    seen_categories, preview_items = set(), []
    for o in raw_observations:
        if o["category"] not in seen_categories:
            seen_categories.add(o["category"])
            preview_items.append(o)

    change_text = "記録なし"
    if monitoring_df is not None:
        m = monitoring_df[monitoring_df["user_id"].astype(str) == selected_user]
        if "change_from_previous" in m.columns:
            vals = [str(v) for v in m["change_from_previous"].tolist() if pd.notna(v) and str(v).strip()]
            if vals:
                change_text = vals[-1]

    non_info_items = [o for o in preview_items if o["category"] != "情報不足"]
    unique_evidence = list(dict.fromkeys(o["evidence"][:40] for o in non_info_items))
    actual_summary = "／".join(unique_evidence) if unique_evidence else "記録上、大きな変化は確認されていません。"

    gap_lines = list(dict.fromkeys(
        PLAN_GAP_TEMPLATES[o["category"]] for o in preview_items if o["category"] in PLAN_GAP_TEMPLATES
    ))
    gap_text = " ".join(gap_lines) if gap_lines else "計画作成時からの大きなずれは確認されていません。"

    planned_support = plan_row.get("support_content", "-") if plan_row is not None else "（計画書未登録）"
    return planned_support, actual_summary, change_text, gap_text
