"""AI 러닝메이트 — PDF를 단원별로 나눠 테스트하고, 틀린 개념은 간격 반복으로 복습"""
import base64
import hashlib
import json
import os
import re
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import anthropic
import streamlit as st
from pypdf import PdfReader, PdfWriter

# ======================= 설정값 =======================
MODEL = "claude-sonnet-5-5"
SAVE_DIR = Path("saves")       # 진도 저장 폴더
MAX_DOC_PAGES = 40             # 한 번에 AI에 보내는 최대 페이지 수
CHUNK_PAGES = 50               # 스캔본 PDF 목차 분석 시 한 번에 보내는 페이지 수
STRUCT_CHARS = 150_000         # 목차 분석에 쓰는 글자 수 상한
IMPORTANT_AT = 2               # 같은 개념을 이만큼 틀리면 ⭐ 중요
HINT_AT = 2                    # 같은 개념/단계를 이만큼 틀리면 힌트 열림
FINAL_PASS = 0.8               # 대단원 종합 테스트 통과 기준 (80%)
INTERVALS = [1, 3, 7, 14, 30]  # 복습 간격(일)
REVIEW_MAX = 10                # 한 번에 복습할 최대 개념 수
TODAY = date.fromisoformat(os.environ["LM_TODAY"]) if os.environ.get("LM_TODAY") else date.today()

st.set_page_config(page_title="AI 러닝메이트", page_icon="📚")
ss = st.session_state


def secret(name):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


# ======================= 사이드바: 설정 =======================
with st.sidebar:
    st.header("⚙️ 설정")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or secret("ANTHROPIC_API_KEY") or st.text_input(
        "Claude API 키", type="password", help="console.anthropic.com 에서 발급"
    )
    nickname = st.text_input("닉네임 (진도 저장용)", value="나").strip() or "나"
    level = st.selectbox("난이도", ["처음 배우는 사람", "기본은 아는 사람", "시험 직전 복습"])
    n_q = st.slider("소단원당 문제 수", 2, 6, 3)
    use_short = st.toggle("서술형 문제 섞기", value=True)

st.title("📚 AI 러닝메이트")

# 배포했을 때 아무나 내 API 키를 쓰지 못하게
app_pw = secret("APP_PASSWORD")
if app_pw and st.text_input("입장 비밀번호", type="password") != app_pw:
    st.stop()

if not api_key:
    st.info("왼쪽 메뉴(≡)에 API 키를 넣어주세요.")
    st.stop()

client = anthropic.Anthropic(api_key=api_key)


# ======================= Claude 호출 =======================
def ask(prompt: str, max_tokens: int = 2000, pdf: bytes | None = None) -> str:
    content = []
    if pdf:
        # 같은 PDF를 반복해서 보낼 때 비용이 줄도록 캐싱
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(pdf).decode()},
            "cache_control": {"type": "ephemeral"},
        })
    content.append({"type": "text", "text": prompt})
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system="너는 대학생의 공부를 돕는 학습 코치야. 항상 한국어로 답해.",
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        st.error("🔑 API 키가 올바르지 않아요. 왼쪽 메뉴에서 다시 확인해 주세요.")
        st.stop()
    except anthropic.RateLimitError:
        st.error("⏳ 요청이 너무 많아요. 1분쯤 기다렸다가 다시 눌러주세요.")
        st.stop()
    except anthropic.BadRequestError as e:
        st.error(f"요청이 거절됐어요. 자료가 너무 크거나 크레딧이 부족할 수 있어요.\n\n({e.message})")
        st.stop()
    except (anthropic.APIConnectionError, anthropic.APIStatusError):
        st.error("🌐 AI 서버에 연결하지 못했어요. 잠시 후 다시 눌러주세요.")
        st.stop()
    if resp.stop_reason == "max_tokens":
        raise ValueError("답이 너무 길어서 잘렸어요")
    return "".join(b.text for b in resp.content if getattr(b, "type", "text") == "text")


def ask_json(prompt: str, max_tokens: int = 4000, pdf: bytes | None = None):
    """AI 답에서 JSON 배열만 골라내기 (앞뒤 설명, ```json 감싸기 등이 있어도)"""
    raw = ask(prompt + "\n\nJSON 배열만 출력해. 다른 말은 쓰지 마.", max_tokens, pdf)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[", raw):
        try:
            obj, _ = dec.raw_decode(raw[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
            return obj
    raise ValueError("JSON을 찾지 못함")


# ======================= PDF 처리 =======================
def slice_pdf(start: int, end: int) -> bytes:
    """start~end쪽(1부터 셈)만 잘라낸 PDF"""
    end = min(end, start + MAX_DOC_PAGES - 1, ss.n_pages)
    cache = ss.setdefault("_pdf_cache", {})
    if (start, end) not in cache:
        reader = PdfReader(BytesIO(ss.pdf_bytes))
        writer = PdfWriter()
        for p in range(start - 1, end):
            writer.add_page(reader.pages[p])
        buf = BytesIO()
        writer.write(buf)
        cache[(start, end)] = buf.getvalue()
    return cache[(start, end)]


STRUCT_RULES = (
    "- 자료의 실제 목차와 흐름을 따를 것\n"
    "- 대단원마다 소단원은 2~5개\n"
    "- 소단원마다 pages: [시작쪽, 끝쪽] (자료 전체 기준 쪽수)\n"
    "- 소단원마다 시험에 필요한 핵심 내용을 3~6문장으로 정리한 points\n"
    '형식: [{"title": "대단원 제목", "sections": '
    '[{"title": "소단원 제목", "pages": [1, 3], "points": ["핵심1", "핵심2"]}]}]'
)


def build_units():
    """PDF를 대단원/소단원으로 나누기. 글자가 있으면 텍스트로, 스캔본이면 PDF를 직접 보여줌"""
    n = ss.n_pages
    if ss.text_ok:
        per_page = max(300, STRUCT_CHARS // n)
        text = "\n".join(f"[p.{i + 1}]\n{t[:per_page]}" for i, t in enumerate(ss.page_texts))
        units = ask_json(
            f"아래는 강의자료의 페이지별 텍스트야. [p.N]은 N쪽이고 전체 {n}쪽이야.\n"
            f"대단원과 소단원으로 나눠줘.\n{STRUCT_RULES}\n\n<자료>\n{text}\n</자료>",
            max_tokens=16000,
        )
    else:
        units = []
        bar = st.progress(0.0, text="스캔본이라 페이지를 직접 보는 중...")
        for s in range(1, n + 1, CHUNK_PAGES):
            e = min(n, s + CHUNK_PAGES - 1)
            part = ask_json(
                f"이 PDF는 전체 {n}쪽 자료 중 {s}~{e}쪽이야 (첫 페이지가 {s}쪽).\n"
                f"이 부분을 대단원과 소단원으로 나눠줘.\n{STRUCT_RULES}",
                max_tokens=16000, pdf=_raw_slice(s, e),
            )
            for u in part:  # 앞 부분과 같은 대단원이 이어지면 합치기
                if units and units[-1]["title"] == u["title"]:
                    units[-1]["sections"] += u["sections"]
                else:
                    units.append(u)
            bar.progress(e / n)
    # 빠진 값 채우기 + 쪽수 검증
    units = [u for u in units if isinstance(u, dict) and isinstance(u.get("sections"), list)]
    for ui, u in enumerate(units):
        u["title"] = str(u.get("title") or f"{ui + 1}단원")
        u["sections"] = [s for s in u["sections"] if isinstance(s, dict)]
        for si, sec in enumerate(u["sections"]):
            sec["title"] = str(sec.get("title") or f"{si + 1}절")
            pts = sec.get("points")
            sec["points"] = [str(p) for p in pts] if isinstance(pts, list) else ([str(pts)] if pts else [])
            if isinstance(sec.get("pages"), str):
                sec["pages"] = re.findall(r"\d+", sec["pages"]) or None
            try:
                pg = sec.get("pages") or [1, n]
                a, b = int(pg[0]), int(pg[-1])
            except (TypeError, ValueError, IndexError):
                a, b = 1, n
            a = min(max(a, 1), n)
            sec["pages"] = [a, min(max(b, a), n)]
    return [u for u in units if u.get("sections")]


def _raw_slice(start, end):
    reader = PdfReader(BytesIO(ss.pdf_bytes))
    writer = PdfWriter()
    for p in range(start - 1, end):
        writer.add_page(reader.pages[p])
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ======================= 상태 =======================
def reset_progress():
    ss.cleared = set()     # 통과한 단계 (대단원, 소단원) — 소단원 번호 = 소단원 수 이면 종합 테스트
    ss.quiz = None         # 지금 푸는 테스트
    ss.result = None       # 테스트 채점 결과
    ss.attempt = 0
    ss.asked = {}          # 단계별 이미 낸 문제
    ss.fails = {}          # 단계별 실패 횟수
    ss.concept_wrong = {}  # (대단원, 소단원, 개념) → 틀린 횟수
    ss.wrong_log = []      # 오답노트
    ss.hint_lv = {}        # 문제별 열어본 힌트 수
    ss.review = {}         # (대단원, 소단원, 개념) → {"box": 단계, "due": 다음 복습일}
    ss.rquiz = None        # 복습 문제
    ss.rresult = None
    ss.rattempt = 0


def _enc(d):
    return [[*k, v] for k, v in d.items()]


def _dec(lst, klen):
    return {tuple(x[:klen]): x[klen] for x in lst}


def save_path():
    name, size = ss.file_id[:2]
    tag = hashlib.md5(f"{name}-{size}".encode()).hexdigest()[:8]
    user = re.sub(r"[^\w가-힣-]", "_", nickname)
    return SAVE_DIR / user / f"{Path(name).stem}-{tag}.json"


def save():
    data = {
        "version": 3,
        "units": ss.units,
        "cleared": [list(p) for p in ss.cleared],
        "quiz": ss.quiz and {**ss.quiz, "stage": list(ss.quiz["stage"])},
        "result": ss.result,
        "attempt": ss.attempt,
        "asked": _enc(ss.asked),
        "fails": _enc(ss.fails),
        "concept_wrong": _enc(ss.concept_wrong),
        "wrong_log": [{**w, "stage": list(w["stage"])} for w in ss.wrong_log],
        "hint_lv": ss.hint_lv,
        "review": _enc(ss.review),
        "rquiz": ss.rquiz,
        "rresult": ss.rresult,
        "rattempt": ss.rattempt,
    }
    save_path().parent.mkdir(parents=True, exist_ok=True)
    save_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load(data):
    if data.get("version") != 3:
        raise ValueError("old version")
    ss.units = data["units"]
    ss.cleared = {tuple(p) for p in data["cleared"]}
    ss.quiz = data["quiz"] and {**data["quiz"], "stage": tuple(data["quiz"]["stage"])}
    ss.result = data["result"]
    ss.attempt = data["attempt"]
    ss.asked = _dec(data["asked"], 2)
    ss.fails = _dec(data["fails"], 2)
    ss.concept_wrong = _dec(data["concept_wrong"], 3)
    ss.wrong_log = [{**w, "stage": tuple(w["stage"])} for w in data["wrong_log"]]
    ss.hint_lv = data["hint_lv"]
    ss.review = _dec(data["review"], 3)
    ss.rquiz = data["rquiz"]
    ss.rresult = data["rresult"]
    ss.rattempt = data["rattempt"]


if "units" not in ss:
    ss.units = None
    ss.file_id = None
    reset_progress()

# ======================= 1. PDF 업로드 =======================
uploaded = st.file_uploader("강의자료 PDF", type=["pdf"])
if not uploaded:
    st.caption("PDF를 단원별로 나누고, 테스트를 통과해야 다음 단원이 열려요. 틀린 개념은 날짜에 맞춰 다시 복습시켜 줘요.")
    st.stop()

if ss.file_id != (uploaded.name, uploaded.size, nickname):  # 새 파일(또는 닉네임 변경)
    ss.file_id = (uploaded.name, uploaded.size, nickname)
    ss.pdf_bytes = uploaded.getvalue()
    ss._pdf_cache = {}
    try:
        reader = PdfReader(BytesIO(ss.pdf_bytes))
        ss.page_texts = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception:
        st.error("PDF를 열 수 없어요. 파일이 손상됐거나 암호가 걸려 있을 수 있어요.")
        st.stop()
    ss.n_pages = len(ss.page_texts)
    ss.text_ok = sum(len(t) for t in ss.page_texts) > 100 * ss.n_pages * 0.3
    ss.units = None
    reset_progress()
    if save_path().exists():
        try:
            load(json.loads(save_path().read_text(encoding="utf-8")))
            st.toast("💾 지난번 진도를 불러왔어요!")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            reset_progress()
            st.warning("저장된 진도를 읽지 못해서 처음부터 시작해요.")

# ======================= 2. 단원 나누기 =======================
if ss.units is None:
    kind = "글자 PDF" if ss.text_ok else "스캔본/이미지 PDF (AI가 페이지를 직접 봐요)"
    st.success(f"{ss.n_pages}쪽짜리 자료를 읽었어요 · {kind}")
    if st.button("📂 단원 나누기", type="primary"):
        with st.spinner("목차를 분석하는 중... (자료가 길면 몇 분 걸려요)"):
            try:
                ss.units = build_units()
                if not ss.units:
                    raise ValueError("empty")
                save()
                st.rerun()
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                ss.units = None
                st.error("단원 나누기에 실패했어요. 다시 눌러주세요.")
    st.stop()

units = ss.units


# ======================= 단계(소단원 / 종합 테스트) 도우미 =======================
def n_sec(ci):
    return len(units[ci]["sections"])


def is_final(ci, si):
    return si == n_sec(ci)


def stage_name(ci, si):
    if is_final(ci, si):
        return f"{ci + 1}. {units[ci]['title']} — 🏁 종합 테스트"
    return f"{ci + 1}-{si + 1}. {units[ci]['sections'][si]['title']}"


def stage_points(ci, si):
    if is_final(ci, si):
        return [f"[{s['title']}] {p}" for s in units[ci]["sections"] for p in s["points"]]
    return units[ci]["sections"][si]["points"]


def stage_pdf(ci, si):
    if is_final(ci, si):
        a = min(s["pages"][0] for s in units[ci]["sections"])
        b = max(s["pages"][1] for s in units[ci]["sections"])
        if b - a + 1 > MAX_DOC_PAGES:
            return None  # 대단원이 너무 길면 핵심 정리만으로 출제
        return slice_pdf(a, b)
    a, b = units[ci]["sections"][si]["pages"]
    return slice_pdf(a, b)


def all_stages():
    for ci in range(len(units)):
        for si in range(n_sec(ci) + 1):
            yield ci, si


def current_stage():
    return next((s for s in all_stages() if s not in ss.cleared), None)


def is_important(ci, si, concept):
    return ss.concept_wrong.get((ci, si, concept), 0) >= IMPORTANT_AT


def due_reviews():
    return sorted(k for k, v in ss.review.items() if v["due"] <= TODAY.isoformat())


# ======================= 문제 만들기 / 채점 =======================
def gen_questions(ci, si, count, focus=(), exact=None, avoid=()):
    """count개 문제 생성. exact가 있으면 그 개념들로 순서대로 한 문제씩"""
    n_short = (max(1, count // 3) if count >= 2 else 0) if use_short else 0
    known = sorted({k[2] for k in ss.concept_wrong if k[:2] == (ci, si)})
    rules = [
        f"'{stage_name(ci, si)}' 범위에서 문제 {count}개를 만들어줘. 그중 서술형은 {n_short}개, 나머지는 객관식.",
        f"난이도: {level}",
        f"핵심 내용: {stage_points(ci, si)}",
        "- 단순 암기보다 이해했는지 확인하는 문제 위주로",
        "- 객관식은 보기 4개, 그럴듯한 오답을 넣을 것",
        "- 서술형은 2~4문장으로 답할 수 있는 크기로",
    ]
    if is_final(ci, si):
        rules.append("- 여러 소단원 내용을 골고루 섞고, 소단원끼리 연결하는 문제도 넣을 것")
    if avoid:
        rules.append(f"- 이미 낸 문제와 똑같은 문제는 금지: {list(avoid)[-30:]}")
    if focus:
        rules.append(f"- 학생이 틀린 개념이야. 각 개념마다 이전과 다른 방식의 문제를 한 개씩 꼭 넣어: {list(focus)}")
    if exact:
        rules.append(f"- 다음 개념마다 정확히 한 문제씩, 이 순서대로 만들 것: {list(exact)}")
    rules += [
        f"- concept: 문제가 확인하는 개념 이름(짧게). 목록에 같은 개념이 있으면 그 이름을 그대로: {known}",
        "- hints: 정답을 바로 알려주지 않는 힌트 3개 (1번은 생각할 방향, 2번은 더 구체적으로, "
        "3번은 객관식이면 오답 하나 지워주기 / 서술형이면 답의 첫 부분 알려주기)",
        '객관식 형식: {"type": "mc", "q": "문제", "options": ["","","",""], "answer": 정답 인덱스(0~3), '
        '"explain": "해설", "concept": "개념", "hints": ["","",""]}',
        '서술형 형식: {"type": "short", "q": "문제", "model_answer": "모범답안", "key_points": ["채점 핵심1"], '
        '"explain": "해설", "concept": "개념", "hints": ["","",""]}',
        "JSON 배열로.",
    ]
    pdf = stage_pdf(ci, si)
    if pdf is None:
        rules.append("(원문 대신 위 핵심 내용을 근거로 출제)")
    items = ask_json("\n".join(rules), max_tokens=5000, pdf=pdf)

    good = []
    for it in items:
        it["type"] = "short" if it.get("type") == "short" or (not it.get("options") and it.get("model_answer")) else "mc"
        if isinstance(it.get("options"), list):
            it["options"] = [str(o) for o in it["options"]]
        if not isinstance(it.get("hints"), list):
            it["hints"] = []
        it["concept"] = str(it.get("concept") or "기타")
        it.setdefault("concept", "기타")
        it.setdefault("hints", [])
        it.setdefault("explain", "")
        if it["type"] == "mc":
            it["answer"] = to_index(it.get("answer"), it.get("options"))
            ok = it["answer"] is not None
        else:
            ok = bool(it.get("model_answer"))
            it["model_answer"] = str(it.get("model_answer"))
            if not isinstance(it.get("key_points"), list):
                it["key_points"] = []
        if ok and it.get("q"):
            it["stage"] = [ci, si]
            good.append(it)
    if exact and len(good) == len(exact):
        for it, c in zip(good, exact):
            it["concept"] = c
    if not good:
        raise ValueError("no questions")
    return good


def to_index(ans, options):
    """정답을 0~3 번호로 맞추기 (0, "1", "B", "B) 보기", 보기 글자 그대로 등)"""
    if not isinstance(options, list) or len(options) < 2:
        return None
    opts = [str(o).strip() for o in options]
    if isinstance(ans, bool):
        return None
    if isinstance(ans, int):
        return ans if 0 <= ans < len(opts) else None
    a = str(ans).strip()
    if a in opts:
        return opts.index(a)
    if a.isdigit():
        k = int(a)
        return k if 0 <= k < len(opts) else None
    m = re.match(r"^([A-Da-d①-④])(?:[).:\s]|$)", a)
    if m:
        k = "ABCD".find(m.group(1).upper()) if m.group(1).isascii() else "①②③④".find(m.group(1))
        return k if k < len(opts) else None
    for i, o in enumerate(opts):  # "B) 보기"가 보기 글자와 일부 일치
        if a and (a in o or o in a):
            return i
    return None


def correct_text(it):
    return it["options"][it["answer"]] if it["type"] == "mc" else it["model_answer"]


def grade(items, answers):
    """객관식은 바로, 서술형은 AI가 채점"""
    correct = [None] * len(items)
    feedback = [""] * len(items)
    shorts = []
    for i, (it, a) in enumerate(zip(items, answers)):
        if it["type"] == "mc":
            correct[i] = a == correct_text(it)
        else:
            shorts.append(i)
    if shorts:
        payload = [{"문제": items[i]["q"], "모범답안": items[i]["model_answer"],
                    "채점핵심": items[i]["key_points"], "학생답": answers[i]} for i in shorts]
        res = ask_json(
            "학생의 서술형 답을 채점해줘.\n"
            "- 핵심을 제대로 이해했으면 표현이 달라도 정답\n"
            "- 핵심이 빠졌거나 틀린 내용이 있으면 오답\n"
            "- feedback: 1~2문장. 잘한 점과 빠진 부분을 짚어줄 것\n"
            '형식: [{"correct": true, "feedback": "..."}] (문항 순서대로)\n\n'
            + json.dumps(payload, ensure_ascii=False),
            max_tokens=2000,
        )
        if len(res) != len(shorts):
            raise ValueError("grading mismatch")
        for i, r in zip(shorts, res):
            correct[i] = bool(r.get("correct"))
            feedback[i] = r.get("feedback", "")
    return {"answers": answers, "correct": correct, "feedback": feedback}


def record_wrong(it, answer, fb):
    """틀린 문제를 오답노트·중요 표시·복습 일정에 반영"""
    ci, si = it["stage"]
    key = (ci, si, it["concept"])
    ss.concept_wrong[key] = ss.concept_wrong.get(key, 0) + 1
    ss.wrong_log.append({
        "stage": (ci, si), "concept": it["concept"], "type": it["type"], "q": it["q"],
        "pick": answer, "correct": correct_text(it),
        "explain": (fb + "\n" if fb else "") + it.get("explain", ""),
        "date": TODAY.isoformat(),
    })
    ss.review[key] = {"box": 0, "due": (TODAY + timedelta(days=INTERVALS[0])).isoformat()}


def render_questions(items, prefix, result, hint_ok):
    """문제 화면. 채점 전에는 입력칸, 채점 후에는 결과"""
    answers = []
    for i, it in enumerate(items):
        ci, si = it["stage"]
        star = "⭐ " if is_important(ci, si, it["concept"]) else ""
        tag = "✍️ [서술형] " if it["type"] == "short" else ""
        st.markdown(f"**{star}Q{i + 1}. {tag}{it['q']}**")
        key = f"{prefix}-{i}"

        if result is None:
            if it["type"] == "mc":
                a = st.radio(key, it["options"], index=None, key=key, label_visibility="collapsed")
            else:
                a = st.text_area(key, key=key, placeholder="내 말로 설명해 보세요",
                                 label_visibility="collapsed").strip() or None
            answers.append(a)
            hints = it.get("hints", [])
            if hints and hint_ok(it):
                lv = ss.hint_lv.get(key, 0)
                for h in hints[:lv]:
                    st.info(f"💡 {h}")
                if lv < len(hints) and st.button(f"💡 힌트 보기 ({lv + 1}/{len(hints)})", key=f"h-{key}"):
                    ss.hint_lv[key] = lv + 1
                    save()
                    st.rerun()
        else:
            ok = result["correct"][i]
            st.markdown(f"{'✅' if ok else '❌'} 내 답: {result['answers'][i]}")
            if result["feedback"][i]:
                st.caption(f"🧑‍🏫 {result['feedback'][i]}")
            if not ok:
                with st.expander("정답과 해설 보기", expanded=True):
                    st.write(f"정답: {correct_text(it)}")
                    if it.get("explain"):
                        st.write(f"해설: {it['explain']}")
        st.write("")
    return answers


cur = current_stage()
due = due_reviews()

# ======================= 사이드바: 메뉴 + 진도 =======================
with st.sidebar:
    n_wrong = len({(w["stage"], w["concept"]) for w in ss.wrong_log})
    menu = st.radio("메뉴", ["📖 공부하기", "🔁 오늘의 복습", "📒 오답노트"], key="menu")
    st.caption(f"🔁 오늘 복습 {len(due)}개 · 📒 오답 개념 {n_wrong}개")

    st.header("🗺️ 진도")
    stages = list(all_stages())
    st.progress(len(ss.cleared) / len(stages), text=f"{len(ss.cleared)} / {len(stages)} 단계 통과")
    for ci, unit in enumerate(units):
        unit_done = (ci, n_sec(ci)) in ss.cleared
        st.markdown(f"**{'✅' if unit_done else '📘'} {ci + 1}. {unit['title']}**")
        for si in range(n_sec(ci) + 1):
            icon = "✅" if (ci, si) in ss.cleared else "▶️" if (ci, si) == cur else "🔒"
            star = " ⭐" if any(k[:2] == (ci, si) and v >= IMPORTANT_AT for k, v in ss.concept_wrong.items()) else ""
            label = "🏁 종합 테스트" if is_final(ci, si) else f"{ci + 1}-{si + 1}. {unit['sections'][si]['title']}"
            st.markdown(f"&nbsp;&nbsp;&nbsp;{icon} {label}{star}")

    st.divider()
    st.download_button(
        "💾 진도 백업 받기", save_path().read_bytes() if save_path().exists() else b"{}",
        file_name=save_path().name, mime="application/json",
    )
    backup = st.file_uploader("📂 백업 불러오기", type=["json"], key="restore")
    if backup and ss.get("restored") != (backup.name, backup.size):
        try:
            load(json.loads(backup.getvalue().decode("utf-8")))
            ss.restored = (backup.name, backup.size)
            save()
            st.rerun()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, UnicodeDecodeError):
            st.error("백업 파일을 읽지 못했어요.")
    if st.button("진도 초기화"):
        reset_progress()
        save()
        st.rerun()

# =====================================================
# 📒 오답노트
# =====================================================
if menu.startswith("📒"):
    st.header("📒 오답노트")
    if not ss.wrong_log:
        st.info("아직 틀린 문제가 없어요. 👍")
        st.stop()
    only_star = st.toggle("⭐ 중요 문제만 보기")
    groups = {}
    for w in ss.wrong_log:
        groups.setdefault((w["stage"], w["concept"]), []).append(w)
    for ((ci, si), concept), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        star = is_important(ci, si, concept)
        if only_star and not star:
            continue
        rv = ss.review.get((ci, si, concept))
        nxt = f" · 다음 복습 {rv['due'][5:]}" if rv else " · 복습 완료 🎓"
        title = f"{'⭐ ' if star else ''}{concept} — {len(items)}번 틀림{nxt}  ({stage_name(ci, si)})"
        with st.expander(title, expanded=star):
            for w in reversed(items):
                st.markdown(f"**Q. {w['q']}**  \n<small>{w.get('date', '')}</small>", unsafe_allow_html=True)
                st.markdown(f"- ❌ 내 답: {w['pick']}\n- ✅ 정답: {w['correct']}")
                st.caption(f"해설: {w['explain']}")
                st.divider()
    st.stop()

# =====================================================
# 🔁 오늘의 복습 (간격 반복)
# =====================================================
if menu.startswith("🔁"):
    st.header("🔁 오늘의 복습")
    st.caption("틀렸던 개념을 1 → 3 → 7 → 14 → 30일 간격으로 다시 물어봐요. 다 맞히면 졸업! 🎓")

    if ss.rquiz is None:
        if not due:
            upcoming = sorted(v["due"] for v in ss.review.values())
            if upcoming:
                st.info(f"오늘은 복습할 게 없어요. 다음 복습: {upcoming[0]}")
            else:
                st.info("복습할 개념이 없어요. 틀린 문제가 생기면 여기로 와요.")
            st.stop()
        todo = due[:REVIEW_MAX]
        st.write(f"오늘 복습할 개념 **{len(due)}개**" + (f" (이번엔 {len(todo)}개만)" if len(due) > len(todo) else ""))
        for ci, si, c in todo:
            st.markdown(f"- {'⭐ ' if is_important(ci, si, c) else ''}{c} <small>({stage_name(ci, si)})</small>",
                        unsafe_allow_html=True)
        if st.button("복습 시작", type="primary"):
            by_stage = {}
            for ci, si, c in todo:
                by_stage.setdefault((ci, si), []).append(c)
            items = []
            with st.spinner("복습 문제 만드는 중..."):
                try:
                    for (ci, si), concepts in by_stage.items():
                        items += gen_questions(ci, si, len(concepts), exact=concepts,
                                               avoid=ss.asked.get((ci, si), []))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    st.error("문제를 만드는 데 실패했어요. 다시 눌러주세요.")
                    st.stop()
            ss.rattempt += 1
            ss.rquiz = {"items": items}
            ss.rresult = None
            save()
            st.rerun()
        st.stop()

    items = ss.rquiz["items"]
    answers = render_questions(
        items, f"r{ss.rattempt}", ss.rresult,
        hint_ok=lambda it: ss.concept_wrong.get((*it["stage"], it["concept"]), 0) >= HINT_AT,
    )

    if ss.rresult is None:
        if st.button("채점하기", type="primary", disabled=None in answers):
            with st.spinner("채점하는 중..."):
                try:
                    res = grade(items, answers)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    st.error("채점에 실패했어요. 다시 눌러주세요.")
                    st.stop()
            for it, ok, a, fb in zip(items, res["correct"], answers, res["feedback"]):
                key = (*it["stage"], it["concept"])
                if ok:
                    box = ss.review.get(key, {"box": 0})["box"] + 1
                    if box >= len(INTERVALS):
                        ss.review.pop(key, None)  # 졸업
                    else:
                        ss.review[key] = {"box": box, "due": (TODAY + timedelta(days=INTERVALS[box])).isoformat()}
                else:
                    record_wrong(it, a, fb)
            ss.rresult = res
            save()
            st.rerun()
        if None in answers:
            st.caption("모든 문제에 답해야 채점할 수 있어요.")
        st.stop()

    score = sum(ss.rresult["correct"])
    st.subheader(f"복습 결과: {score} / {len(items)}")
    st.caption("맞힌 개념은 다음 복습까지 간격이 늘어나고, 틀린 개념은 내일 다시 나와요.")
    if st.button("복습 끝내기", type="primary"):
        ss.rquiz = None
        ss.rresult = None
        save()
        st.rerun()
    st.stop()

# =====================================================
# 📖 공부하기
# =====================================================
if due:
    st.info(f"🔁 오늘 복습할 개념이 {len(due)}개 있어요. 왼쪽 메뉴에서 '오늘의 복습'을 눌러보세요.")

if cur is None:
    st.balloons()
    st.header("🎉 모든 단원을 통과했어요!")
    st.write("이제 '오늘의 복습'으로 틀렸던 개념을 날짜에 맞춰 다시 보면 오래 기억에 남아요.")
    st.stop()

ci, si = cur
final = is_final(ci, si)
fails = ss.fails.get((ci, si), 0)

st.subheader(f"📘 {ci + 1}. {units[ci]['title']}")
if final:
    st.markdown("### 🏁 대단원 종합 테스트")
    st.caption(f"모든 소단원 내용을 섞어서 물어봐요. {int(FINAL_PASS * 100)}% 이상 맞히면 다음 대단원이 열려요.")
else:
    a, b = units[ci]["sections"][si]["pages"]
    st.markdown(f"### ▶️ {stage_name(ci, si)}")
    st.caption(f"자료 {a}~{b}쪽")
if fails:
    st.caption(f"도전 {fails + 1}번째")

with st.expander("📖 핵심 정리 먼저 보기"):
    for p in stage_points(ci, si):
        st.markdown(f"- {p}")

with st.expander("💬 이 단원에서 모르는 거 물어보기"):
    q = st.text_input("질문", key=f"ask-{ci}-{si}")
    if q and st.button("물어보기"):
        with st.spinner("생각하는 중..."):
            st.markdown(ask(
                f"'{stage_name(ci, si)}' 공부 중인 학생의 질문: {q}\n"
                f"핵심 내용: {stage_points(ci, si)}\n"
                f"'{level}' 수준에 맞게, 자료를 근거로 쉽게 설명해줘.",
                pdf=stage_pdf(ci, si),
            ))


def start_test(focus=()):
    count = n_q + 2 if final else n_q
    with st.spinner("문제 만드는 중..."):
        try:
            items = gen_questions(ci, si, count, focus=focus, avoid=ss.asked.get((ci, si), []))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            st.error("문제를 만드는 데 실패했어요. 다시 눌러주세요.")
            return
    ss.quiz = {"stage": (ci, si), "items": items}
    ss.asked[(ci, si)] = ss.asked.get((ci, si), []) + [it["q"] for it in items]
    ss.result = None
    ss.attempt += 1
    save()
    st.rerun()


if ss.quiz is None or ss.quiz["stage"] != (ci, si):
    if st.button("📝 테스트 시작", type="primary"):
        start_test()
    st.stop()

items = ss.quiz["items"]
answers = render_questions(
    items, f"t{ss.attempt}", ss.result,
    hint_ok=lambda it: ss.concept_wrong.get((ci, si, it["concept"]), 0) >= HINT_AT or fails >= HINT_AT,
)

if ss.result is None:
    if st.button("채점하기", type="primary", disabled=None in answers):
        with st.spinner("채점하는 중..."):
            try:
                res = grade(items, answers)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                st.error("채점에 실패했어요. 다시 눌러주세요.")
                st.stop()
        for it, ok, a, fb in zip(items, res["correct"], answers, res["feedback"]):
            if not ok:
                record_wrong(it, a, fb)
        score = sum(res["correct"])
        need = len(items) if not final else -(-len(items) * FINAL_PASS // 1)
        res["passed"] = score >= need
        if not res["passed"]:
            ss.fails[(ci, si)] = fails + 1
        ss.result = res
        save()
        st.rerun()
    if None in answers:
        st.caption("모든 문제에 답해야 채점할 수 있어요.")
    st.stop()

# ---------- 채점 결과 ----------
score = sum(ss.result["correct"])
if ss.result["passed"]:
    if final:
        st.balloons()
        st.success(f"🎉 {score} / {len(items)} — 대단원 '{units[ci]['title']}' 완료! 다음 대단원이 열려요.")
    else:
        st.success(f"💯 {score} / {len(items)} — 소단원 통과!")
    if any(not c for c in ss.result["correct"]):
        st.caption("틀린 문제는 오답노트와 복습 일정에 들어갔어요.")
    if st.button("다음으로 ▶", type="primary"):
        ss.cleared.add((ci, si))
        ss.quiz = None
        ss.result = None
        save()
        st.rerun()
else:
    rule = f"{int(FINAL_PASS * 100)}% 이상" if final else "전부"
    st.error(f"{score} / {len(items)} — {rule} 맞혀야 다음으로 넘어갈 수 있어요.")
    if ss.fails.get((ci, si), 0) >= HINT_AT:
        st.info("💡 이제 이 단계 문제에서 힌트를 볼 수 있어요.")
    st.caption("틀린 개념은 다른 문제로 다시 나와요. 오답노트와 복습 일정에도 들어갔어요.")
    if st.button("🔄 새 문제로 다시 도전", type="primary"):
        wrong_concepts = sorted({it["concept"] for it, c in zip(items, ss.result["correct"]) if not c})
        start_test(focus=wrong_concepts)
