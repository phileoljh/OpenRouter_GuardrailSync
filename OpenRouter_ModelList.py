import os
import sys
from datetime import datetime
import requests
from dotenv import load_dotenv

# 1. 載入 .env 環境變數
load_dotenv()

MANAGEMENT_KEY = os.getenv("OPENROUTER_MANAGEMENT_KEY")
GUARDRAIL_ID = os.getenv("OPENROUTER_GUARDRAIL_ID")

if not MANAGEMENT_KEY:
    print("【錯誤】未在 .env 中讀取到 OPENROUTER_MANAGEMENT_KEY，請確認設定。")
    sys.exit(1)

try:
    max_input_m = float(os.getenv("MAX_INPUT_PRICE_PER_M", "0.25"))
    max_output_m = float(os.getenv("MAX_OUTPUT_PRICE_PER_M", "0.50"))
except ValueError:
    print("【錯誤】費率門檻必須為數字，請檢查 .env。")
    sys.exit(1)

# 換算為每 Token 單價
MAX_INPUT_PRICE_PER_TOKEN = max_input_m / 1_000_000
MAX_OUTPUT_PRICE_PER_TOKEN = max_output_m / 1_000_000

HEADERS = {
    "Authorization": f"Bearer {MANAGEMENT_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://localhost",
    "X-Title": "Guardrail Model Sync & Markdown Export",
}


def fetch_all_models_raw() -> list[dict]:
    """呼叫 OpenRouter API 取得全量模型清單與完整定價資料"""
    url = "https://openrouter.ai/api/v1/models"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
    except requests.RequestException as e:
        print(f"【錯誤】無法取得 OpenRouter 模型列表: {e}")
        sys.exit(1)

    raw_models = res.json().get("data", [])
    parsed_models = []

    for model in raw_models:
        model_id = model.get("id", "")
        model_name = model.get("name") or model_id
        canonical_slug = model.get("canonical_slug")
        pricing = model.get("pricing", {})

        try:
            prompt_price = float(pricing.get("prompt", 0))
            completion_price = float(pricing.get("completion", 0))
        except (ValueError, TypeError):
            prompt_price = 0.0
            completion_price = 0.0

        # 解析廠商名稱 (Provider)，例如 google/gemini-2.5-flash -> google
        if "/" in model_id:
            provider = model_id.split("/", 1)[0]
        else:
            provider = "other"

        # 換算每百萬 Token 美元費用
        input_per_m = prompt_price * 1_000_000
        output_per_m = completion_price * 1_000_000

        # 判斷是否為 Batch 模型
        is_batch = (
            ":batch" in model_id.lower()
            or "(batch)" in model_name.lower()
            or model_id.lower().endswith("-batch")
        )

        # 判斷是否為特殊負數費率 (例如特定動態路由或暫停模型)
        is_negative = (prompt_price < 0 or completion_price < 0)

        # 判斷是否為完全免費模型
        is_free = (input_per_m == 0 and output_per_m == 0 and not is_negative)

        parsed_models.append({
            "id": model_id,
            "canonical_slug": canonical_slug,
            "name": model_name,
            "provider": provider,
            "context_length": model.get("context_length", 0),
            "prompt_price": prompt_price,
            "completion_price": completion_price,
            "input_per_m": input_per_m,
            "output_per_m": output_per_m,
            "is_free": is_free,
            "is_batch": is_batch,
            "is_negative": is_negative,
        })

    return parsed_models


def export_all_models_to_markdown(models: list[dict], filename: str = "models_ALL.md"):
    """將全量模型依照廠商 (Provider) 排序並匯出為 Markdown 檔案"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 排序規則：先照廠商小寫排序，再照模型名稱小寫排序
    sorted_models = sorted(
        models,
        key=lambda x: (x["provider"].lower(), x["name"].lower())
    )

    # 統計廠商數目
    providers = set(m["provider"] for m in sorted_models)

    lines = [
        "# OpenRouter 全量模型清單 (All Models Catalog)",
        "",
        f"- **匯出時間**：{now_str}",
        f"- **全量模型總數**：{len(sorted_models)} 款",
        f"- **涵蓋廠商總數**：{len(providers)} 家",
        "- **排序規則**：優先依照「廠商 (Provider)」字母順序分組排序",
        "",
        "| 廠商 (Provider) | 模型名稱 | 模型 ID | Context 視窗 | Input 單價 ($/M) | Output 單價 ($/M) | 類型 | 備註 |",
        "| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
    ]

    for m in sorted_models:
        context_k = f"{m['context_length'] // 1000}k" if m['context_length'] >= 1000 else str(m['context_length'])
        
        if m["is_free"]:
            tag = "免費 (Free)"
        elif m["is_negative"]:
            tag = "動態/特殊"
        else:
            tag = "計費"

        note_items = []
        if m["is_batch"]:
            note_items.append("Batch 模型")
        if m["is_negative"]:
            note_items.append("負數/動態費率")
        note_str = "、".join(note_items) if note_items else "-"

        # 處理負數價格顯示
        in_price_str = f"${m['input_per_m']:.4f}" if not m["is_negative"] else "動態"
        out_price_str = f"${m['output_per_m']:.4f}" if not m["is_negative"] else "動態"

        lines.append(
            f"| `{m['provider']}` | {m['name']} | `{m['id']}` | {context_k} | {in_price_str} | {out_price_str} | {tag} | {note_str} |"
        )

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"全量模型清單已成功匯出至：{os.path.abspath(filename)}（共 {len(sorted_models)} 款）")


def filter_whitelist_models(models: list[dict]) -> list[dict]:
    """依據費率門檻過濾平價與免費模型，並強制排除 Batch 模型與負數特殊模型"""
    qualified = []

    for m in models:
        # 1. 強制排除 Batch 模型
        if m["is_batch"]:
            continue

        # 2. 排除負數/特殊定價模型
        if m["is_negative"]:
            continue

        # 3. 檢查費率門檻（包含完全免費模型）
        if m["prompt_price"] <= MAX_INPUT_PRICE_PER_TOKEN and m["completion_price"] <= MAX_OUTPUT_PRICE_PER_TOKEN:
            qualified.append(m)

    # 白名單依照模型名稱（不區分大小寫）排序
    qualified.sort(key=lambda x: x["name"].lower())
    return qualified


def resolve_model_slug(m: dict) -> str:
    """解析白名單模型的最佳快照 Slug：
    1. 若模型為免費版 (id 帶有 :free)，其快照應為 canonical_slug 加上 :free (如 cohere/north-mini-code-20260617:free)
    2. 一般付費模型優先採用官方快照版 (canonical_slug) 以正確匹配 UI 圖示與正式名稱
    3. 若無 canonical_slug 則 fallback 使用原生 id
    """
    model_id = m.get("id", "")
    canonical_slug = m.get("canonical_slug")

    if not canonical_slug:
        return model_id

    # 免費版模型需在快照版後綴加上 :free，才能精確匹配後台官方免費端點
    if ":free" in model_id:
        if not canonical_slug.endswith(":free"):
            return f"{canonical_slug}:free"
        return canonical_slug

    return canonical_slug


def export_whitelist_to_markdown(models: list[dict], filename: str = "models_whitelist.md"):
    """將過濾後的白名單模型清單匯出為 Markdown 檔案"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# OpenRouter 允許模型清單 (Guardrail 白名單)",
        "",
        f"- **匯出時間**：{now_str}",
        f"- **門檻條件**：Input ≤ ${max_input_m}/M , Output ≤ ${max_output_m}/M（含完全免費模型）",
        "- **過濾規則**：已自動排除所有 Batch 非同步模型與負數特殊模型",
        f"- **符合模型總數**：{len(models)} 款",
        "",
        "| 模型名稱 | 模型 ID | Context 視窗 | Input 單價 ($/M) | Output 單價 ($/M) | 類型 |",
        "| :--- | :--- | :---: | :---: | :---: | :---: |",
    ]

    for m in models:
        context_k = f"{m['context_length'] // 1000}k" if m['context_length'] >= 1000 else str(m['context_length'])
        tag = "免費 (Free)" if m["is_free"] else "平價"
        target_slug = resolve_model_slug(m)
        lines.append(
            f"| {m['name']} | `{target_slug}` | {context_k} | ${m['input_per_m']:.4f} | ${m['output_per_m']:.4f} | {tag} |"
        )

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"白名單檔案已成功生成：{os.path.abspath(filename)}（共 {len(models)} 款）")


def get_workspace_id() -> str | None:
    """自動取得當前 Management Key 所屬的 Workspace ID"""
    url = "https://openrouter.ai/api/v1/guardrails"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        if res.status_code == 200:
            data = res.json().get("data", [])
            if data and "workspace_id" in data[0]:
                return data[0]["workspace_id"]
    except requests.RequestException:
        pass
    return None


def list_guardrail_policies(workspace_id: str | None = None) -> list[dict]:
    """呼叫新版 API 查詢當前 Workspace 下所有的 Model Availability Policies"""
    ws_id = workspace_id or get_workspace_id()
    if not ws_id:
        print("【警告】無法自動解析 Workspace ID，請確認 Management Key 權限。")
        return []

    url = f"https://openrouter.ai/api/v1/guardrails/policies/model-availability?workspace_id={ws_id}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        if res.status_code == 200:
            result = res.json()
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                data = result.get("data")
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    return [data]
                return [result]

        print(f"【查詢失敗】HTTP {res.status_code}: {res.text}")
        return []
    except requests.RequestException as e:
        print(f"【連線異常】無法查詢 Guardrail Policies: {e}")
        return []


def update_guardrail_policy(policy_id: str, model_ids: list[str]) -> bool:
    """呼叫新版 Guardrail Policies API (PUT) 更新模型白名單，自動解析 workspace_id 並保留原安全設定"""
    ws_id = get_workspace_id()
    if not ws_id:
        print("【錯誤】無法取得 Workspace ID，無法進行 Policy 更新。")
        return False

    # 先取得既有 Policy 的詳細資訊（保留原設定名稱與其他安全過濾參數）
    detail_url = f"https://openrouter.ai/api/v1/guardrails/policies/model-availability/{policy_id}?workspace_id={ws_id}"
    try:
        res_get = requests.get(detail_url, headers=HEADERS, timeout=30)
        if res_get.status_code != 200:
            print(f"【錯誤】無法取得政策 ({policy_id}) 詳情: HTTP {res_get.status_code} - {res_get.text}")
            print("\n【除錯建議】：")
            print("1. 請確認該 Policy ID 是否存在於當前 Workspace 中。")
            print("2. 可執行 'python OpenRouter_ModelList.py --list' 查詢現有有效的政策與 ID。")
            return False

        policy_data = res_get.json().get("data", {})
        policy_name = policy_data.get("name", "Model Availability Policy")
        original_config = policy_data.get("config", {})

        # 保留既有的 ZDR、provider 限制等其他欄位，僅更新 allowed_models
        updated_config = {
            **original_config,
            "allowed_models": model_ids
        }

        put_url = f"https://openrouter.ai/api/v1/guardrails/policies/model-availability/{policy_id}"
        payload = {
            "name": policy_name,
            "workspace_id": ws_id,
            "config": updated_config
        }

        response = requests.put(put_url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code in (200, 204):
            print(f"Guardrail 政策「{policy_name}」({policy_id}) 模型白名單更新成功！共寫入 {len(model_ids)} 款模型。")
            return True

        print(f"【更新失敗】HTTP {response.status_code}: {response.text}")
        return False

    except requests.RequestException as e:
        print(f"【錯誤】連線異常: {e}")
        return False


def main():
    # 支援 --list 參數列出帳號下所有 Policies
    if len(sys.argv) > 1 and sys.argv[1] in ("--list", "-l"):
        print("正在查詢 Workspace 下的 Model Availability Policies...")
        policies = list_guardrail_policies()
        if not policies:
            print("目前 Workspace 下未找到任何 Model Availability 政策，或權限不足。")
            return

        print(f"\n共找到 {len(policies)} 個政策：")
        print("-" * 75)
        for p in policies:
            p_id = p.get("id", "未知 ID")
            p_name = p.get("name", "未命名")
            cfg = p.get("config", {}) if isinstance(p.get("config"), dict) else {}
            allowed_models = cfg.get("allowed_models") or p.get("allowed_models")
            allowed_count = len(allowed_models) if allowed_models else "無限制/全部"
            print(f"政策名稱: {p_name}")
            print(f"政策 ID  : {p_id}")
            print(f"白名單數: {allowed_count}")
            print("-" * 75)
        return

    print("1. 正在抓取 OpenRouter 全量模型資料...")
    all_models = fetch_all_models_raw()
    print(f"抓取完成！共取得 {len(all_models)} 款模型。")

    # 產出 models_ALL.md（照廠商排序）
    print("2. 正在生成全量模型清單 (models_ALL.md，依照廠商排序)...")
    export_all_models_to_markdown(all_models)

    # 篩選白名單模型（排除 batch 與負數費率）
    print(f"3. 正在執行費率篩選（門檻：Input ≤ ${max_input_m}/M, Output ≤ ${max_output_m}/M，排除 Batch 模型）...")
    whitelist_models = filter_whitelist_models(all_models)
    print(f"篩選完成！共 {len(whitelist_models)} 款模型符合白名單條件。")

    if not whitelist_models:
        print("未找到符合條件的模型，終止後續同步作業。")
        return

    # 產出 models_whitelist.md
    print("4. 正在生成白名單報表 (models_whitelist.md)...")
    export_whitelist_to_markdown(whitelist_models)

    # 檢查是否設定 GUARDRAIL_ID
    if not GUARDRAIL_ID:
        print("\n【提示】未在 .env 中設定 OPENROUTER_GUARDRAIL_ID。")
        print("正在嘗試為您自動查詢帳號下的 Guardrail Policy ID：")
        policies = list_guardrail_policies()
        if policies:
            for p in policies:
                print(f"  - 名稱: {p.get('name')} | ID: {p.get('id')}")
            print("請將欲維護的 ID 填入 .env 的 OPENROUTER_GUARDRAIL_ID 後重新執行。")
        return

    # 同步寫入 Guardrail Policy
    print(f"5. 正在同步更新 Guardrail Policy: {GUARDRAIL_ID} ...")
    # 採用經防呆解析的最佳 Slug（免費模型保留原生 :free，平價模型優先 canonical_slug），維持 1:1 純淨對應
    model_ids = []
    seen = set()
    for m in whitelist_models:
        target_slug = resolve_model_slug(m)
        if target_slug and target_slug not in seen:
            seen.add(target_slug)
            model_ids.append(target_slug)

    success = update_guardrail_policy(GUARDRAIL_ID, model_ids)

    if success:
        print("全部流程執行完畢！所有關聯金鑰已套用最新模型限制。")


if __name__ == "__main__":
    main()
