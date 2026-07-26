#!/usr/bin/env python3
"""Transcript study pipeline — 영어 청크/구동사 스크립트 → 구조화된 학습 포스트.

input/script.md 코드블록에 붙여넣은 스크립트(청크·구동사·phrase 목록) 전체를 하나의
항목으로 읽어, Claude로 분석해 다음 섹션으로 구성된 학습 포스트를 생성한다:
  - Phrasal Verbs & Chunks (청크마다 예문 1개 + 한글 번역 + 활용 대화 one-turn)
  - Check Yourself (토글 아코디언 퀴즈, 선택지는 이번 세션 청크에서만 출제)
  - Reference (스크립트에 적힌 참고 링크를 맨 하단에 그대로 노출)

코드블록 안에서 `---` 만 있는 줄로 구분하면 스크립트 여러 개를 각각 별도 포스트로
처리한다. 이미 게시에 사용된 스크립트(텍스트 해시 기준)는 다시 나타나도 건너뛴다.
이 사이트는 온디맨드 후킹 전용이다 — 크론 폴백이 없으므로(FALLBACK_QUOTES = [])
입력이 비어 있으면 그날은 아무것도 게시하지 않고 건너뛴다.

Usage:
    python pipeline/generate.py [--dry-run]

Env:
    JUDGE_BACKEND            "claude-code" | "api" (기본: 자동 — claude CLI가 있으면
                             claude-code, 없으면 api)
    CLAUDE_CODE_OAUTH_TOKEN  claude-code 백엔드 CI 인증 (claude setup-token으로 발급,
                             로컬은 claude 로그인 세션 사용)
    ANTHROPIC_API_KEY        api 백엔드 필수
    CLAUDE_MODEL             생성 모델 (기본 claude-sonnet-4-6)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENTENCE_FILE = ROOT / "input" / "script.md"
STATE_FILE = ROOT / "pipeline" / "state.json"
CONTENT_DIR = ROOT / "content" / "posts"

KST = timezone(timedelta(hours=9))

# ============================== 도메인 설정 =================================
# 이 블록만 새 프로젝트 주제에 맞게 교체한다. 아래 엔진 코드는 건드릴 필요 없다.

# 온디맨드 후킹 전용 사이트 — 입력이 없는 날 대신 쓸 폴백 풀을 두지 않는다.
# (비워두면 main()이 "오늘은 건너뜁니다" 로그와 함께 exit 0으로 끝난다.)
FALLBACK_QUOTES = []

# Claude에게 부여할 역할/톤
SYSTEM_PROMPT = """You are a bilingual (English-Korean) ESL coach who helps Korean \
learners study English phrasal verbs and chunks. You are given a raw transcript/list \
(often speech-to-text output or a curated list, possibly with timestamps or Korean \
glosses already attached) that names one chunk or phrasal verb per line. For every \
distinct chunk you produce one natural example sentence, its Korean translation, and \
a short one-turn dialogue (a single A/B exchange) that shows a realistic situation for \
using it. Titles, summaries, meanings, and explanations are written in natural Korean; \
example sentences and dialogue lines are written in natural English. Never invent a \
chunk that is not present in the transcript, and never drop a chunk that is."""

# {sentence} / {note} 두 자리를 반드시 유지. JSON 스키마의 이중 중괄호는 str.format()
# 이스케이프이므로 스키마를 고칠 때도 그대로 유지한다.
GENERATE_PROMPT = """Below is a transcript/list of English phrasal verbs or chunks \
(one per line, often with a timestamp and/or a Korean gloss already given).{note} \
Analyze it and produce study notes. Respond ONLY with JSON in exactly this format, \
no other text:

{{"title": "세션 주제를 담은 간단한 제목 (한글)",
 "summary": "이번 세션에서 다룬 청크/구동사에 대한 2-3문장 한글 개요",
 "chunks": [
   {{"phrase": "스크립트에 있는 원문 그대로의 구동사/청크 (영어, 원형)",
     "meaning_ko": "한글 의미 — 스크립트에 이미 주어져 있으면 그대로 재사용",
     "example_en": "그 청크를 자연스럽게 포함하는 영어 예문 1개",
     "example_ko": "위 예문의 한글 번역",
     "dialogue": [
       {{"speaker": "A", "line": "그 청크를 쓰기 좋은 상황을 보여주는 영어 대화 첫 마디"}},
       {{"speaker": "B", "line": "자연스럽게 이어지는 영어 응답"}}
     ]}}
 ],
 "quiz": [
   {{"question": "빈칸(____)이 있는 자연스러운 영어 문장",
     "options": ["chunk A", "chunk B", "chunk C"],
     "answer": "chunk B",
     "explanation": "정답인 이유와 오답이 안 되는 이유를 짧은 한글 한 줄로"}}
 ],
 "tags": ["kebab-case-tag", "최대 3개"]}}

Requirements: extract EVERY distinct chunk/phrasal verb listed in the transcript. If \
the same phrase appears more than once (e.g. with two different timestamps), keep only \
ONE entry for it. Produce exactly one example_en + example_ko + one 2-line dialogue per \
chunk — never more than one example per chunk. Make sure example_en actually contains \
the phrase (its base or a naturally inflected form). Keep example_en, example_ko, and \
each dialogue line short (roughly 8-15 words) so the response stays manageable even when \
the transcript lists dozens of chunks. If the transcript already gives a Korean meaning \
next to a phrase, reuse it verbatim for meaning_ko instead of inventing a new one.
Quiz rules (5-8 questions): every question is FILL-IN-THE-BLANK — a natural sentence \
with "____" marking the blank. Every option (3-4 per question) MUST be taken verbatim \
from the "phrase" field of the chunks above — never invent outside words, so all \
distractors are plausible items the learner just studied. Exactly one option fits the \
blank; the sentence must be fully grammatical when the correct option is inserted \
(adjust the option's form — tense, particle, agreement — inside the option text if \
needed). The same option set should not repeat across questions. Do not copy a chunk's \
example_en as a quiz sentence — write a fresh sentence.

Transcript:
{sentence}"""

QUOTE_NOTE = ""

# 포스트 본문 섹션 제목
HEADING_INPUT = "Session Overview"
HEADING_INPUT_QUOTE = "Today's Idiom"
HEADING_CHUNKS = "📦 Phrasal Verbs & Chunks"
HEADING_QUIZ = "✅ Check Yourself"
HEADING_REFERENCE = "📎 Reference"

# ============================ 도메인 설정 끝 =================================


def log(msg: str) -> None:
    print(msg, flush=True)


def sentence_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return (slug or "study")[:60].rstrip("-")


def read_sentences() -> list[str]:
    """input/script.md 코드블록 안의 스크립트를 읽는다.

    코드블록 전체가 항목 하나. `---` 만 있는 줄로 구분하면 여러 스크립트를
    각각 별도 항목(= 별도 포스트)으로 처리한다.
    """
    if not SENTENCE_FILE.exists():
        log(f"오류: {SENTENCE_FILE} 파일이 없습니다")
        sys.exit(1)
    text = SENTENCE_FILE.read_text(encoding="utf-8")
    fenced = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    body = fenced.group(1) if fenced else text
    scripts = []
    for chunk in re.split(r"^\s*---+\s*$", body, flags=re.MULTILINE):
        chunk = chunk.strip()
        if chunk and not chunk.startswith("<!--"):
            scripts.append(chunk)
    return scripts


def fallback_quote_item(today) -> dict | None:
    """input이 비어 있을 때 사용할 항목 — 날짜 기준으로 풀을 순환 선택."""
    if not FALLBACK_QUOTES:
        return None
    idx = today.timetuple().tm_yday % len(FALLBACK_QUOTES)
    quote = FALLBACK_QUOTES[idx]
    return {
        "text": quote["text"],
        "source": quote.get("author") or "idiom",
        # 날짜를 해시에 포함 — 같은 항목이 몇 주 뒤 다시 나와도 새로 게시되도록
        "dedup_key": sentence_hash(f"{today.isoformat()}::{quote['text']}"),
    }


def build_queue(sentences: list[str], today) -> list[dict]:
    if sentences:
        return [
            {"text": s, "source": None, "dedup_key": sentence_hash(s)}
            for s in sentences
        ]
    fallback = fallback_quote_item(today)
    return [fallback] if fallback else []


class FatalAPIError(Exception):
    """재시도가 무의미한 오류(크레딧 부족, 인증 실패) — 실행 전체 중단."""


def is_fatal_api_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in (
        "credit balance", "authenticat", "invalid x-api-key",
        "invalid api key", "invalid bearer token", "oauth token", "/login",
        "401",
    ))


def parse_result(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    required = ("title", "summary")
    if not all(isinstance(data.get(k), str) and data.get(k) for k in required):
        return None
    for key in ("chunks", "quiz"):
        value = data.get(key) or []
        data[key] = value if isinstance(value, list) else []

    seen_phrases: set[str] = set()
    chunks = []
    for item in data["chunks"]:
        if not isinstance(item, dict):
            continue
        phrase = str(item.get("phrase", "")).strip()
        example_en = str(item.get("example_en", "")).strip()
        example_ko = str(item.get("example_ko", "")).strip()
        if not (phrase and example_en and example_ko):
            continue
        key = phrase.lower()
        if key in seen_phrases:  # 스크립트에 같은 청크가 중복 등장한 경우 방어적으로 한 번만
            continue
        seen_phrases.add(key)
        dialogue = []
        for turn in (item.get("dialogue") or [])[:2]:
            if isinstance(turn, dict) and str(turn.get("line", "")).strip():
                dialogue.append({
                    "speaker": str(turn.get("speaker", "")).strip(),
                    "line": str(turn.get("line", "")).strip(),
                })
        chunks.append({
            "phrase": phrase,
            "meaning_ko": str(item.get("meaning_ko", "")).strip(),
            "example_en": example_en,
            "example_ko": example_ko,
            "dialogue": dialogue,
        })
    data["chunks"] = chunks
    if not data["chunks"]:
        return None

    quiz = []
    for item in data["quiz"]:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        answer = str(item.get("answer", "")).strip()
        options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
        if not (question and answer and options):
            continue
        quiz.append({
            "question": question,
            "options": options,
            "answer": answer,
            "explanation": str(item.get("explanation", "")).strip(),
        })
    data["quiz"] = quiz

    tags = data.get("tags") or []
    data["tags"] = [slugify(str(t)) for t in tags[:3] if str(t).strip()] or ["study-notes"]
    return data


def build_prompt(sentence: str, source: str | None) -> str:
    note = QUOTE_NOTE if source else ""
    return GENERATE_PROMPT.format(sentence=sentence, note=note)


def generate_api(client, model: str, sentence: str, source: str | None = None) -> dict | None:
    prompt = build_prompt(sentence, source)
    for attempt in (1, 2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            if is_fatal_api_error(exc):
                raise FatalAPIError(str(exc)) from exc
            log(f"  API 오류 (시도 {attempt}): {exc}")
            if attempt == 2:
                return None
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        result = parse_result(text)
        if result:
            return result
        log(f"  JSON 파싱 실패 (시도 {attempt}): {text[:120]!r}")
    return None


def generate_cli(model: str, sentence: str, source: str | None = None) -> dict | None:
    prompt = build_prompt(sentence, source)
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    cmd = ["claude", "-p", "--model", model, "--tools", "",
           "--output-format", "text", "--append-system-prompt", SYSTEM_PROMPT]
    for attempt in (1, 2):
        try:
            result = subprocess.run(cmd, input=prompt, env=env, timeout=600,
                                     capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            log(f"  CLI 타임아웃 (시도 {attempt})")
            continue
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            if is_fatal_api_error(RuntimeError(err)):
                raise FatalAPIError(err[:300])
            log(f"  CLI 오류 (시도 {attempt}): {err[:200]}")
            if attempt == 2:
                return None
            continue
        parsed = parse_result(result.stdout)
        if parsed:
            return parsed
        log(f"  JSON 파싱 실패 (시도 {attempt}): {result.stdout[:120]!r}")
    return None


def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


REFERENCE_LINK_RE = re.compile(
    r"(?:참고\s*(?:영상|자료|링크|사이트)|출처|reference|source)\s*[:：]\s*(\S+)",
    re.IGNORECASE,
)


def extract_reference_links(text: str) -> list[str]:
    """스크립트 하단에 '참고 영상/자료/출처' 등으로 라벨된 링크만 뽑는다.

    각 청크 옆의 타임스탬프 링크(`[[00:00](...)]`)는 대상이 아니다 — 라벨이 붙은
    줄만 참고 링크로 취급해, 맨 하단 Reference 섹션에 노출한다.
    """
    links: list[str] = []
    for m in REFERENCE_LINK_RE.finditer(text):
        url = m.group(1).rstrip(".,)]>")
        if url not in links:
            links.append(url)
    return links


def write_post(sentence: str, result: dict, date: datetime, source: str | None = None) -> Path:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{date.date().isoformat()}-{slugify(result['title'])}"
    path = CONTENT_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = CONTENT_DIR / f"{base}-{n}.md"
        n += 1

    tags = list(result["tags"])
    if source:
        tags = (tags + ["idiom-of-the-day"])[:4]
    tags_str = ", ".join(yaml_quote(t) for t in tags)

    sections = []

    if source:
        sections.append(f"## {HEADING_INPUT_QUOTE}\n\n> **{sentence}**\n\n{result['summary']}\n")
    else:
        sections.append(f"## {HEADING_INPUT}\n\n{result['summary']}\n")

    if result["chunks"]:
        lines = [f"## {HEADING_CHUNKS}\n"]
        for item in result["chunks"]:
            phrase = item.get("phrase", "")
            meaning_ko = item.get("meaning_ko", "")
            heading = f"### {phrase}" + (f" — {meaning_ko}" if meaning_ko else "")
            lines.append(heading + "\n")
            example_en = item.get("example_en", "")
            example_ko = item.get("example_ko", "")
            if example_en:
                lines.append(f"- *{example_en}*")
            if example_ko:
                lines.append(f"  - {example_ko}")
            dialogue = item.get("dialogue") or []
            if dialogue:
                # 줄 사이에 ">" 만 있는 빈 줄을 둬서 한 blockquote 안에서도 A/B가
                # 별도 문단으로 줄바꿈되게 한다 (안 그러면 goldmark가 한 줄로 합침).
                dlines = "\n>\n".join(
                    f"> **{d.get('speaker', '')}:** {d.get('line', '')}" for d in dialogue
                )
                lines.append(f"\n{dlines}\n")
            else:
                lines.append("")
        sections.append("\n".join(lines))

    if result["quiz"]:
        lines = [f"## {HEADING_QUIZ}\n"]
        for i, item in enumerate(result["quiz"], 1):
            lines.append(f"**Q{i}.** {item.get('question', '')}\n")
            options = item.get("options") or []
            if options:
                lines.append("\n".join(f"- {opt}" for opt in options) + "\n")
            answer = html_escape(str(item.get("answer", "")))
            explanation = html_escape(str(item.get("explanation", "")))
            detail = f"<strong>{answer}</strong>"
            if explanation:
                detail += f" — {explanation}"
            lines.append(
                "<details><summary>Show answer</summary>"
                f"<p>{detail}</p></details>\n"
            )
        sections.append("\n".join(lines))

    refs = extract_reference_links(sentence)
    if refs:
        lines = [f"## {HEADING_REFERENCE}\n"]
        lines.extend(f"- {url}" for url in refs)
        sections.append("\n".join(lines) + "\n")

    post = f"""---
title: {yaml_quote(f"{date.date().isoformat()} {result['title']}")}
date: {date.isoformat()}
tags: [{tags_str}]
---
""" + "\n".join(sections)
    path.write_text(post, encoding="utf-8")
    return path


def clear_input() -> None:
    """게시가 끝난 뒤 input/script.md 코드블록을 비운다 (안내 주석은 유지)."""
    text = SENTENCE_FILE.read_text(encoding="utf-8")
    cleared = re.sub(r"```[a-zA-Z]*\n.*?```", "```\n```", text, count=1, flags=re.DOTALL)
    if cleared != text:
        SENTENCE_FILE.write_text(cleared, encoding="utf-8")
        log("input/script.md 코드블록을 비웠습니다 (게시 완료)")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcript study pipeline")
    parser.add_argument("--dry-run", action="store_true",
                         help="파일 생성/state.json 갱신 없이 결과만 출력")
    args = parser.parse_args()

    backend = os.environ.get("JUDGE_BACKEND", "").strip() or (
        "claude-code" if shutil.which("claude") else "api"
    )
    client = None
    if backend == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log("오류: api 백엔드에는 ANTHROPIC_API_KEY 환경변수가 필요합니다")
            return 1
        import anthropic  # 지연 임포트

        client = anthropic.Anthropic()
    elif backend == "claude-code":
        if not shutil.which("claude"):
            log("오류: claude-code 백엔드에는 claude CLI가 PATH에 있어야 합니다")
            return 1
    else:
        log(f"오류: 알 수 없는 JUDGE_BACKEND={backend!r} (claude-code | api)")
        return 1

    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    today = datetime.now(KST).date()
    sentences = read_sentences()
    queue = build_queue(sentences, today)
    if sentences:
        log(f"입력된 스크립트 {len(sentences)}개")
    elif queue:
        log(f"input/script.md 에 스크립트가 없어 이디엄으로 대체합니다: {queue[0]['text']}")
    else:
        log("input/script.md 에 스크립트가 없고 FALLBACK_QUOTES도 비어 있어 오늘은 건너뜁니다")
        return 0

    state = load_state()
    processed: dict = state.get("processed", {})

    log(f"=== 생성 시작 (backend={backend}, model={model}, dry_run={args.dry_run}) ===")

    new_count = 0
    skipped_dup = 0
    failed = 0
    fatal_error = None
    for item in queue:
        sentence, source, h = item["text"], item["source"], item["dedup_key"]
        if h in processed:
            skipped_dup += 1
            continue

        preview = sentence if len(sentence) <= 80 else sentence[:80] + "…"
        log(f"\n오늘의 항목 ({len(sentence)}자): {preview}")
        try:
            if backend == "claude-code":
                result = generate_cli(model, sentence, source)
            else:
                result = generate_api(client, model, sentence, source)
        except FatalAPIError as exc:
            fatal_error = exc
            break

        if result is None:
            log("  생성 실패 — 건너뜁니다 (다음 실행에서 재시도)")
            failed += 1
            continue

        now = datetime.now(KST)
        log(f"  → {result['title']}")

        if args.dry_run:
            log(json.dumps(result, ensure_ascii=False, indent=2))
            continue

        path = write_post(sentence, result, now, source)
        log(f"  생성 파일: {path.relative_to(ROOT)}")
        processed[h] = now.date().isoformat()
        new_count += 1

    log(f"\n=== 결과: 신규 {new_count} / 중복 스킵 {skipped_dup} / 생성 실패 {failed} ===")

    if args.dry_run:
        log("(dry-run — 파일 생성/기록 갱신 없음)")
        return 1 if fatal_error else 0

    if new_count:
        state["processed"] = processed
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    # 전부 성공했을 때만 입력란 초기화 — 실패분이 있으면 다음 실행 재시도를 위해 남겨둔다
    if sentences and new_count and not failed and fatal_error is None:
        clear_input()

    if fatal_error:
        log(f"\n중단: 복구 불가능한 API 오류 — {fatal_error}")
        log("→ Anthropic 크레딧/API 키(또는 CLAUDE_CODE_OAUTH_TOKEN)를 확인하세요.")
        log("→ 성공한 항목은 이미 게시/기록되었습니다.")
        return 1
    return 1 if failed and not new_count else 0


if __name__ == "__main__":
    sys.exit(main())
