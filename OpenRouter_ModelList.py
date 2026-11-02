import os
import sys
from datetime import datetime
import requests
from dotenv import load_dotenv

# 1. 載入 .env 設定
load_dotenv()

MANAGEMENT_KEY = os.getenv("OPENROUTER_MANAGEMENT_KEY")
GUARDRAIL_ID = os.getenv("OPENROUTER_GUARDRAIL_ID")

if not MANAGEMENT_KEY:
    print("【錯誤】未在 .env 中讀取到 OPENROUTER_MANAGEMENT_KEY，請確認設定。")
    sys.exit(1)

if not GUARDRAIL_ID:
    print("【錯誤】未在 .env 中讀取到 OPENROUTER_GUARDRAIL_ID，請確認設定。")
    sys.exit(1)

try:
    max_input_m = float(os.getenv("MAX_INPUT_PRICE_PER_M", "0.5"))
    max_output_m = float(os.getenv("MAX_OUTPUT_PRICE_PER_M", "1.0"))
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


def fetch_and_filter_models() -> list[dict]:
    """抓取所有模型、過濾費率條件（含免費模型），並依照名稱排序"""
    url = "https://openrouter.ai/api/v1/models"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
    except requests.RequestException as e:
        print(f"【錯誤】連線失敗: {e}")
        sys.exit(1)

    models_data = res.json().get("data", [])
    qualified = []

    for model in models_data:
        pricing = model.get("pricing", {})
        try:
            prompt_price = float(pricing.get("prompt", 0))
            completion_price = float(pricing.get("completion", 0))
        except (ValueError, TypeError):
            continue

        # 包含費率 0 的免費模型以及低於門檻者
        if prompt_price <= MAX_INPUT_PRICE_PER_TOKEN and completion_price <= MAX_OUTPUT_PRICE_PER_TOKEN:
            # 換算回每百萬 Token 美元費用
            input_per_m = prompt_price * 1_000_000
            output_per_m = completion_price * 1_000_000

            qualified.append({
                "id": model.get("id"),
                "name": model.get("name") or model.get("id"),
                "context_length": model.get("context_length", 0),
                "input_per_m": input_per_m,
                "output_per_m": output_per_m,
                "is_free": (input_per_m == 0 and output_per_m == 0),
            })

    # 依照模型名稱（不區分大小寫）排序
    qualified.sort(key=lambda x: x["name"].lower())
    return qualified


def export_to_markdown(models: list[dict], filename: str = "models_whitelist.md"):
    """將模型清單建表並匯出為 Markdown 檔案"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"# OpenRouter 允許模型清單 (Guardrail 白名單)",
        f"",
        f"- **匯出時間**：{now_str}",
        f"- **門檻條件**：Input ≤ ${max_input_m}/M , Output ≤ ${max_output_m}/M（含完全免費模型）",
        f"- **符合模型總數**：{len(models)} 款",
        f"",
        f"| 模型名稱 | 模型 ID | Context 視窗 | Input 單價 ($/M) | Output 單價 ($/M) | 類型 |",
        f"| :--- | :--- | :---: | :---: | :---: | :---: |",
    ]

    for m in models:
        context_k = f"{m['context_length'] // 1000}k" if m['context_length'] >= 1000 else str(m['context_length'])
        tag = "免費 (Free)" if m["is_free"] else "平價"
        lines.append(
            f"| {m['name']} | `{m['id']}` | {context_k} | ${m['input_per_m']:.4f} | ${m['output_per_m']:.4f} | {tag} |"
        )

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Markdown 檔案已成功生成：{os.path.abspath(filename)}")


def update_guardrail(guardrail_id: str, model_ids: list[str]) -> bool:
    """呼叫 API 更新 Guardrail 白名單"""
    url = f"https://openrouter.ai/api/v1/guardrails/{guardrail_id}"
    payload = {"allowed_models": model_ids}

    try:
        response = requests.patch(url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code == 200:
            print(f"Guardrail ({guardrail_id}) 白名單更新成功！")
            return True
        else:
            print(f"【更新失敗】HTTP {response.status_code}: {response.text}")
            return False
    except requests.RequestException as e:
        print(f"【錯誤】更新異常: {e}")
        return False


def main():
    print("1. 正在查詢 OpenRouter 模型庫並執行費率篩選...")
    models = fetch_and_filter_models()
    print(f"篩選完成！共找到 {len(models)} 個模型符合條件（含免費模型）。")

    if not models:
        print("未找到符合條件的模型，終止後續作業。")
        return

    print("2. 正在生成 Markdown 說明文件...")
    export_to_markdown(models)

    print(f"3. 正在同步更新 Guardrail: {GUARDRAIL_ID} ...")
    model_ids = [m["id"] for m in models]
    success = update_guardrail(GUARDRAIL_ID, model_ids)

    if success:
        print("全部流程執行完畢！所有關聯金鑰已套用最新模型限制。")


if __name__ == "__main__":
    main()