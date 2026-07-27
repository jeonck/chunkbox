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

스크립트가 긴 목록(예: 구동사 수십~수백 개)이면 한 번의 Claude 호출로 처리하기엔
출력이 너무 커져 타임아웃/누락이 생기기 쉽다. 그래서 청크를 CHUNK_BATCH_SIZE개씩
나눠 여러 번 호출해 chunks만 뽑고(각 배치는 독립적인 작은 호출), 마지막에 전체
청크 목록을 모아 title/summary/quiz를 만드는 한 번의 마무리 호출을 추가로 실행한다.
청크가 적은 스크립트도 같은 경로(배치 1개 + 마무리 호출)를 타므로 코드 경로가
하나뿐이다.

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
import time
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

# 한 번의 Claude 호출에 넘기는 청크(줄) 개수. 스크립트의 불릿 줄이 이보다 많으면
# 여러 배치로 나눠 각각 호출한다 — 한 호출에 너무 많은 청크를 요구하면 응답이
# 길어져 타임아웃/누락 위험이 커지기 때문.
CHUNK_BATCH_SIZE = 20

# 배치 호출 사이에 두는 간격(초). CLI/네트워크에 연속 요청을 몰아치지 않도록 쉬어간다.
BATCH_INTERVAL_SECONDS = 3

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

# {sentence} 자리를 반드시 유지. JSON 스키마의 이중 중괄호는 str.format() 이스케이프
# 이므로 스키마를 고칠 때도 그대로 유지한다. (배치 호출 — chunks만 생성)
CHUNK_BATCH_PROMPT = """Below is an excerpt from a transcript/list of English phrasal \
verbs or chunks (one per line, often with a timestamp and/or a Korean gloss already \
given). This may be only part of a longer script that is being processed in batches — \
do not worry about an overall session title, summary, or quiz; just extract the chunks \
from THIS excerpt. Respond ONLY with JSON in exactly this format, no other text:

{{"chunks": [
   {{"phrase": "스크립트에 있는 원문 그대로의 구동사/청크 (영어, 원형)",
     "meaning_ko": "한글 의미 — 스크립트에 이미 주어져 있으면 그대로 재사용",
     "example_en": "그 청크를 자연스럽게 포함하는 영어 예문 1개",
     "example_ko": "위 예문의 한글 번역",
     "dialogue": [
       {{"speaker": "A", "line": "그 청크를 쓰기 좋은 상황을 보여주는 영어 대화 첫 마디"}},
       {{"speaker": "B", "line": "자연스럽게 이어지는 영어 응답"}}
     ]}}
 ]}}

Requirements: extract EVERY distinct chunk/phrasal verb listed in this excerpt. If the \
same phrase appears more than once (e.g. with two different timestamps), keep only ONE \
entry for it. Produce exactly one example_en + example_ko + one 2-line dialogue per \
chunk — never more than one example per chunk. Make sure example_en actually contains \
the phrase (its base or a naturally inflected form). Keep example_en, example_ko, and \
each dialogue line short (roughly 8-15 words). If the excerpt already gives a Korean \
meaning next to a phrase, reuse it verbatim for meaning_ko instead of inventing a new one.

Excerpt:
{sentence}"""

# 마무리 호출 — 전체 청크 목록(phrase — meaning_ko)을 넘겨 title/summary/quiz/tags만
# 생성한다. 원문 트랜스크립트가 아니라 이미 뽑아낸 청크 목록만 보므로 입력이 짧고
# 빠르다.
FINAL_PROMPT = """Below is the full list of chunks/phrasal verbs covered in one English \
study session (English phrase — Korean meaning, one per line). Write a session title, \
summary, and quiz for the WHOLE session. Respond ONLY with JSON in exactly this format, \
no other text:

{{"title": "세션 주제를 담은 간단한 제목 (한글)",
 "summary": "이번 세션에서 다룬 청크/구동사에 대한 2-3문장 한글 개요",
 "quiz": [
   {{"question": "빈칸(____)이 있는 자연스러운 영어 문장",
     "options": ["chunk A", "chunk B", "chunk C"],
     "answer": "chunk B",
     "explanation": "정답인 이유와 오답이 안 되는 이유를 짧은 한글 한 줄로"}}
 ],
 "tags": ["kebab-case-tag", "최대 3개"]}}

Quiz rules (5-8 questions): every question is FILL-IN-THE-BLANK — a natural sentence \
with "____" marking the blank. Every option (3-4 per question) MUST be taken verbatim \
from the phrase list below — never invent outside words, so all distractors are \
plausible items the learner just studied. Exactly one option fits the blank; the \
sentence must be fully grammatical when the correct option is inserted (adjust the \
option's form — tense, particle, agreement — inside the option text if needed). The \
same option set should not repeat across questions.

Chunk list:
{sentence}"""

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


def split_into_batches(text: str, batch_size: int = CHUNK_BATCH_SIZE) -> list[str]:
    """긴 스크립트를 청크 배치 여러 개로 나눈다.

    줄 단위로 나눈다 — 불릿 마커("*"/"-")가 붙어 있든 없든(붙여넣기 과정에서
    사라지는 경우가 있어 마커에 의존하지 않는다) 상관없이, 빈 줄과 참고 링크로
    라벨된 줄(REFERENCE_LINK_RE, 예: "참고 영상: ...")을 제외한 나머지 줄을
    batch_size개씩 묶는다. 줄 수가 batch_size 이하면 원문 전체를 배치 1개로 그대로
    반환한다 — 짧은 스크립트도 항상 같은 처리 경로(배치 N개 + 마무리 호출 1개)를
    타게 된다.
    """
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not REFERENCE_LINK_RE.search(ln)]
    if len(lines) <= batch_size:
        return [text]

    batches = []
    for i in range(0, len(lines), batch_size):
        batches.append("\n".join(lines[i:i + batch_size]))
    return batches


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


def normalize_chunk_item(item) -> dict | None:
    """chunks 배열의 원소 하나를 검증/정규화한다. 필수 필드가 없으면 None."""
    if not isinstance(item, dict):
        return None
    phrase = str(item.get("phrase", "")).strip()
    example_en = str(item.get("example_en", "")).strip()
    example_ko = str(item.get("example_ko", "")).strip()
    if not (phrase and example_en and example_ko):
        return None
    dialogue = []
    for turn in (item.get("dialogue") or [])[:2]:
        if isinstance(turn, dict) and str(turn.get("line", "")).strip():
            dialogue.append({
                "speaker": str(turn.get("speaker", "")).strip(),
                "line": str(turn.get("line", "")).strip(),
            })
    return {
        "phrase": phrase,
        "meaning_ko": str(item.get("meaning_ko", "")).strip(),
        "example_en": example_en,
        "example_ko": example_ko,
        "dialogue": dialogue,
    }


def parse_chunks_response(text: str) -> list[dict] | None:
    """배치 호출 응답 — {"chunks": [...]} 를 파싱해 정규화된 리스트로 반환."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    raw = data.get("chunks")
    if not isinstance(raw, list):
        return None
    chunks = [c for item in raw if (c := normalize_chunk_item(item))]
    return chunks or None


def parse_final_response(text: str) -> dict | None:
    """마무리 호출 응답 — {"title","summary","quiz","tags"} 를 파싱/정규화."""
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

    quiz = []
    for item in (data.get("quiz") or []):
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


def call_backend_once(backend: str, client, model: str, prompt: str, timeout: int) -> str | None:
    """백엔드(claude-code CLI | api)에 프롬프트 하나를 보내 원문 응답을 받는다.

    복구 불가능한 오류는 FatalAPIError로 올리고, 일시적 오류/타임아웃은 None을
    반환해 호출부가 재시도하게 한다.
    """
    if backend == "claude-code":
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        cmd = ["claude", "-p", "--model", model, "--tools", "",
               "--output-format", "text", "--append-system-prompt", SYSTEM_PROMPT]
        try:
            result = subprocess.run(cmd, input=prompt, env=env, timeout=timeout,
                                     capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            log(f"  CLI 타임아웃 ({timeout}s)")
            return None
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            if is_fatal_api_error(RuntimeError(err)):
                raise FatalAPIError(err[:300])
            log(f"  CLI 오류: {err[:200]}")
            return None
        return result.stdout

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        if is_fatal_api_error(exc):
            raise FatalAPIError(str(exc)) from exc
        log(f"  API 오류: {exc}")
        return None
    return next((b.text for b in response.content if b.type == "text"), "")


def call_and_parse(backend: str, client, model: str, prompt: str, parse_fn, timeout: int):
    """2회까지 재시도하며 호출 + 파싱을 함께 처리하는 공용 루프."""
    for attempt in (1, 2):
        text = call_backend_once(backend, client, model, prompt, timeout)
        if text is None:
            if attempt == 2:
                return None
            continue
        parsed = parse_fn(text)
        if parsed is not None:
            return parsed
        log(f"  JSON 파싱 실패 (시도 {attempt}): {text[:120]!r}")
    return None


def generate_chunks_for_batch(backend: str, client, model: str, batch_text: str) -> list[dict] | None:
    prompt = CHUNK_BATCH_PROMPT.format(sentence=batch_text)
    timeout = 300 if backend == "claude-code" else 0
    return call_and_parse(backend, client, model, prompt, parse_chunks_response, timeout)


def generate_final(backend: str, client, model: str, chunk_list_text: str) -> dict | None:
    prompt = FINAL_PROMPT.format(sentence=chunk_list_text)
    timeout = 180 if backend == "claude-code" else 0
    return call_and_parse(backend, client, model, prompt, parse_final_response, timeout)


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


def process_item(backend: str, client, model: str, text: str) -> dict | None:
    """스크립트 하나(= 포스트 하나)를 배치로 나눠 처리하고 결과를 병합한다.

    반환값은 write_post()가 기대하는 {"title","summary","chunks","quiz","tags"}
    형태. 배치 중 하나라도 실패하면(재시도 후에도) 전체를 실패로 본다 — 다음 실행
    에서 처음부터 다시 시도한다.
    """
    batches = split_into_batches(text)
    all_chunks: list[dict] = []
    seen_phrases: set[str] = set()

    for i, batch_text in enumerate(batches, 1):
        if i > 1 and BATCH_INTERVAL_SECONDS:
            time.sleep(BATCH_INTERVAL_SECONDS)
        log(f"  배치 {i}/{len(batches)} 처리 중 ({len(batch_text)}자)")
        chunks = generate_chunks_for_batch(backend, client, model, batch_text)
        if chunks is None:
            log(f"  배치 {i}/{len(batches)} 생성 실패")
            return None
        added = 0
        for c in chunks:
            key = c["phrase"].lower()
            if key in seen_phrases:  # 배치 경계를 넘어 같은 청크가 중복 등장한 경우
                continue
            seen_phrases.add(key)
            all_chunks.append(c)
            added += 1
        log(f"  배치 {i}/{len(batches)}: 청크 {added}개 추가 (누적 {len(all_chunks)}개)")

    if not all_chunks:
        return None

    chunk_list_text = "\n".join(
        f"{c['phrase']} — {c['meaning_ko']}" if c["meaning_ko"] else c["phrase"]
        for c in all_chunks
    )
    final = generate_final(backend, client, model, chunk_list_text)
    if final is None:
        log("  마무리 호출(title/summary/quiz) 생성 실패")
        return None

    final["chunks"] = all_chunks
    return final


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
            result = process_item(backend, client, model, sentence)
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
