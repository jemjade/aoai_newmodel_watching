import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright


STATE_DIR = Path("state")
SNAPSHOT_FILE = STATE_DIR / "catalog_snapshot.json"
CATALOG_URL = "https://ai.azure.com/catalog"

# 모델명 후보로 인정할 브랜드/계열 키워드
MODEL_HINT_PATTERNS = [
    r"\bgpt\b",
    r"\bphi\b",
    r"\bllama\b",
    r"\bmistral\b",
    r"\bdeepseek\b",
    r"\bqwen\b",
    r"\bgemma\b",
    r"\bclaude\b",
    r"\bcommand\b",
    r"\bwhisper\b",
    r"\bembedding\b",
    r"\bo[134]\b",        # o1, o3, o4 같은 패턴
    r"\bmini\b",
    r"\bnano\b",
    r"\bvision\b",
    r"\binstruct\b",
    r"\breason(?:ing)?\b",
]

# 페이지 잡음 제거용
NOISE_PATTERNS = [
    r"^search$",
    r"^filter$",
    r"^sort$",
    r"^compare$",
    r"^catalog$",
    r"^models?$",
    r"^deploy$",
    r"^learn more$",
    r"^show more$",
    r"^documentation$",
    r"^privacy$",
    r"^terms$",
    r"^sign in$",
    r"^pricing$",
    r"^overview$",
    r"^azure ai foundry$",
    r"^azure ai foundry models$",
]

# 너무 일반적인 단어는 제외
GENERIC_BAD_WORDS = {
    "azure", "foundry", "model", "models", "catalog", "documentation",
    "learn", "more", "privacy", "terms", "overview", "search", "filter",
    "sort", "deploy", "compare",
}


def get_env(name: str, required: bool = True, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_noise(text: str) -> bool:
    lowered = text.lower().strip()

    for pattern in NOISE_PATTERNS:
        if re.fullmatch(pattern, lowered):
            return True

    # 단어가 너무 적고 의미가 너무 일반적이면 버림
    tokens = re.findall(r"[a-z0-9]+", lowered)
    if tokens and all(token in GENERIC_BAD_WORDS for token in tokens):
        return True

    return False


def looks_like_model_name(text: str) -> bool:
    if not text:
        return False

    text = normalize_text(text)
    lowered = text.lower()

    # 길이 제한
    if len(text) < 3 or len(text) > 100:
        return False

    # 잡음 제거
    if is_noise(text):
        return False

    # 너무 긴 문장형 설명 제거
    if len(text.split()) > 10:
        return False

    # URL, 이메일, 특수한 안내문 제거
    if "http://" in lowered or "https://" in lowered or "@" in lowered:
        return False

    # 모델 힌트 키워드가 있으면 통과
    if any(re.search(pattern, lowered) for pattern in MODEL_HINT_PATTERNS):
        return True

    # 예: "Llama 4 Scout", "Phi-4-mini", "GPT-4.1"
    if re.fullmatch(r"[A-Za-z0-9 .:+\-_()/]+", text):
        has_alpha = re.search(r"[A-Za-z]", text) is not None
        has_digit = re.search(r"\d", text) is not None
        has_modelish_sep = any(ch in text for ch in ["-", ".", "(", ")", "/"])

        # 숫자가 있거나 모델스러운 구분자가 있으면 후보로 허용
        if has_alpha and (has_digit or has_modelish_sep):
            return True

    return False


def post_filter(models: list[str]) -> list[str]:
    """
    2차 필터:
    - 완전히 같은 이름 중복 제거
    - 대소문자만 다른 중복 제거
    - 너무 일반적인 항목 제거
    """
    result = []
    seen_lower = set()

    for item in models:
        lowered = item.lower()

        if lowered in seen_lower:
            continue

        if is_noise(item):
            continue

        # 'OpenAI' 같은 공급자 이름 단독은 제외
        if lowered in {"openai", "meta", "mistral", "deepseek", "qwen", "anthropic"}:
            continue

        seen_lower.add(lowered)
        result.append(item)

    result.sort()
    return result


def scrape_catalog_models() -> list[str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(CATALOG_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(8000)

        # 페이지 전체 텍스트 후보 수집
        texts = page.locator("h1, h2, h3, h4, a, button, div, span").all_inner_texts()

        browser.close()

    candidates = []
    seen = set()

    for raw in texts:
        for piece in raw.split("\n"):
            text = normalize_text(piece)
            if not text:
                continue

            if looks_like_model_name(text) and text not in seen:
                seen.add(text)
                candidates.append(text)

    return post_filter(candidates)


def load_previous_snapshot() -> list[str]:
    if not SNAPSHOT_FILE.exists():
        return []
    with SNAPSHOT_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(models: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SNAPSHOT_FILE.open("w", encoding="utf-8") as f:
        json.dump(models, f, ensure_ascii=False, indent=2)


def diff_new_models(previous: list[str], current: list[str]) -> list[str]:
    previous_set = set(previous)
    return [m for m in current if m not in previous_set]


def build_email_body(total_count: int, new_models: list[str]) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    if not new_models:
        return (
            "[Public Foundry Catalog Check]\n\n"
            f"Checked at: {now}\n"
            f"Catalog URL: {CATALOG_URL}\n"
            f"Detected model-like entries: {total_count}\n\n"
            "No new visible model entries were found today."
        )

    lines = [
        "[Public Foundry Catalog Check]",
        "",
        f"Checked at: {now}",
        f"Catalog URL: {CATALOG_URL}",
        f"Detected model-like entries: {total_count}",
        f"New visible entries: {len(new_models)}",
        "",
        "New entries:",
    ]

    for idx, name in enumerate(new_models, start=1):
        lines.append(f"{idx}. {name}")

    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    smtp_host = get_env("EMAIL_SMTP_HOST")
    smtp_port = int(get_env("EMAIL_SMTP_PORT"))
    username = get_env("EMAIL_USERNAME")
    password = get_env("EMAIL_PASSWORD")
    email_from = get_env("EMAIL_FROM")
    email_to = get_env("EMAIL_TO")

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    recipients = [x.strip() for x in email_to.split(",") if x.strip()]

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(email_from, recipients, msg.as_string())


def main() -> None:
    current_models = scrape_catalog_models()
    previous_models = load_previous_snapshot()

    first_run = len(previous_models) == 0
    new_models = diff_new_models(previous_models, current_models)

    save_snapshot(current_models)

    if first_run:
        print("First run detected. Snapshot initialized, no email sent.")
        print(f"Current detected entries: {len(current_models)}")
        print(current_models[:30])
        return

    subject = f"[Public Foundry Catalog] {len(new_models)} new visible entr{'y' if len(new_models) == 1 else 'ies'}"
    body = build_email_body(len(current_models), new_models)
    send_email(subject, body)

    print(f"Current detected entries: {len(current_models)}")
    print(f"New visible entries: {len(new_models)}")
    print(new_models)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)