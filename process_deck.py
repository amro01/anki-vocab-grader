#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Anki 牌组批量打标签，并在背面顶部生成分层提示。

输入（默认位于脚本所在目录）:
    2026-10-27.colpkg, LONGMAN3000.txt, 1600.txt, 3500.txt
输出:
    2026-10-27_tagged.colpkg（原始 colpkg 保持不变）

核心配置:
    LONGMAN_CORE_TARGETS  可自由组合 {"W1", "S1"} / {"W1", "S1", "W2"} 等。
    命中任意目标频级的单词打 lm3k_core，并显示实际命中的频级。

说明:
    * 新版 .colpkg 的真实数据库在 collection.anki21b（zstd 压缩的 SQLite），
      collection.anki2 往往只是“请升级 Anki”的占位库，因此优先处理 anki21b。
    * Longman 文本来自 djvu OCR：存在 W1 -> Wl/WI/VV1、词性与单词粘连、
      词条跨行等瑕疵，解析器按“词条行 + 后续词性/频度行”的状态机处理。
    * 匹配前对卡片正面做保守的词形还原：不规则动词/复数、be 动词变体、
      常见缩写和规则复数/过去式/现在分词；只有词形还原结果命中词表时才使用。
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sqlite3
import string
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import zstandard

# ---------------------------------------------------------------------------
# 可调配置
# ---------------------------------------------------------------------------

# 任意组合，例如 {"W1", "S1"} 或 {"W1", "S1", "W2"}
LONGMAN_CORE_TARGETS = {"W1", "S1"}

CORE_TAG = "lm3k_core"
OLD_CORE_TAG = "lm3kw1"
ZK_TAG = "zk16k"
GK_TAG = "gk35k"
MANAGED_TAGS = {CORE_TAG, OLD_CORE_TAG, ZK_TAG, GK_TAG}

LEVEL_ORDER = ("S1", "S2", "S3", "W1", "W2", "W3")
GRAY_HINT_TEMPLATE = (
    '<div style="color: #888888; font-size: 0.9em; margin-bottom: 4px;">'
    "Longman Core ({levels})</div>"
)
RED_HINT = (
    '<div style="color: red; font-weight: bold; margin-bottom: 8px;">'
    "中小学1600词（必须掌握听说读写）</div>"
)
OLD_HINT_MARK = "LongMan 3000 W1"

FIELD_SEP = "\x1f"

# ---------------------------------------------------------------------------
# Longman OCR 词条解析
# ---------------------------------------------------------------------------

# djvu OCR 的词性写法（n/v 常与词条粘连，adj/ad]/ad/ 等混用）
POS_WORDS = {
    "n", "v", "vt", "vi", "adj", "odj", "adv", "prep", "pron", "pran", "pra",
    "conj", "con]", "con/", "conl", "cony", "det",
    "determiner", "determiners", "predeterminer", "predeterminers",
    "interjection", "interjections", "modal", "mode", "mode/", "mode|",
    "moda/", "moda|", "auxiliary", "indefinite", "ad", "ad]", "ad/", "ad}", "ad|",
    "int", "ir", "article", "articles", "number", "definite",
}
HEAD_POS_COLLISIONS = {"article", "articles", "number"}
POS_ATOM = (
    r"(?:n|v|vt|vi|adj|odj|ad(?:j|[\]/}|])?|adv|prep|pron|pra[n]?|"
    r"con(?:j|y|[/\]])|determiners?|predeterminers?|interjections?|"
    r"mode(?:l|[/|])?|moda(?:l|[/|])|auxiliary|indefinite|definite|articles?|number|ir)"
)
MARK_ATOM = r"(?:(?:[SW]|VV)[1-3lLI!|])"
OCR_V_ATOM = r"(?:[il1]?[/\\!|]+)"
TAIL_ATOM = rf"(?:{POS_ATOM}|{MARK_ATOM}|{OCR_V_ATOM})"
TAIL_RE = re.compile(rf"^[\s,.;:]*{TAIL_ATOM}(?:[\s,.;:]*{TAIL_ATOM})*[\s,.;:]*$", re.I)
MARK_RE = re.compile(r"(?:(?:[SW]|VV)[1-3lLI!|])", re.I)
MULTI_POS_RE = re.compile(
    r"(?:adj|odj|adv|prep|pron|conj|det|determiners?|predeterminers?|"
    r"interjections?|mode(?:l|[/|])?|moda(?:l|[/|])|auxiliary|indefinite|definite)",
    re.I,
)
TRAILING_POS = {
    "n", "v", "vt", "vi", "adj", "adv", "prep", "pron", "conj",
    "modal", "a", "ad", "art", "num", "int", "aux",
}


def _is_tail_token(token: str) -> bool:
    """判断一个空白分隔的 token 是否属于词性/频度尾巴。"""
    stripped = token.strip(",.;:")
    if not stripped:
        return True
    low = stripped.lower()
    if low in POS_WORDS or MARK_RE.fullmatch(stripped):
        return True
    # 词性 OCR 常出现 deferm/ner、ad]、mode/ 等非单词 token
    if re.search(r"[/\\\]\}|]", stripped):
        return True
    # nSI / vSI / S1W1 这类纯字母但含频度标记的粘连写法则归入尾巴
    if re.fullmatch(r"[A-Za-z]+", stripped):
        return bool(MARK_RE.search(stripped) and TAIL_RE.fullmatch(stripped))
    return bool(TAIL_RE.fullmatch(token))


def _attached_cut(token: str) -> int | None:
    """把 ``acidnW3`` 这类粘连 token 切在词条与词性/频度之间。

    返回词条的结束下标；找不到返回 None。只有尾巴里出现频度标记、斜杠，
    或明确的多字母词性（如 pron）时才切，避免把 action 误切成 actio。
    """
    # 从右往左找最长的词条前缀；否则 leadvS1 会被切成 le + advS1
    for pos in range(len(token) - 1, 0, -1):
        head, tail = token[:pos], token[pos:]
        if not re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", head):
            continue
        # areanSI.WI：不能把频度标记 SI 并进词条
        if re.search(r"[SW][1-3lLI!|]$", head):
            continue
        if not TAIL_RE.fullmatch(tail):
            continue
        if MARK_RE.search(tail) or re.search(r"[/\\!|]", tail) or MULTI_POS_RE.fullmatch(tail):
            return pos
    return None


def _clean_headword(text: str) -> str | None:
    text = re.sub(r"\s+", " ", text).strip(" .,;:")
    if not re.fullmatch(r"[A-Za-z][A-Za-z'\- ]*", text):
        return None
    return text.lower()


def _split_headword(line: str) -> tuple[str | None, str]:
    """返回 (词条, 词性/频度尾巴)。无法识别词条时返回 (None, "")。"""
    tokens = line.split()
    heads: list[str] = []
    for index, token in enumerate(tokens):
        if index == 0 and token.lower().strip(",.;:") in HEAD_POS_COLLISIONS:
            # article/number 既是词性也是词条：行首时按词条处理
            heads.append(token)
            continue
        if _is_tail_token(token):
            return _clean_headword(" ".join(heads)), " ".join(tokens[index:])

        cut = _attached_cut(token)
        if cut is not None:
            heads.append(token[:cut])
            tail = token[cut:]
            if index + 1 < len(tokens):
                tail += " " + " ".join(tokens[index + 1:])
            return _clean_headword(" ".join(heads)), tail

        heads.append(token)
    return None, ""


def _strip_absorbed_pos(headword: str, tail: str) -> str:
    """``earn SI, Wl`` 实际是 ``ear n S1, W1``：n 被 OCR 并进了单词。

    只处理“尾巴直接以频度标记开头”的情况；若尾巴还有独立词性（如
    ``action n SI``），则保留单词原样。
    """
    if headword and tail and len(headword) >= 3 and headword[-1] in "nv":
        if MARK_RE.match(tail.lstrip()):
            return headword[:-1]
    return headword


def _normalize_frequency_ocr(text: str) -> str:
    # VV1/VV2/VV3 是 W1/W2/W3 的常见 djvu 误识
    return re.sub(r"(?i)\bVV(?=[1-3])", "W", text)


LEVEL_RE = re.compile(r"([SW])([1-3lLI!|])(?![A-Za-z0-9])")


def _extract_levels(text: str) -> set[str]:
    """从词条尾巴提取 S1-S3 / W1-W3，兼容 SI、Wl、WI 等 OCR 误识。"""
    levels: set[str] = set()
    for match in LEVEL_RE.finditer(_normalize_frequency_ocr(text)):
        marker = match.group(1).upper()
        digit_char = match.group(2)
        digit = "1" if digit_char in "1lLI!|" else digit_char
        levels.add(marker + digit)
    return levels


def _parse_longman_entries(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    current: str | None = None
    buffer: list[str] = []
    started = False

    def flush() -> None:
        nonlocal current, buffer
        if current is not None:
            entries.append((current, " ".join(buffer)))
        current, buffer = None, []

    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"\d+", line) or "LONGMAN" in line.upper():
            continue

        if not started:
            head, _ = _split_headword(line)
            if head is None:
                continue  # 前言 / 页眉
            started = True

        first_token = line.split()[0]
        if (
            _is_tail_token(first_token)
            and first_token.lower().strip(",.;:") not in HEAD_POS_COLLISIONS
        ):
            if current is not None:
                buffer.append(line)
            continue

        head, tail = _split_headword(line)
        if head is not None:
            flush()
            head = _strip_absorbed_pos(head, tail)
            current, buffer = head, [line]
        elif current is not None:
            buffer.append(line)
    flush()
    return entries


def parse_longman_levels(path: Path) -> dict[str, set[str]]:
    """返回 {词条: {S1,S2,S3,W1,W2,W3}}，同一单词的多个词性条目合并。"""
    result: dict[str, set[str]] = {}
    for headword, text in _parse_longman_entries(path):
        result.setdefault(headword, set()).update(_extract_levels(text))
    return result


# ---------------------------------------------------------------------------
# 词形还原
# ---------------------------------------------------------------------------

_IRREGULAR_VERBS = {
    "be": "am is are was were been being",
    "have": "has had having",
    "do": "does did done doing",
    "go": "goes went gone",
    "eat": "ate eaten",
    "buy": "bought",
    "come": "came",
    "drink": "drank drunk",
    "fall": "fell fallen",
    "feel": "felt",
    "get": "got gotten",
    "hold": "held",
    "learn": "learnt learned",
    "lose": "lost",
    "meet": "met",
    "run": "ran",
    "sing": "sang sung",
    "see": "saw seen",
    "swim": "swam swum",
    "teach": "taught",
    "take": "took taken",
    "write": "wrote written",
    "speak": "spoke spoken",
    "break": "broke broken",
    "choose": "chose chosen",
    "drive": "drove driven",
    "fly": "flew flown",
    "grow": "grew grown",
    "know": "knew known",
    "throw": "threw thrown",
    "wear": "wore worn",
    "tear": "tore torn",
    "ride": "rode ridden",
    "rise": "rose risen",
    "wake": "woke woken",
    "freeze": "froze frozen",
    "steal": "stole stolen",
    "hide": "hid hidden",
    "bite": "bit bitten",
    "feed": "fed",
    "lead": "led",
    "flee": "fled",
    "build": "built",
    "send": "sent",
    "spend": "spent",
    "lend": "lent",
    "bend": "bent",
    "keep": "kept",
    "sleep": "slept",
    "weep": "wept",
    "sweep": "swept",
    "creep": "crept",
    "deal": "dealt",
    "mean": "meant",
    "hear": "heard",
    "make": "made",
    "pay": "paid",
    "say": "said",
    "sell": "sold",
    "tell": "told",
    "stand": "stood",
    "understand": "understood",
    "find": "found",
    "bind": "bound",
    "wind": "wound",
    "fight": "fought",
    "catch": "caught",
    "bring": "brought",
    "think": "thought",
    "seek": "sought",
    "shoot": "shot",
    "shine": "shone",
    "win": "won",
    "sit": "sat",
    "leave": "left",
    "light": "lit",
    "burn": "burnt burned",
    "dream": "dreamt dreamed",
    "smell": "smelt smelled",
    "spell": "spelt spelled",
    "spill": "spilt spilled",
    "spoil": "spoilt spoiled",
    "show": "showed shown",
    "shake": "shook shaken",
    "sew": "sewed sewn",
    "sow": "sowed sown",
    "prove": "proved proven",
    "strike": "struck",
    "swear": "swore sworn",
    "draw": "drew drawn",
    "withdraw": "withdrew withdrawn",
    "become": "became",
    "begin": "began begun",
    "forget": "forgot forgotten",
    "forgive": "forgave forgiven",
    "give": "gave given",
    "ring": "rang rung",
    "spring": "sprang sprung",
    "sink": "sank sunk",
    "shrink": "shrank shrunk",
    "stick": "stuck",
    "sting": "stung",
    "stink": "stank stunk",
    "swing": "swung",
    "hang": "hung",
    "kneel": "knelt",
    "lean": "leant leaned",
    "leap": "leapt leaped",
    "mistake": "mistook mistaken",
    "overcome": "overcame",
    "speed": "sped speeded",
    "spin": "spun",
    "spit": "spat",
    "arise": "arose arisen",
    "awake": "awoke awoken",
    "bear": "bore born borne",
    "beat": "beaten",
    "bleed": "bled",
    "breed": "bred",
    "cling": "clung",
    "dig": "dug",
    "fling": "flung",
    "grind": "ground",
    "lie": "lain",
    "mislead": "misled",
    "rebuild": "rebuilt",
    "repay": "repaid",
    "retell": "retold",
    "rewrite": "rewrote rewritten",
    "slay": "slew slain",
    "slide": "slid",
    "sling": "slung",
    "string": "strung",
    "strive": "strove striven",
    "swell": "swelled swollen",
    "tread": "trod trodden",
    "weave": "wove woven",
    "wring": "wrung",
}

_IRREGULAR_NOUNS = {
    "children": ("child",),
    "men": ("man",),
    "women": ("woman",),
    "people": ("person",),
    "feet": ("foot",),
    "teeth": ("tooth",),
    "geese": ("goose",),
    "mice": ("mouse",),
    "oxen": ("ox",),
    "dice": ("die",),
    "lives": ("life",),
    "wives": ("wife",),
    "knives": ("knife",),
    "leaves": ("leaf",),
    "halves": ("half",),
    "shelves": ("shelf",),
    "wolves": ("wolf",),
    "calves": ("calf",),
    "elves": ("elf",),
    "loaves": ("loaf",),
    "scarves": ("scarf",),
    "thieves": ("thief",),
    "selves": ("self",),
    "analyses": ("analysis",),
    "bases": ("base", "basis"),
    "crises": ("crisis",),
    "theses": ("thesis",),
    "diagnoses": ("diagnosis",),
    "parentheses": ("parenthesis",),
    "indices": ("index",),
    "matrices": ("matrix",),
    "vertices": ("vertex",),
    "axes": ("axis", "axe"),
    "criteria": ("criterion",),
    "phenomena": ("phenomenon",),
    "alumni": ("alumnus",),
    "cacti": ("cactus",),
    "fungi": ("fungus",),
    "nuclei": ("nucleus",),
    "radii": ("radius",),
    "stimuli": ("stimulus",),
    "syllabi": ("syllabus",),
    "bacteria": ("bacterium",),
    "curricula": ("curriculum",),
    "data": ("datum",),
    "media": ("medium",),
}

_IRREGULAR_ADJ = {
    "better": ("good", "well"),
    "best": ("good", "well"),
    "worse": ("bad", "ill"),
    "worst": ("bad", "ill"),
    "more": ("much", "many"),
    "most": ("much", "many"),
    "less": ("little",),
    "least": ("little",),
    "further": ("far",),
    "furthest": ("far",),
    "farther": ("far",),
    "farthest": ("far",),
    "elder": ("old",),
    "eldest": ("old",),
    "bigger": ("big",), "biggest": ("big",),
    "smaller": ("small",), "smallest": ("small",),
    "happier": ("happy",), "happiest": ("happy",),
    "easier": ("easy",), "easiest": ("easy",),
    "earlier": ("early",), "earliest": ("early",),
    "later": ("late",), "latest": ("late",),
    "longer": ("long",), "longest": ("long",),
    "older": ("old",), "oldest": ("old",),
    "younger": ("young",), "youngest": ("young",),
    "higher": ("high",), "highest": ("high",),
    "lower": ("low",), "lowest": ("low",),
    "stronger": ("strong",), "strongest": ("strong",),
    "faster": ("fast",), "fastest": ("fast",),
    "slower": ("slow",), "slowest": ("slow",),
    "taller": ("tall",), "tallest": ("tall",),
    "shorter": ("short",), "shortest": ("short",),
    "hotter": ("hot",), "hottest": ("hot",),
    "colder": ("cold",), "coldest": ("cold",),
    "warmer": ("warm",), "warmest": ("warm",),
    "thinner": ("thin",), "thinnest": ("thin",),
    "fatter": ("fat",), "fattest": ("fat",),
    "sadder": ("sad",), "saddest": ("sad",),
    "nicer": ("nice",), "nicest": ("nice",),
    "fewer": ("few",), "fewest": ("few",),
}

_CONTRACTIONS = {
    "won't": ("will",),
    "can't": ("can",),
    "cannot": ("can",),
    "shan't": ("shall",),
    "ain't": ("be", "have"),
    "i'm": ("i", "be"),
    "i've": ("i", "have"),
    "i'll": ("i", "will"),
    "i'd": ("i", "would", "had"),
}

IRREGULAR_FORMS: dict[str, list[str]] = {}


def _register_irregular(form: str, *lemmas: str) -> None:
    bucket = IRREGULAR_FORMS.setdefault(form, [])
    for lemma in lemmas:
        if lemma and lemma not in bucket:
            bucket.append(lemma)


for _base, _forms in _IRREGULAR_VERBS.items():
    for _form in _forms.split():
        _register_irregular(_form, _base)
for _form, _lemmas in _IRREGULAR_NOUNS.items():
    _register_irregular(_form, *_lemmas)
for _form, _lemmas in _IRREGULAR_ADJ.items():
    _register_irregular(_form, *_lemmas)

NON_PLURAL_S = {"news", "means", "series", "species", "clothes"}
NON_VERB_ING = {"morning", "evening"}


def _regular_candidates(word: str) -> list[str]:
    """规则复数、第三人称单数、过去式和现在分词。"""
    out: list[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate != word and candidate not in out:
            out.append(candidate)

    if len(word) > 3 and word.endswith("s") and word not in NON_PLURAL_S:
        if word.endswith("sses"):
            add(word[:-2])
            add(word[:-1])
        elif word.endswith("ies"):
            add(word[:-1])
            add(word[:-3] + "y")
        elif word.endswith("oes"):
            add(word[:-1])
            add(word[:-2])
        elif word.endswith("ses"):
            add(word[:-1])
            add(word[:-2])
        elif word.endswith(("xes", "zes", "ches", "shes")):
            add(word[:-2])
            add(word[:-1])
        elif not word.endswith(("ss", "us", "is", "ous", "ics")):
            add(word[:-1])

    if len(word) >= 4 and word.endswith("ied"):
        add(word[:-1])
        add(word[:-3] + "y")
    elif len(word) >= 5 and word.endswith("ed"):
        base = word[:-2]
        add(base + "e")
        add(base)
        if len(base) >= 3 and base[-1] == base[-2]:
            add(base[:-1])

    if len(word) >= 5 and word.endswith("ing") and word not in NON_VERB_ING:
        if word.endswith("ying"):
            add(word[:-4] + "ie")
        base = word[:-3]
        add(base + "e")
        add(base)
        if len(base) >= 3 and base[-1] == base[-2]:
            add(base[:-1])

    return out


def _contraction_candidates(word: str) -> list[str]:
    out: list[str] = []
    if word in _CONTRACTIONS:
        return list(_CONTRACTIONS[word])

    if word.endswith("n't"):
        base = word[:-3]
        if base == "wo":
            out.append("will")
        elif base == "ca":
            out.append("can")
        elif base == "sha":
            out.append("shall")
        elif base == "ai":
            out.extend(("be", "have"))
        else:
            out.append(base)
            out.extend(IRREGULAR_FORMS.get(base, ()))
        return out

    if word.endswith("'s"):
        out.append(word[:-2])
    elif word.endswith("'re"):
        out.append(word[:-3])
    elif word.endswith("'ve"):
        out.append(word[:-3])
    elif word.endswith("'ll"):
        out.append(word[:-3])
    elif word.endswith("'d"):
        out.extend((word[:-2], "would", "had"))
    elif word.endswith("'m"):
        out.extend((word[:-2], "be"))
    return out


def lemma_candidates(word: str) -> list[str]:
    """按置信度返回可能的词元；多词短语和连字符词不做还原。"""
    if not word or " " in word or "-" in word:
        return []

    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.lower().strip()
        if (
            candidate
            and candidate != word
            and re.fullmatch(r"[a-z]+", candidate)
            and candidate not in out
        ):
            out.append(candidate)

    for candidate in IRREGULAR_FORMS.get(word, ()):
        add(candidate)
    for candidate in _contraction_candidates(word):
        add(candidate)
    if "'" not in word:
        for candidate in _regular_candidates(word):
            add(candidate)
    return out


def _list_hit(word: str, target: set[str]) -> tuple[bool, str | None]:
    """返回 (是否命中, 命中所用的词元/None 表示原形直接命中)。"""
    if word in target:
        return True, None
    for lemma in lemma_candidates(word):
        if lemma in target:
            return True, lemma
    return False, None


def _core_hit(
    word: str,
    level_map: dict[str, set[str]],
    targets: set[str],
) -> tuple[set[str], str | None]:
    """返回 (命中的目标频级, 命中所用的词元/None 表示原形直接命中)。"""
    exact_levels = level_map.get(word, set()) & targets
    if exact_levels:
        return exact_levels, None
    for lemma in lemma_candidates(word):
        levels = level_map.get(lemma, set()) & targets
        if levels:
            return levels, lemma
    return set(), None


# ---------------------------------------------------------------------------
# 1600 / 3500 词表解析
# ---------------------------------------------------------------------------

def _fallback_prefix(line: str) -> str | None:
    """处理少数没有 ``[`` 的正文行（如 ``am/æm/``、``Oceania n.``）。"""
    match = re.match(r"^([A-Za-z][A-Za-z'\-]*)\s*/", line)
    if match and re.search(r"/\s*(?:n|v|a|ad|adv|prep|pron|conj|vt|vi|int)\.", line):
        return match.group(1)
    match = re.match(r"^([A-Za-z][A-Za-z'\-]*)\s*[ˈˌ]", line)
    if match:
        return match.group(1)
    match = re.match(
        r"^([A-Za-z][A-Za-z'\-]*)\s+(?:n|v|a|ad|adv|prep|pron|conj|vt|vi|int|num|art)\.",
        line,
    )
    if match:
        return match.group(1)
    return None


def _add_forms(expr: str, out: list[str]) -> list[str]:
    """把 ``grey / gray``、``bike = bicycle`` 一类的写法拆成独立词形。"""
    for chunk in re.split(r"[=/]", expr):
        chunk = re.sub(r"\s+", " ", chunk.strip(" .,;:"))
        if chunk and re.fullmatch(r"[A-Za-z][A-Za-z'\- ]*", chunk):
            out.append(chunk.lower())
    return out


def _prefix_variants(prefix: str) -> list[str]:
    """从音标前的词头提取单词；支持 ``a (an)``、``afterward(s)`` 等变形。"""
    prefix = re.sub(r"\s+", " ", prefix.strip())
    if not prefix:
        return []
    match = re.match(r"^(.*?)\s*[\(（]([^)）]*)[\)）]", prefix)
    if match:
        base, inside = match.group(1).strip(), match.group(2).strip()
    else:
        base, inside = prefix, ""

    # 个别行在音标前多印了词性，如 “really v [..]”“must modal [..]”
    tokens = base.split()
    if len(tokens) > 1 and tokens[-1].lower().strip(".,") in TRAILING_POS:
        base = " ".join(tokens[:-1])

    forms: list[str] = []
    _add_forms(base, forms)

    if re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", inside) and len(inside) >= 2:
        forms.append(inside.lower())
    elif inside == "s":
        for form in list(forms):
            if " " not in form:
                forms.append(form + "s")
    return forms


def parse_vocab(path: Path) -> set[str]:
    """提取 1600/3500 词表每行音标 ``[`` 之前的词汇。"""
    words: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "[" in line:
            prefix = line.split("[", 1)[0].strip()
        else:
            if re.fullmatch(r"[A-Z]", line):
                continue  # 目录行 A / B / ...
            prefix = _fallback_prefix(line)
            if not prefix:
                continue
        words.update(_prefix_variants(prefix))
    return words


# ---------------------------------------------------------------------------
# 提示与标签
# ---------------------------------------------------------------------------

HINT_STRIP_RE = re.compile(
    r"\s*<div\b[^>]*>\s*(?:"
    r"Longman Core\s*\([^)]*\)"
    r"|LongMan 3000 W1（必须掌握听说读写）"
    r"|中小学1600词（必须掌握听说读写）"
    r")\s*</div>",
    re.I | re.S,
)
GRAY_HINT_RE = re.compile(
    r'<div style="color: #888888; font-size: 0\.9em; margin-bottom: 4px;">'
    r"Longman Core \([^)]*\)</div>"
)
RED_HINT_RE = re.compile(re.escape(RED_HINT))


def _gray_hint(levels: set[str]) -> str:
    ordered = [level for level in LEVEL_ORDER if level in levels]
    return GRAY_HINT_TEMPLATE.format(levels=", ".join(ordered))


def _strip_injected_hints(back: str) -> str:
    """清理顶部旧的 Longman/1600 提示块（含上一版 LongMan 3000 W1 红字）。"""
    pos = 0
    while True:
        match = HINT_STRIP_RE.match(back, pos)
        if not match:
            break
        pos = match.end()
    return back[pos:]


def _hint_flags(back: str) -> tuple[bool, bool]:
    """检查顶部是否依次为灰字、红字提示。"""
    gray = red = False
    rest = back
    match = GRAY_HINT_RE.match(rest)
    if match:
        gray = True
        rest = rest[match.end():]
    match = RED_HINT_RE.match(rest)
    if match:
        red = True
    return gray, red


def _clean_front(field: str) -> str:
    text = re.sub(r"<[^>]+>", "", field or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(string.punctuation + "，。！？；：、（）【】“”‘’ ")
    return text.lower()


def _merge_tags(existing: str, additions: list[str]) -> str:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in existing.split():
        if tag in MANAGED_TAGS or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    for tag in additions:
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return f" {' '.join(tags)} " if tags else ""


# ---------------------------------------------------------------------------
# Anki 数据库与打包
# ---------------------------------------------------------------------------

def _unicase(left: str, right: str) -> int:
    """Anki 数据库索引使用自定义的 unicase 排序规则。"""
    left, right = left.casefold(), right.casefold()
    return (left > right) - (left < right)


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.create_collation("unicase", _unicase)
    return con


def _open_collection(pkg_dir: Path, work_dir: Path) -> tuple[sqlite3.Connection, str, Path, Path]:
    """返回 (连接, 源类型, 可写数据库路径, 打包时要覆盖的目标路径)。"""
    a21b = pkg_dir / "collection.anki21b"
    if a21b.exists():
        blob = a21b.read_bytes()
        db_path = work_dir / "collection.working.sqlite"
        if blob[:4] == b"\x28\xb5\x2f\xfd":
            with a21b.open("rb") as src, db_path.open("wb") as dst:
                zstandard.ZstdDecompressor().copy_stream(src, dst)
        else:
            db_path.write_bytes(blob)
        if db_path.read_bytes()[:16] != b"SQLite format 3\x00":
            raise RuntimeError("collection.anki21b 解压后不是 SQLite 数据库")
        return _connect(db_path), "anki21b", db_path, a21b

    for name in ("collection.anki21", "collection.anki2"):
        candidate = pkg_dir / name
        if candidate.exists():
            return _connect(candidate), name, candidate, candidate
    raise FileNotFoundError("colpkg 中没有 collection.anki21b / anki21 / anki2")


def update_notes(
    con: sqlite3.Connection,
    level_map: dict[str, set[str]],
    targets: set[str],
    zk16k: set[str],
    gk35k: set[str],
) -> dict[str, object]:
    rows = con.execute("SELECT id, flds, tags FROM notes").fetchall()
    now = int(time.time())
    updates: list[tuple[str, str, int, int, int]] = []

    gray_added = red_added = both = 0
    lemma_notes = 0
    lemma_words: set[str] = set()

    for note_id, flds, tags in rows:
        fields = flds.split(FIELD_SEP)
        front = _clean_front(fields[0])

        core_levels, core_lemma = _core_hit(front, level_map, targets)
        core_hit = bool(core_levels)
        zk_hit, zk_lemma = _list_hit(front, zk16k)
        gk_hit, gk_lemma = _list_hit(front, gk35k)

        if core_lemma or zk_lemma or gk_lemma:
            lemma_words.add(front)
            lemma_notes += 1

        new_flds = flds
        if len(fields) > 1:
            cleaned_back = _strip_injected_hints(fields[1])
            hint_prefix = ""
            if core_hit:
                hint_prefix += _gray_hint(core_levels)
                gray_added += 1
            if zk_hit:
                hint_prefix += RED_HINT
                red_added += 1
            if core_hit and zk_hit:
                both += 1
            fields[1] = hint_prefix + cleaned_back
            new_flds = FIELD_SEP.join(fields)

        additions: list[str] = []
        if core_hit:
            additions.append(CORE_TAG)
        if zk_hit:
            additions.append(ZK_TAG)
        if gk_hit:
            additions.append(GK_TAG)
        new_tags = _merge_tags(tags, additions)

        if new_tags != tags or new_flds != flds:
            updates.append((new_tags, new_flds, now, -1, note_id))

    if updates:
        con.executemany(
            "UPDATE notes SET tags = ?, flds = ?, mod = ?, usn = ? WHERE id = ?",
            updates,
        )
        con.commit()

    return {
        "updated": len(updates),
        "gray_added": gray_added,
        "red_added": red_added,
        "both": both,
        "lemma_words": len(lemma_words),
        "lemma_notes": lemma_notes,
    }


def _pack(pkg_dir: Path, out_path: Path, source_kind: str, db_path: Path, target: Path) -> None:
    if source_kind == "anki21b":
        data = db_path.read_bytes()
        target.write_bytes(zstandard.ZstdCompressor(level=3).compress(data))
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(pkg_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(pkg_dir).as_posix())


def _verify_package(out_path: Path, work_dir: Path, expected: dict[str, int]) -> dict[str, int]:
    """重新读取输出包，校验数据库、标签和提示。"""
    verify_dir = work_dir / "verify"
    verify_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(out_path) as archive:
        names = set(archive.namelist())
        if "collection.anki21b" in names:
            blob = archive.read("collection.anki21b")
            if blob[:4] != b"\x28\xb5\x2f\xfd":
                raise RuntimeError("输出中的 collection.anki21b 不是 zstd 数据")
            data = zstandard.ZstdDecompressor().decompress(blob, max_output_size=512 * 1024 * 1024)
            db_path = verify_dir / "verify.sqlite"
            db_path.write_bytes(data)
        else:
            member = "collection.anki21" if "collection.anki21" in names else "collection.anki2"
            db_path = verify_dir / "verify.sqlite"
            db_path.write_bytes(archive.read(member))

    con = _connect(db_path)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"输出数据库完整性检查失败: {integrity}")
        notes = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        cards = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        tag_counts = {
            tag: con.execute(
                "SELECT COUNT(*) FROM notes WHERE tags LIKE ?", (f"% {tag} %",)
            ).fetchone()[0]
            for tag in (CORE_TAG, ZK_TAG, GK_TAG, OLD_CORE_TAG)
        }
        gray_hints = red_hints = both_hints = old_marker = duplicate_hints = 0
        for (flds,) in con.execute("SELECT flds FROM notes"):
            fields = flds.split(FIELD_SEP)
            if len(fields) < 2:
                continue
            back = fields[1]
            gray, red = _hint_flags(back)
            gray_hints += int(gray)
            red_hints += int(red)
            both_hints += int(gray and red)
            if OLD_HINT_MARK in back:
                old_marker += 1
            if back.count("Longman Core (") > 1 or back.count("中小学1600词") > 1:
                duplicate_hints += 1
    finally:
        con.close()

    actual = {
        "notes": notes,
        "cards": cards,
        "tag_lm3k_core": tag_counts[CORE_TAG],
        "tag_zk16k": tag_counts[ZK_TAG],
        "tag_gk35k": tag_counts[GK_TAG],
        "old_core_tag": tag_counts[OLD_CORE_TAG],
        "gray_hints": gray_hints,
        "red_hints": red_hints,
        "both_hints": both_hints,
        "old_marker": old_marker,
        "duplicate_hints": duplicate_hints,
    }
    for key, value in expected.items():
        if actual.get(key) != value:
            raise RuntimeError(f"输出校验失败 {key}: 期望 {value}, 实际 {actual.get(key)}")
    return actual


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deck", type=Path, default=base / "2026-10-27.colpkg")
    parser.add_argument("--longman", type=Path, default=base / "LONGMAN3000.txt")
    parser.add_argument("--zk16k", type=Path, default=base / "1600.txt")
    parser.add_argument("--gk35k", type=Path, default=base / "3500.txt")
    parser.add_argument("--out", type=Path, default=base / "2026-10-27_tagged.colpkg")
    args = parser.parse_args()

    for path in (args.deck, args.longman, args.zk16k, args.gk35k):
        if not path.exists():
            parser.error(f"文件不存在: {path}")
    if args.out.resolve() == args.deck.resolve():
        parser.error("输出文件不能覆盖原始 colpkg")

    targets = {level.upper() for level in LONGMAN_CORE_TARGETS}
    invalid = targets - set(LEVEL_ORDER)
    if invalid:
        parser.error(f"LONGMAN_CORE_TARGETS 含无效频级: {sorted(invalid)}")

    level_map = parse_longman_levels(args.longman)
    zk16k = parse_vocab(args.zk16k)
    gk35k = parse_vocab(args.gk35k)
    core_word_count = sum(1 for levels in level_map.values() if levels & targets)

    tmp = Path(tempfile.mkdtemp(prefix="anki_tag_"))
    pkg_dir = tmp / "pkg"
    work_dir = tmp / "work"
    pkg_dir.mkdir()
    work_dir.mkdir()
    con = None
    try:
        with zipfile.ZipFile(args.deck) as archive:
            archive.extractall(pkg_dir)

        con, source_kind, db_path, target = _open_collection(pkg_dir, work_dir)
        notes_total = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        cards_total = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        result = update_notes(con, level_map, targets, zk16k, gk35k)
        tag_counts = {
            tag: con.execute(
                "SELECT COUNT(*) FROM notes WHERE tags LIKE ?", (f"% {tag} %",)
            ).fetchone()[0]
            for tag in (CORE_TAG, ZK_TAG, GK_TAG, OLD_CORE_TAG)
        }
        con.close()
        con = None

        _pack(pkg_dir, args.out, source_kind, db_path, target)

        expected = {
            "notes": notes_total,
            "cards": cards_total,
            "tag_lm3k_core": tag_counts[CORE_TAG],
            "tag_zk16k": tag_counts[ZK_TAG],
            "tag_gk35k": tag_counts[GK_TAG],
            "old_core_tag": tag_counts[OLD_CORE_TAG],
            "gray_hints": int(result["gray_added"]),
            "red_hints": int(result["red_added"]),
            "both_hints": int(result["both"]),
            "old_marker": 0,
            "duplicate_hints": 0,
        }
        actual = _verify_package(args.out, work_dir, expected)

        print("===== 处理完成 =====")
        print(f"Longman Core 目标频级: {', '.join(sorted(targets, key=LEVEL_ORDER.index))}")
        print(f"Longman Core 词条数: {core_word_count}")
        print(f"词形还原命中的单词数: {result['lemma_words']}（对应 {result['lemma_notes']} 张卡片）")
        print(f"命中 zk16k 并添加红字提示的卡片数: {actual['red_hints']}")
        print(f"命中 lm3k_core 并添加灰字提示的卡片数: {actual['gray_hints']}")
        print(f"两者同时命中的卡片数: {actual['both_hints']}")
        print(f"lm3k_core 标签卡片数: {actual['tag_lm3k_core']}")
        print(f"zk16k 标签卡片数: {actual['tag_zk16k']}")
        print(f"gk35k 标签卡片数: {actual['tag_gk35k']}")
        print(f"旧 lm3kw1 标签残留: {actual['old_core_tag']}")
        print(f"旧 LongMan 3000 W1 提示残留: {actual['old_marker']}")
        print(f"Anki 笔记总数: {actual['notes']}")
        print(f"Anki 卡片总数: {actual['cards']}")
        print(f"更新笔记数: {result['updated']}")
        print(f"输出文件: {args.out}")
        print(f"原始文件已保留: {args.deck}")
        print("输出包校验通过（SQLite integrity_check / 标签 / 提示计数一致）。")
        return 0
    finally:
        if con is not None:
            con.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"错误: {exc}", file=sys.stderr)
        raise
