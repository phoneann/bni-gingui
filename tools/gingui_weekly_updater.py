#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BNI 金貴分會官網（ginguitn.com）每週例會講者自動更新、來源讀取與真偽驗收工具。

修復與防護重點：
1. 杜絕假成功：未真正完成下一週（target_friday）主講者更新時，`verify-status --require-upcoming-speaker`
   必定回傳非 0（exit code 2），禁止排程標記為 `verified_ok`。
2. Facebook 粉專來源讀取：支援讀取官方粉專與 Reels 頁面、檢查已開啟之 Chrome 分頁（唯讀，不索取密碼、
   不繞過驗證、不讀 Cookie）及讀取本機已驗證之來源回執 `content/speaker-source-YYYYMMDD.json`。
   遇到登入牆或未公告時，明確標示「受阻」（`blocked_login_required` / `no_upcoming_announcement`）。
3. 嚴格遵守 `UPDATE_WORKFLOW.md`：
   - 有已確認下一場來源時：同步更新 `docs/index.html`、`docs/videos.html`、`docs/join.html`、
     `docs/assets/photos/focus-YYYYMMDD.svg` 與 `docs/sitemap.xml`。
   - 場次開始後且下一場尚未公告／受阻時（`--close-expired`）：停止接受已過期場次報名，
     `videos.html` 改標「場次已結束」，`index.html` 關閉過期場次報名鈕並保留影音入口與引導接待窗口安排，
     `join.html` 停用過期場次選項並標示「（已結束報名）」，絕不憑空臆測下一場日期或講者。
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

TPE = datetime.timezone(datetime.timedelta(hours=8))

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
CONTENT_DIR = REPO_ROOT / "content"
TESTS_DIR = REPO_ROOT / "tests"

FANPAGE_ID = "100025054496051"
FANPAGE_URL = f"https://www.facebook.com/profile.php?id={FANPAGE_ID}"
FANPAGE_REELS_URL = f"https://www.facebook.com/profile.php?id={FANPAGE_ID}&sk=reels_tab"

REQUIRED_SOURCE_FIELDS = [
    "meeting_date",
    "speaker_name",
    "profession",
    "company",
    "topic_headline",
    "topic_svg_line1",
    "topic_svg_line2",
    "topic_paragraphs",
    "partners",
    "ideal_referral",
    "dream_referral",
    "reel_url",
    "source_post_url",
    "fetched_at",
]


def now_tpe(override_iso: str | None = None) -> datetime.datetime:
    if override_iso:
        dt = datetime.datetime.fromisoformat(override_iso)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=TPE)
        return dt.astimezone(TPE)
    return datetime.datetime.now(TPE)


def compute_target_friday(ref_dt: datetime.datetime) -> datetime.date:
    """Calculate the upcoming Friday meeting date to be displayed/updated.

    - Weekly meeting starts every Friday at 06:30 Asia/Taipei.
    - Before Friday 06:30 -> that same Friday.
    - On Friday at/after 06:30 (including Friday 12:00 schedule) -> next Friday (+7 days).
    - Saturday..Thursday -> the coming Friday.
    """
    ref_tpe = ref_dt.astimezone(TPE)
    wd = ref_tpe.weekday()  # Mon=0 .. Fri=4 .. Sun=6
    if wd == 4:
        cutoff = ref_tpe.replace(hour=6, minute=30, second=0, microsecond=0)
        if ref_tpe < cutoff:
            return ref_tpe.date()
        return ref_tpe.date() + datetime.timedelta(days=7)
    days_until_friday = (4 - wd) % 7
    return ref_tpe.date() + datetime.timedelta(days=days_until_friday)


def format_month_day(d: datetime.date) -> str:
    return f"{d.month}/{d.day}"


def format_month_day_padded(d: datetime.date) -> str:
    return f"{d.month:02d} / {d.day:02d}"


def extract_reel_id(reel_url: str) -> str:
    m = re.search(r"/reel/(\d+)", reel_url)
    if not m:
        raise ValueError(f"無法從 Reel 網址解析影片 ID: {reel_url}")
    return m.group(1)


def validate_source_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_SOURCE_FIELDS:
        val = payload.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"缺少必要欄位: {field}")
    meeting_date_str = str(payload.get("meeting_date", "")).strip()
    try:
        d = datetime.date.fromisoformat(meeting_date_str)
        if d.weekday() != 4:
            errors.append(f"meeting_date ({meeting_date_str}) 不是星期五")
    except ValueError:
        errors.append(f"meeting_date 格式錯誤 (需為 YYYY-MM-DD): {meeting_date_str}")

    reel_url = str(payload.get("reel_url", "")).strip()
    if reel_url and not re.search(r"^https://www\.facebook\.com/reel/\d+/?$", reel_url):
        errors.append(f"reel_url 格式不符官方 Facebook Reel 永久連結: {reel_url}")

    source_post_url = str(payload.get("source_post_url", "")).strip()
    if source_post_url and not source_post_url.startswith("https://www.facebook.com/"):
        errors.append(f"source_post_url 必須為 Facebook 永久連結: {source_post_url}")

    paragraphs = payload.get("topic_paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs or not all(isinstance(p, str) and p.strip() for p in paragraphs):
        errors.append("topic_paragraphs 必須為非空字串陣列")

    return errors


def inspect_open_chrome_fanpage() -> dict[str, Any]:
    """Inspect logged-in Chrome for the Gingui Facebook fanpage and Reels tab.

    Strictly adheres to safety rules:
    - Never reads or exports cookies, storage, passwords, or credentials.
    - Uses AppleScript to inspect existing Facebook tabs or open the public Gingui fanpage URL
      in the already-logged-in Chrome window, and attempts DOM text/link extraction.
    """
    js_expand_and_extract = (
        "(function(){"
        "Array.from(document.querySelectorAll('div[role=\"button\"]')).forEach(function(el){"
        "  var t = (el.innerText || '').trim();"
        "  if (t === '查看更多' || t === 'See more') { try { el.click(); } catch(e){} }"
        "});"
        "window.scrollBy(0, 1200);"
        "var txt = document.body ? document.body.innerText.slice(0, 25000) : '';"
        "var anchors = Array.from(document.querySelectorAll('a[href]')).map(function(a){"
        "  return {href: a.href, text: (a.innerText || a.getAttribute('aria-label') || '').slice(0, 250)};"
        "}).filter(function(x){"
        "  return x.href.indexOf('notif_id=') === -1 && x.href.indexOf('notif_t=') === -1 && "
        "         (x.href.indexOf('/reel/') !== -1 || x.href.indexOf('/posts/') !== -1 || "
        "          x.href.indexOf('story_fbid') !== -1 || x.href.indexOf('pfbid') !== -1 || "
        "          x.href.indexOf('/videos/') !== -1);"
        "}).slice(0, 80);"
        "return JSON.stringify({url: location.href, title: document.title, text: txt, links: anchors});"
        "})()"
    )
    js_extract_escaped = js_expand_and_extract.replace("\\", "\\\\").replace('"', '\\"')

    script = f'''
    tell application "System Events"
        if not (exists process "Google Chrome") then
            return "CHROME_NOT_RUNNING"
        end if
    end tell
    tell application "Google Chrome"
        set fbTabsInfo to ""
        set targetTab to missing value
        repeat with w in windows
            repeat with t in tabs of w
                set u to URL of t
                set ttl to title of t
                if u contains "facebook.com" then
                    set fbTabsInfo to fbTabsInfo & u & " [[TITLE]] " & ttl & "\\n"
                end if
                if u contains "100025054496051" or ttl contains "金貴" then
                    set targetTab to t
                end if
            end repeat
        end repeat

        set openedNewTab to false
        if targetTab is missing value then
            if (count of windows) is 0 then
                make new window
            end if
            set targetTab to make new tab at end of tabs of front window with properties {{URL:"{FANPAGE_URL}"}}
            set openedNewTab to true
            delay 5
        else
            if (URL of targetTab) contains "sk=reels_tab" then
                set URL of targetTab to "{FANPAGE_URL}"
                delay 4
            end if
        end if

        set finalUrl to URL of targetTab
        set finalTitle to title of targetTab
        set jsResult to ""
        set jsError to ""
        try
            set jsResult to execute targetTab javascript "{js_extract_escaped}"
            delay 2
            set jsResult to execute targetTab javascript "{js_extract_escaped}"
        on error errMsg number errNum
            set jsError to (errNum as string) & ": " & errMsg
        end try

        set reelsJsResult to ""
        if jsError is "" then
            try
                set URL of targetTab to "{FANPAGE_REELS_URL}"
                delay 4
                set reelsJsResult to execute targetTab javascript "{js_extract_escaped}"
            on error errMsg2
                set reelsJsResult to "REELS_ERR: " & errMsg2
            end try
        end if

        return "RESULT||" & (openedNewTab as string) & "||" & finalUrl & "||" & finalTitle & "||" & jsError & "||" & fbTabsInfo & "||" & jsResult & "||" & reelsJsResult
    end tell
    '''
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=35,
        )
        out = (res.stdout or "").strip()
        if out.startswith("RESULT||"):
            parts = out.split("||", 7)
            opened_new = parts[1] == "true" if len(parts) > 1 else False
            final_url = parts[2] if len(parts) > 2 else ""
            final_title = parts[3] if len(parts) > 3 else ""
            js_error = parts[4] if len(parts) > 4 else ""
            fb_tabs_raw = parts[5] if len(parts) > 5 else ""
            js_payload_raw = parts[6] if len(parts) > 6 else ""
            reels_payload_raw = parts[7] if len(parts) > 7 else ""

            parsed_main = {}
            if js_payload_raw.startswith("{"):
                try:
                    parsed_main = json.loads(js_payload_raw)
                except Exception:
                    parsed_main = {"raw": js_payload_raw[:2000]}

            parsed_reels = {}
            if reels_payload_raw.startswith("{"):
                try:
                    parsed_reels = json.loads(reels_payload_raw)
                except Exception:
                    parsed_reels = {"raw": reels_payload_raw[:2000]}

            info = {
                "checked": True,
                "found_tab": True,
                "opened_new_tab": opened_new,
                "url": final_url,
                "title": final_title,
                "js_error": js_error,
                "existing_fb_tabs": [line for line in fb_tabs_raw.splitlines() if line.strip()],
                "main_page": parsed_main,
                "reels_page": parsed_reels,
                "body_excerpt": parsed_main.get("text", "") if isinstance(parsed_main, dict) else "",
            }
            try:
                CONTENT_DIR.mkdir(parents=True, exist_ok=True)
                (CONTENT_DIR / "chrome-fanpage-inspection.json").write_text(
                    json.dumps(info, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            return info
        return {"checked": True, "found_tab": False, "detail": out or res.stderr.strip()}
    except Exception as exc:
        return {"checked": False, "found_tab": False, "error": str(exc)}


def fetch_http_url(url: str, timeout: int = 12) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            raw_bytes = resp.read(250_000)
            body = raw_bytes.decode("utf-8", errors="ignore")
            title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
            title = html.unescape(title_m.group(1).strip()) if title_m else ""
            login_wall = (
                "/login" in final_url
                or "login.php" in final_url
                or "login_form" in body
                or "Log Into Facebook" in body
                or "登入 Facebook" in body
                or ("100025054496051" in url and "主講" not in body and "例會" not in body)
            )
            return {
                "ok": True,
                "requested_url": url,
                "final_url": final_url,
                "status_code": resp.status,
                "title": title,
                "login_wall_detected": login_wall,
                "body_length": len(body),
                "body_sample": body[:600],
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "requested_url": url,
            "status_code": exc.code,
            "error_type": "http_error",
            "error": str(exc),
            "login_wall_detected": exc.code in (400, 401, 403, 302),
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "requested_url": url,
            "error_type": "network_error",
            "error": str(exc.reason),
            "login_wall_detected": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "requested_url": url,
            "error_type": "unexpected_error",
            "error": str(exc),
            "login_wall_detected": False,
        }


def parse_speaker_from_chrome_info(
    chrome_info: dict[str, Any],
    target_friday: datetime.date,
    ref_dt: datetime.datetime,
) -> dict[str, Any] | None:
    """Parse a complete speaker announcement for target_friday from Chrome DOM text + links."""
    text = str(chrome_info.get("body_excerpt") or "")
    if not text:
        main_page = chrome_info.get("main_page") or {}
        if isinstance(main_page, dict):
            text = str(main_page.get("text") or "")

    date_token = f"日期｜{target_friday.month}月{target_friday.day}日"
    if date_token not in text and target_friday.isoformat() not in text:
        return None

    # Isolate the post segment containing the target date
    idx = text.find(date_token)
    if idx == -1:
        idx = text.find(target_friday.isoformat())
    segment = text[max(0, idx - 600) : min(len(text), idx + 900)]

    title_m = re.search(r"【([^】\n]{4,40})】", segment)
    speaker_m = re.search(r"主講(?:人|者)[｜|：:\s]*([^\s\n｜|]+)", segment)
    prof_m = re.search(r"專業領域[｜|：:\s]*([^\s\n｜|]+)", segment)
    comp_m = re.search(r"公司[｜|：:\s]*([^\s\n｜|]+)", segment)

    if not (title_m and speaker_m and prof_m and comp_m):
        return None

    headline = title_m.group(1).strip()
    speaker = speaker_m.group(1).strip()
    prof_raw = prof_m.group(1).strip()
    profession = prof_raw if prof_raw.endswith("代表") else f"{prof_raw}代表"
    company = comp_m.group(1).strip()

    if "，" in headline:
        parts = [p.strip() for p in headline.split("，", 1)]
        svg_line1, svg_line2 = parts[0], parts[1]
    elif "——" in headline:
        parts = [p.strip() for p in headline.split("——", 1)]
        svg_line1, svg_line2 = parts[0], parts[1]
    else:
        mid = max(1, len(headline) // 2)
        svg_line1, svg_line2 = headline[:mid], headline[mid:]

    def extract_bullets(section_header: str, next_header: str) -> str:
        s_idx = segment.find(section_header)
        if s_idx == -1:
            return ""
        e_idx = segment.find(next_header, s_idx) if next_header else len(segment)
        sub = segment[s_idx : (e_idx if e_idx != -1 else len(segment))]
        items = [m.group(1).strip() for m in re.finditer(r"🔹\s*([^\n]+)", sub)]
        return "、".join(items)

    partners = extract_bullets("合作夥伴招募", "理想引薦對象")
    ideal_ref = extract_bullets("理想引薦對象", "夢幻引薦對象")
    dream_ref = extract_bullets("夢幻引薦對象", "報名方式")

    # Extract summary sentence from official intro paragraph if present
    intro_m = re.search(
        rf"邀請到\s*{re.escape(speaker)}[^，,\n]*[，,]\s*\n*([^\n]+(?:\n[^\n\-━]+)*)",
        segment,
    )
    md_short = format_month_day(target_friday)
    if intro_m:
        intro_clean = re.sub(r"\s+", "", intro_m.group(1)).rstrip("！!。")
        paragraph = f"這支影片為 {md_short} 主講的演講前預告。{speaker}將{intro_clean}。"
    else:
        paragraph = f"這支影片為 {md_short} 主講的演講前預告。{speaker}以「{headline}」為題進行專題分享。"

    # Match the corresponding Reel permalink from main_page.links
    reel_url = ""
    main_links = (chrome_info.get("main_page") or {}).get("links") or []
    for link_obj in main_links:
        href = str(link_obj.get("href") or "")
        ltxt = str(link_obj.get("text") or "")
        m_reel = re.match(r"^(https://www\.facebook\.com/reel/\d+/?)", href)
        if m_reel and (headline[:6] in ltxt or speaker in ltxt):
            reel_url = m_reel.group(1)
            if not reel_url.endswith("/"):
                reel_url += "/"
            break

    if not (partners and ideal_ref and dream_ref and reel_url):
        return None

    return {
        "meeting_date": target_friday.isoformat(),
        "speaker_name": speaker,
        "profession": profession,
        "company": company,
        "topic_headline": headline,
        "topic_svg_line1": svg_line1,
        "topic_svg_line2": svg_line2,
        "topic_paragraphs": [paragraph],
        "partners": partners,
        "ideal_referral": ideal_ref,
        "dream_referral": dream_ref,
        "reel_url": reel_url,
        "source_post_url": reel_url,
        "fetched_at": ref_dt.isoformat(timespec="seconds"),
    }


def cmd_fetch_source(args: argparse.Namespace) -> int:
    ref_dt = now_tpe(args.now)
    target_friday = (
        datetime.date.fromisoformat(args.target_friday)
        if args.target_friday
        else compute_target_friday(ref_dt)
    )
    compact_date = target_friday.strftime("%Y%m%d")
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    receipt_path = CONTENT_DIR / f"speaker-source-{compact_date}.json"
    latest_check_path = CONTENT_DIR / "latest-source-check.json"

    # 1. Check if a verified local source receipt already exists for target_friday
    if receipt_path.exists():
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            errs = validate_source_payload(payload)
            if not errs:
                result = {
                    "checked_at": ref_dt.isoformat(timespec="seconds"),
                    "target_friday": target_friday.isoformat(),
                    "status": "ok_verified_source",
                    "blocked": False,
                    "source_type": "local_verified_receipt",
                    "source_receipt_path": str(receipt_path.relative_to(REPO_ROOT)),
                    "speaker_name": payload["speaker_name"],
                    "meeting_date": payload["meeting_date"],
                    "source_post_url": payload["source_post_url"],
                    "reel_url": payload["reel_url"],
                    "message": f"已取得 {target_friday.isoformat()}（{payload['speaker_name']}）完整驗證來源回執。",
                }
                latest_check_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
        except Exception as exc:
            pass

    # 2. Optional read-only check of already-open Chrome fanpage tab (or cached inspection)
    chrome_info: dict[str, Any] = {"checked": False}
    if args.check_open_chrome:
        chrome_info = inspect_open_chrome_fanpage()
        if not chrome_info.get("found_tab") and (CONTENT_DIR / "chrome-fanpage-inspection.json").exists():
            try:
                cached = json.loads((CONTENT_DIR / "chrome-fanpage-inspection.json").read_text(encoding="utf-8"))
                if cached.get("found_tab") and not cached.get("js_error"):
                    chrome_info = cached
            except Exception:
                pass
        if chrome_info.get("found_tab") and chrome_info.get("body_excerpt"):
            parsed = parse_speaker_from_chrome_info(chrome_info, target_friday, ref_dt)
            if parsed and not validate_source_payload(parsed):
                receipt_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result = {
                    "checked_at": ref_dt.isoformat(timespec="seconds"),
                    "target_friday": target_friday.isoformat(),
                    "status": "ok_verified_source",
                    "blocked": False,
                    "source_type": "chrome_logged_in_fanpage",
                    "source_receipt_path": str(receipt_path.relative_to(REPO_ROOT)),
                    "speaker_name": parsed["speaker_name"],
                    "meeting_date": parsed["meeting_date"],
                    "source_post_url": parsed["source_post_url"],
                    "reel_url": parsed["reel_url"],
                    "message": (
                        f"已從本機已登入之 Chrome 金貴粉專成功讀取並驗證 {target_friday.isoformat()} "
                        f"（{parsed['speaker_name']}｜{parsed['topic_headline']}）完整主講資訊。"
                    ),
                }
                latest_check_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0

    # 3. Direct HTTP check against Facebook fanpage & reels tab
    fanpage_http = fetch_http_url(FANPAGE_URL)
    reels_http = fetch_http_url(FANPAGE_REELS_URL)

    network_blocked = (
        not fanpage_http.get("ok")
        and fanpage_http.get("error_type") == "network_error"
        and not reels_http.get("ok")
    )
    login_blocked = bool(
        fanpage_http.get("login_wall_detected") or reels_http.get("login_wall_detected")
    )

    js_err_str = str(chrome_info.get("js_error") or "").strip()

    if js_err_str:
        status = "blocked_chrome_js_disabled"
        msg = (
            f"受阻：已連線至本機已登入 Facebook 之 Chrome 分頁（{chrome_info.get('url') or FANPAGE_URL}），"
            f"但 Chrome 回傳「{js_err_str}」，且依安全規範不得讀取 Cookie／憑證或自行擴大 Chrome 權限，"
            f"目前無法自動讀取 {target_friday.isoformat()} 主講者貼文與預告 Reel。"
        )
    elif network_blocked:
        status = "blocked_network"
        msg = (
            f"受阻：無法連線至 Facebook 粉專（{fanpage_http.get('error')}），"
            f"且本機尚無 {target_friday.isoformat()} 已驗證講者來源檔（{receipt_path.name}）。"
        )
    elif login_blocked:
        status = "blocked_login_required"
        msg = (
            f"受阻：金貴會 Facebook 粉專（id={FANPAGE_ID}）與 Reels 頁面要求登入驗證，"
            "依安全規範不得繞過驗證或索取密碼；目前無法自動讀取 "
            f"{target_friday.isoformat()} 主講者貼文與預告 Reel。"
        )
    else:
        status = "no_upcoming_announcement"
        msg = (
            f"受阻：已檢查金貴會來源，但尚未發現 {target_friday.isoformat()} "
            "完整主講者公告與預告 Reel，依規定不臆測日期或講者。"
        )

    result = {
        "checked_at": ref_dt.isoformat(timespec="seconds"),
        "target_friday": target_friday.isoformat(),
        "fanpage_url": FANPAGE_URL,
        "reels_url": FANPAGE_REELS_URL,
        "status": status,
        "blocked": True,
        "source_receipt_path": None,
        "chrome_readonly_check": {
            "checked": bool(chrome_info.get("checked")),
            "found_tab": bool(chrome_info.get("found_tab")),
            "opened_new_tab": bool(chrome_info.get("opened_new_tab")),
            "url": chrome_info.get("url") or "",
            "title": chrome_info.get("title") or "",
            "js_error": chrome_info.get("js_error") or "",
            "existing_fb_tabs": chrome_info.get("existing_fb_tabs") or [],
            "body_excerpt_preview": (chrome_info.get("body_excerpt") or "")[:1500],
            "detail": chrome_info.get("detail") or chrome_info.get("title") or "",
        },
        "http_checks": {
            "fanpage": {
                "ok": fanpage_http.get("ok"),
                "status_code": fanpage_http.get("status_code"),
                "final_url": fanpage_http.get("final_url"),
                "login_wall_detected": fanpage_http.get("login_wall_detected"),
                "error": fanpage_http.get("error"),
            },
            "reels": {
                "ok": reels_http.get("ok"),
                "status_code": reels_http.get("status_code"),
                "final_url": reels_http.get("final_url"),
                "login_wall_detected": reels_http.get("login_wall_detected"),
                "error": reels_http.get("error"),
            },
        },
        "message": msg,
    }
    latest_check_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2


def render_focus_svg(payload: dict[str, Any]) -> str:
    d = datetime.date.fromisoformat(payload["meeting_date"])
    md_short = format_month_day(d)
    md_padded = format_month_day_padded(d)
    speaker = html.escape(payload["speaker_name"])
    profession = html.escape(payload["profession"])
    company = html.escape(payload["company"])
    line1 = html.escape(payload["topic_svg_line1"])
    line2 = html.escape(payload["topic_svg_line2"])
    headline = html.escape(payload["topic_headline"])

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1076 1522" role="img" lang="zh-Hant" aria-labelledby="title desc">
  <title id="title">{md_short} {speaker}主講資訊</title>
  <desc id="desc">{headline} {d.year}年{d.month}月{d.day}日星期五上午6點30分，台南 BNI 菁英會所。</desc>
  <defs>
    <linearGradient id="paper" x2="1" y2="1">
      <stop stop-color="#f8f1e2"/>
      <stop offset="1" stop-color="#e5d0ad"/>
    </linearGradient>
    <linearGradient id="gold" x2="1" y2="1">
      <stop stop-color="#947042"/>
      <stop offset="1" stop-color="#82613a"/>
    </linearGradient>
  </defs>
  <rect width="1076" height="1522" fill="url(#paper)"/>
  <rect x="35" y="35" width="1006" height="1452" rx="16" fill="none" stroke="#a47c4c" stroke-width="2"/>
  <path d="M0 0h1076v270H0z" fill="#212a2a"/>
  <text x="78" y="116" fill="#f2e5c9" font-family="Noto Sans TC, sans-serif" font-size="32" font-weight="700" letter-spacing="8">BNI 台南市區 · 金貴分會</text>
  <text x="78" y="201" fill="#d5b888" font-family="Noto Sans TC, sans-serif" font-size="36" letter-spacing="6">FEATURED SPEAKER</text>
  <text x="78" y="387" fill="#8b2530" font-family="Noto Sans TC, sans-serif" font-size="112" font-weight="900">{md_padded}</text>
  <line x1="78" y1="435" x2="998" y2="435" stroke="#aa8454" stroke-width="3"/>
  <text x="78" y="525" fill="#3b342e" font-family="Noto Sans TC, sans-serif" font-size="43" letter-spacing="5">星期五 · 上午 06:30</text>
  <text x="78" y="682" fill="#272e2e" font-family="Noto Sans TC, sans-serif" font-size="90" font-weight="900">{speaker}</text>
  <text x="78" y="758" fill="#73583a" font-family="Noto Sans TC, sans-serif" font-size="39">{profession}</text>
  <text x="78" y="835" fill="#584733" font-family="Noto Sans TC, sans-serif" font-size="34">{company}</text>
  <rect x="68" y="909" width="940" height="264" rx="16" fill="url(#gold)"/>
  <text x="105" y="982" fill="#fff9ec" font-family="Noto Sans TC, sans-serif" font-size="32" letter-spacing="5">分享主題</text>
  <text x="105" y="1061" fill="#fff" font-family="Noto Sans TC, sans-serif" font-size="55" font-weight="700">{line1}</text>
  <text x="105" y="1134" fill="#fff" font-family="Noto Sans TC, sans-serif" font-size="42">{line2}</text>
  <text x="78" y="1270" fill="#333a39" font-family="Noto Sans TC, sans-serif" font-size="36" font-weight="700">台南 BNI 菁英會所</text>
  <text x="78" y="1332" fill="#5c625f" font-family="Noto Sans TC, sans-serif" font-size="29">台南市北區文成路 781-2 號</text>
  <text x="78" y="1414" fill="#8b2530" font-family="Noto Sans TC, sans-serif" font-size="28">官網資訊圖卡 · 場次資訊以官方公告為準</text>
</svg>
"""


def update_sitemap_lastmod(docs_dir: pathlib.Path, updated_files: dict[str, str]) -> None:
    """Update <lastmod> in docs/sitemap.xml for updated_files and sync historical dates."""
    sitemap_path = docs_dir / "sitemap.xml"
    text = sitemap_path.read_text(encoding="utf-8")

    # Known accurate historical dates from git history for pages not touched today
    historical_dates = {
        "https://ginguitn.com/": "2026-10-31",
        "https://ginguitn.com/about.html": "2026-10-06",
        "https://ginguitn.com/videos.html": "2026-10-31",
        "https://ginguitn.com/directory.html": "2026-10-10",
        "https://ginguitn.com/member.html": "2026-10-06",
        "https://ginguitn.com/member-xiefengan.html": "2026-10-05",
        "https://ginguitn.com/member-chenyixin.html": "2026-10-05",
        "https://ginguitn.com/member-huangxiaohan.html": "2026-10-05",
        "https://ginguitn.com/member-huangzhaoying.html": "2026-10-05",
        "https://ginguitn.com/member-chenlingwei.html": "2026-10-05",
        "https://ginguitn.com/faq.html": "2026-10-06",
        "https://ginguitn.com/join.html": "2026-10-31",
    }
    historical_dates.update(updated_files)

    def repl_url_block(match: re.Match[str]) -> str:
        block = match.group(0)
        loc_m = re.search(r"<loc>(.*?)</loc>", block)
        if not loc_m:
            return block
        loc = loc_m.group(1).strip()
        new_date = historical_dates.get(loc)
        if not new_date:
            return block
        return re.sub(r"<lastmod>.*?</lastmod>", f"<lastmod>{new_date}</lastmod>", block)

    new_text = re.sub(r"<url>\s*.*?\s*</url>", repl_url_block, text, flags=re.S)
    sitemap_path.write_text(new_text, encoding="utf-8")


def apply_close_expired(docs_dir: pathlib.Path, today_str: str) -> dict[str, Any]:
    """Apply UPDATE_WORKFLOW.md Rule 6 when the current session has started/ended and next speaker is not yet confirmed."""
    changes: list[str] = []

    # 1. docs/videos.html: Replace active registration links on past sessions with "場次已結束"
    videos_path = docs_dir / "videos.html"
    videos_html = videos_path.read_text(encoding="utf-8")

    def close_video_cta(m: re.Match[str]) -> str:
        md_label = m.group(2)
        return f'<span style="color:var(--gray-70)">{md_label} 場次已結束</span>'

    new_videos_html, n_vid = re.subn(
        r'<a class="btn btn-outline-dark" href="join\.html\?meeting=(\d{4}-\d{2}-\d{2})">預約 (\d{1,2}/\d{1,2}) 例會</a>',
        close_video_cta,
        videos_html,
    )
    if n_vid > 0 and new_videos_html != videos_html:
        videos_path.write_text(new_videos_html, encoding="utf-8")
        changes.append(f"docs/videos.html: 已將 {n_vid} 個過期場次報名按鈕改為「場次已結束」")

    # 2. docs/index.html: Close expired registration CTA on homepage while keeping video entry & guiding to reception
    index_path = docs_dir / "index.html"
    index_html = index_path.read_text(encoding="utf-8")
    new_index_html = index_html

    new_index_html = re.sub(
        r'<div class="week">(\d{4}-\d{2}-\d{2}（五）06:30)(?:（場次已結束｜下場講者待官方公告）)?</div>',
        r'<div class="week">\1（場次已結束｜下場講者待官方公告）</div>',
        new_index_html,
    )
    new_index_html = re.sub(
        r'<a href="join\.html\?meeting=\d{4}-\d{2}-\d{2}" class="btn btn-red"([^>]*)>預約 \d{1,2}/\d{1,2} 例會</a>',
        r'<a href="join.html" class="btn btn-red"\1>預約來訪（由接待窗口安排場次）</a>',
        new_index_html,
    )
    if new_index_html != index_html:
        index_path.write_text(new_index_html, encoding="utf-8")
        changes.append("docs/index.html: 已停止過期場次直接報名、改引導接待窗口安排並保留影音入口")

    # 3. docs/join.html: Mark expired static option as disabled + （已結束報名）, and make JS idempotent
    join_path = docs_dir / "join.html"
    join_html = join_path.read_text(encoding="utf-8")
    new_join_html = join_html

    # Update static <option> if not already disabled
    new_join_html = re.sub(
        r'<option value="(\d{4}-\d{2}-\d{2}（五）06:30｜[^"]+主講)">(\1)</option>',
        r'<option value="\1" disabled>\1（已結束報名）</option>',
        new_join_html,
    )

    old_js_block = """if (featuredMeetingClosed) {
  const featuredOption = document.querySelector('#g-date option[value^="2026-11-06"]');
  featuredOption.disabled = true;
  featuredOption.textContent += '（已結束報名）';
}"""
    new_js_block = """const featuredOption = document.querySelector('#g-date option[value^="2026-11-06"]');
if (featuredOption) {
  featuredOption.disabled = featuredMeetingClosed;
  const baseLabel = featuredOption.value;
  featuredOption.textContent = featuredMeetingClosed ? `${baseLabel}（已結束報名）` : baseLabel;
}"""
    if old_js_block in new_join_html:
        new_join_html = new_join_html.replace(old_js_block, new_js_block)

    if new_join_html != join_html:
        join_path.write_text(new_join_html, encoding="utf-8")
        changes.append("docs/join.html: 靜態 HTML 預設停用已過期場次選項並保持 JS 時間切換相容")

    # 4. docs/sitemap.xml: Update lastmod dates
    update_sitemap_lastmod(
        docs_dir,
        {
            "https://ginguitn.com/": today_str,
            "https://ginguitn.com/videos.html": today_str,
            "https://ginguitn.com/join.html": today_str,
        },
    )
    changes.append(f"docs/sitemap.xml: 已同步全站 11 頁實際 <lastmod>（首頁/影音/報名頁更新為 {today_str}）")

    return {"mode": "close_expired", "updated_at_date": today_str, "changes": changes}


def apply_speaker_update(docs_dir: pathlib.Path, payload: dict[str, Any], today_str: str) -> dict[str, Any]:
    """Apply full speaker update across SVG, index.html, videos.html, join.html, and sitemap.xml."""
    errs = validate_source_payload(payload)
    if errs:
        raise ValueError("講者來源 JSON 驗證失敗: " + "; ".join(errs))

    d = datetime.date.fromisoformat(payload["meeting_date"])
    ymd = d.isoformat()
    compact = d.strftime("%Y%m%d")
    md_short = format_month_day(d)
    speaker = payload["speaker_name"].strip()
    profession = payload["profession"].strip()
    company = payload["company"].strip()
    headline = payload["topic_headline"].strip()
    paragraphs = [p.strip() for p in payload["topic_paragraphs"] if p.strip()]
    partners = payload["partners"].strip()
    ideal_ref = payload["ideal_referral"].strip()
    dream_ref = payload["dream_referral"].strip()
    reel_url = payload["reel_url"].strip()
    if not reel_url.endswith("/"):
        reel_url += "/"

    changes: list[str] = []

    # 1. Generate SVG poster
    svg_rel = f"assets/photos/focus-{compact}.svg"
    svg_path = docs_dir / svg_rel
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(render_focus_svg(payload), encoding="utf-8")
    changes.append(f"docs/{svg_rel}: 已產生 {md_short} {speaker} 主講資訊圖卡")

    # 2. Update docs/index.html .focus-strip card
    index_path = docs_dir / "index.html"
    index_html = index_path.read_text(encoding="utf-8")
    prof_short = profession[:-2] if profession.endswith("代表") else profession
    new_focus_section = f"""<section class="focus-strip">
  <div class="wrap">
    <div class="kicker">UPCOMING FEATURED SPEAKER</div>
    <h2 style="margin-bottom:20px">近期主講</h2>
    <div class="focus-card video-focus-card">
      <div class="poster reel-frame edm-frame" id="home-reel-{compact}">
        <img src="{svg_rel}" alt="{md_short} {html.escape(speaker)}主講資訊圖卡：{html.escape(headline)}">
        <button class="reel-load" type="button" data-reel-target="home-reel-{compact}" data-facebook-url="{reel_url}" data-reel-title="{html.escape(speaker)}主講預告：{html.escape(headline)}">
          <span aria-hidden="true">▶</span> 播放主講影片
        </button>
      </div>
      <div class="body">
        <div class="week">{ymd}（五）06:30</div>
        <h3>{html.escape(headline)}</h3>
        <p class="speaker">講者：<b>{html.escape(speaker)}・{html.escape(prof_short)}</b>（{html.escape(company)}）</p>
        <p style="color:var(--gray-90);font-size:.92rem">
          合作夥伴：{html.escape(partners)}。理想引薦：{html.escape(ideal_ref)}。夢幻引薦：{html.escape(dream_ref)}。
        </p>
        <p class="video-note">點擊左側播放按鈕，會以直式完整畫面開啟影片；這支主講預告也已收錄於影音紀錄，方便回看。</p>
        <p class="video-note">左側資訊圖卡由本站依官方預告整理製作。</p>
        <div class="btns compact-btns">
          <a href="join.html?meeting={ymd}" class="btn btn-red" data-track="guest_registration_click" data-track-label="近期主講報名">預約 {md_short} 例會</a>
          <a href="videos.html" class="btn btn-outline-dark">查看影音紀錄</a>
        </div>
      </div>
    </div>
  </div>
</section>"""

    new_index_html, n_idx = re.subn(
        r'<section class="focus-strip">.*?</section>',
        new_focus_section,
        index_html,
        count=1,
        flags=re.S,
    )
    if n_idx != 1:
        raise RuntimeError("無法在 docs/index.html 定位 .focus-strip 區塊")
    index_path.write_text(new_index_html, encoding="utf-8")
    changes.append(f"docs/index.html: 已切換首頁主講人為 {ymd} {speaker}")

    # 3. Update docs/videos.html: close old active CTAs and prepend new card if not present
    videos_path = docs_dir / "videos.html"
    videos_html = videos_path.read_text(encoding="utf-8")

    def close_old_video_cta(m: re.Match[str]) -> str:
        old_ymd, old_md = m.group(1), m.group(2)
        if old_ymd < ymd:
            return f'<span style="color:var(--gray-70)">{old_md} 場次已結束</span>'
        return m.group(0)

    new_videos_html = re.sub(
        r'<a class="btn btn-outline-dark" href="join\.html\?meeting=(\d{4}-\d{2}-\d{2})">預約 (\d{1,2}/\d{1,2}) 例會</a>',
        close_old_video_cta,
        videos_html,
    )

    archive_id = f"archive-reel-{compact}"
    if archive_id not in new_videos_html:
        summary_text = paragraphs[0] if paragraphs else f"這支影片為 {md_short} 主講的演講前預告。"
        month_en = d.strftime("%B").upper()
        kicker_line = f'<div class="kicker">{d.year} {month_en}</div>'
        card_body = f"""    <h2 style="margin-bottom:20px">{html.escape(speaker)}｜{html.escape(prof_short)}・主講預告</h2>
    <article class="video-archive-card">
      <div class="reel-frame edm-frame" id="{archive_id}">
        <img src="{svg_rel}" alt="{html.escape(speaker)} {md_short} 主講資訊圖卡：{html.escape(headline)}">
        <button class="reel-load" type="button" data-reel-target="{archive_id}" data-facebook-url="{reel_url}" data-reel-title="{html.escape(speaker)}主講預告：{html.escape(headline)}">
          <span aria-hidden="true">▶</span> 播放影片
        </button>
      </div>
      <div class="body">
        <div class="week">{ymd}（五）06:30</div>
        <h3>{html.escape(headline)}</h3>
        <p class="speaker">主講者：<b>{html.escape(speaker)}</b>・{html.escape(company)}</p>
        <p>{html.escape(summary_text)}</p>
        <p class="meta">合作夥伴：{html.escape(partners)}</p>
        <p class="video-note">影片由金貴分會官方粉絲團發布；若嵌入播放器無法載入，可直接前往 Facebook 觀看。</p>
        <p class="video-note">圖卡由本站依官方預告整理製作。</p>
        <div class="btns compact-btns">
          <a class="btn btn-red" href="{reel_url}" target="_blank" rel="noopener">前往 Facebook 觀看</a>
          <a class="btn btn-outline-dark" href="join.html?meeting={ymd}">預約 {md_short} 例會</a>
        </div>
      </div>
    </article>

"""
        if kicker_line in new_videos_html:
            new_videos_html, n_grid = re.subn(
                rf"({re.escape(kicker_line)}\n)",
                r"\1" + card_body,
                new_videos_html,
                count=1,
            )
        else:
            new_videos_html, n_grid = re.subn(
                r'(<div class="kicker">\d{4}\s+[A-Z]+</div>)',
                f"    {kicker_line}\n{card_body}" + r"\1",
                new_videos_html,
                count=1,
            )
        if n_grid != 1:
            raise RuntimeError("無法在 docs/videos.html 定位歷史影片區起點")
    videos_path.write_text(new_videos_html, encoding="utf-8")
    changes.append(f"docs/videos.html: 已新增 {ymd} {speaker} 預告卡並關閉舊場次報名鈕")

    # 4. Update docs/join.html
    join_path = docs_dir / "join.html"
    join_html = join_path.read_text(encoding="utf-8")
    option_val = f"{ymd}（五）06:30｜{speaker}主講"

    new_join_html = re.sub(
        r'<option value="\d{4}-\d{2}-\d{2}（五）06:30｜[^"]+主講"[^>]*>.*?</option>',
        f'<option value="{html.escape(option_val)}">{html.escape(option_val)}</option>',
        join_html,
        count=1,
    )
    new_join_html = re.sub(
        r"saved\.date\.startsWith\('\d{4}-\d{2}-\d{2}'\)",
        f"saved.date.startsWith('{ymd}')",
        new_join_html,
    )
    new_join_html = re.sub(
        r"text: 'BNI 金貴分會例會｜[^']+主講'",
        f"text: 'BNI 金貴分會例會｜{speaker}主講'",
        new_join_html,
    )
    new_join_html = re.sub(
        r"dates: '\d{8}T063000/\d{8}T083000'",
        f"dates: '{compact}T063000/{compact}T083000'",
        new_join_html,
    )
    new_join_html = re.sub(
        r"new Date\('\d{4}-\d{2}-\d{2}T06:30:00\+08:00'\)",
        f"new Date('{ymd}T06:30:00+08:00')",
        new_join_html,
    )
    new_join_html = re.sub(
        r'#g-date option\[value\^="\d{4}-\d{2}-\d{2}"\]',
        f'#g-date option[value^="{ymd}"]',
        new_join_html,
    )
    new_join_html = re.sub(
        r"requestedMeeting !== '\d{4}-\d{2}-\d{2}'",
        f"requestedMeeting !== '{ymd}'",
        new_join_html,
    )
    new_join_html = re.sub(
        r"requestedMeeting === '\d{4}-\d{2}-\d{2}'",
        f"requestedMeeting === '{ymd}'",
        new_join_html,
    )
    new_join_html = re.sub(
        r"document\.getElementById\('g-date'\)\.value = '\d{4}-\d{2}-\d{2}（五）06:30｜[^']+主講';",
        f"document.getElementById('g-date').value = '{option_val}';",
        new_join_html,
    )
    join_path.write_text(new_join_html, encoding="utf-8")
    changes.append(f"docs/join.html: 已更新報名選項、截止時間與 Google 日曆為 {ymd} {speaker}")

    # 5. Update docs/sitemap.xml
    update_sitemap_lastmod(
        docs_dir,
        {
            "https://ginguitn.com/": today_str,
            "https://ginguitn.com/videos.html": today_str,
            "https://ginguitn.com/join.html": today_str,
        },
    )
    changes.append(f"docs/sitemap.xml: 已更新 <lastmod> 為 {today_str}")

    return {"mode": "speaker_update", "meeting_date": ymd, "speaker_name": speaker, "changes": changes}


def cmd_update_site(args: argparse.Namespace) -> int:
    ref_dt = now_tpe(args.now)
    today_str = ref_dt.date().isoformat()
    docs_dir = pathlib.Path(args.docs_dir).resolve() if args.docs_dir else DOCS_DIR

    if args.source_json:
        src_path = pathlib.Path(args.source_json).resolve()
        payload = json.loads(src_path.read_text(encoding="utf-8"))
        res = apply_speaker_update(docs_dir, payload, today_str)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.close_expired:
        res = apply_close_expired(docs_dir, today_str)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    raise SystemExit("請指定 --source-json <path> 或 --close-expired")


def verify_site_status(
    docs_dir: pathlib.Path,
    content_dir: pathlib.Path,
    ref_dt: datetime.datetime,
    target_friday: datetime.date,
    run_node_test: bool = True,
) -> dict[str, Any]:
    index_html = (docs_dir / "index.html").read_text(encoding="utf-8")
    videos_html = (docs_dir / "videos.html").read_text(encoding="utf-8")
    join_html = (docs_dir / "join.html").read_text(encoding="utf-8")
    sitemap_xml = (docs_dir / "sitemap.xml").read_text(encoding="utf-8")

    hygiene_errors: list[str] = []
    upcoming_missing_reasons: list[str] = []

    target_ymd = target_friday.isoformat()
    target_compact = target_friday.strftime("%Y%m%d")

    # 1. Check for any active registration links pointing to expired dates (< target_friday)
    for page_name, content in [("docs/index.html", index_html), ("docs/videos.html", videos_html)]:
        for m in re.finditer(r'href="join\.html\?meeting=(\d{4}-\d{2}-\d{2})"', content):
            linked_date = m.group(1)
            if linked_date < target_ymd:
                hygiene_errors.append(f"{page_name} 仍殘留已過期場次報名連結: join.html?meeting={linked_date}")

    # 2. Check join.html option state
    opt_m = re.search(
        r'<option value="(\d{4}-\d{2}-\d{2})（五）06:30｜([^"]+?)主講"([^>]*)>(.*?)</option>',
        join_html,
    )
    join_option_date = opt_m.group(1) if opt_m else None
    join_option_speaker = opt_m.group(2) if opt_m else None
    join_option_attrs = opt_m.group(3) if opt_m else ""
    join_option_text = opt_m.group(4) if opt_m else ""

    if not opt_m:
        hygiene_errors.append("docs/join.html 找不到主講場次 <option>")
    elif join_option_date and join_option_date < target_ymd:
        if "disabled" not in join_option_attrs or "已結束報名" not in join_option_text:
            hygiene_errors.append(
                f"docs/join.html 過期場次 ({join_option_date}) 靜態 <option> 未標示 disabled 或（已結束報名）"
            )

    # 3. Check internal SVG/image references exist
    for page_name, content in [("docs/index.html", index_html), ("docs/videos.html", videos_html)]:
        for img_m in re.finditer(r'<img[^>]+src="([^"]+)"', content):
            src = img_m.group(1)
            if src.startswith(("http://", "https://", "data:")):
                continue
            if not (docs_dir / src).exists():
                hygiene_errors.append(f"{page_name} 引用不存在的圖片資源: {src}")

    # 4. Check reel container IDs vs triggers in videos.html
    for trig_m in re.finditer(r'data-reel-target="([^"]+)"', videos_html):
        target_id = trig_m.group(1)
        if f'id="{target_id}"' not in videos_html:
            hygiene_errors.append(f"docs/videos.html 找不到 data-reel-target 對應容器 id={target_id}")

    # 5. Check sitemap.xml validity and lastmod freshness
    try:
        root = ET.fromstring(sitemap_xml)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        url_nodes = root.findall("sm:url", ns)
        if len(url_nodes) < 11:
            hygiene_errors.append(f"docs/sitemap.xml URL 數量不足: {len(url_nodes)}")
        lastmod_map = {}
        for u in url_nodes:
            loc_el = u.find("sm:loc", ns)
            mod_el = u.find("sm:lastmod", ns)
            if loc_el is not None and mod_el is not None and loc_el.text and mod_el.text:
                lastmod_map[loc_el.text.strip()] = mod_el.text.strip()
        for key_url in ("https://ginguitn.com/", "https://ginguitn.com/join.html", "https://ginguitn.com/videos.html"):
            if lastmod_map.get(key_url, "2026-10-05") <= "2026-10-05":
                hygiene_errors.append(f"docs/sitemap.xml {key_url} 的 <lastmod> 仍停留在舊日期 ({lastmod_map.get(key_url)})")
    except Exception as exc:
        hygiene_errors.append(f"docs/sitemap.xml XML 解析失敗: {exc}")

    # 6. Run tests/join-success.test.cjs (via node or deno) if verifying default docs_dir
    node_test_res = {"ran": False, "returncode": 0, "stdout": "", "stderr": ""}
    if run_node_test:
        test_file = str(TESTS_DIR / "join-success.test.cjs")
        js_cmd: list[str] | None = None
        for cand_bin in ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/opt/homebrew/opt/node@20/bin/node"):
            if pathlib.Path(cand_bin).exists():
                js_cmd = [cand_bin, test_file]
                break
        if js_cmd is None and pathlib.Path("/opt/homebrew/bin/deno").exists():
            js_cmd = ["/opt/homebrew/bin/deno", "run", "--allow-read", test_file]
        if js_cmd is None:
            js_cmd = ["node", test_file]

        try:
            proc = subprocess.run(
                js_cmd,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            node_test_res = {
                "ran": True,
                "cmd": " ".join(js_cmd),
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
            if proc.returncode != 0:
                hygiene_errors.append(f"tests/join-success.test.cjs 測試失敗: {proc.stderr or proc.stdout}")
        except Exception as exc:
            node_test_res = {
                "ran": True,
                "cmd": " ".join(js_cmd),
                "returncode": -1,
                "stdout": "",
                "stderr": str(exc),
            }
            hygiene_errors.append(f"執行 tests/join-success.test.cjs 發生例外: {exc}")

    # 7. Check whether the upcoming Friday (target_friday) speaker has genuinely been updated
    source_receipt_file = content_dir / f"speaker-source-{target_compact}.json"
    latest_source_check_file = content_dir / "latest-source-check.json"
    latest_source_status = None
    latest_source_message = None
    if latest_source_check_file.exists():
        try:
            lsc = json.loads(latest_source_check_file.read_text(encoding="utf-8"))
            latest_source_status = lsc.get("status")
            latest_source_message = lsc.get("message")
        except Exception:
            pass

    if not source_receipt_file.exists():
        upcoming_missing_reasons.append(f"缺少 {target_ymd} 講者來源驗證回執 ({source_receipt_file.name})")
    else:
        try:
            src_payload = json.loads(source_receipt_file.read_text(encoding="utf-8"))
            src_errs = validate_source_payload(src_payload)
            if src_errs:
                upcoming_missing_reasons.append(f"{source_receipt_file.name} 欄位不完整: {'; '.join(src_errs)}")
        except Exception as exc:
            upcoming_missing_reasons.append(f"{source_receipt_file.name} 讀取失敗: {exc}")

    if f'href="join.html?meeting={target_ymd}"' not in index_html:
        upcoming_missing_reasons.append(f"docs/index.html 尚未更新為 {target_ymd} 場次報名入口")
    if join_option_date != target_ymd:
        upcoming_missing_reasons.append(f"docs/join.html 主講場次仍為 {join_option_date}（尚未更新至 {target_ymd}）")
    if f"archive-reel-{target_compact}" not in videos_html:
        upcoming_missing_reasons.append(f"docs/videos.html 尚未新增 {target_ymd} 主講預告卡片")

    local_hygiene_ok = len(hygiene_errors) == 0
    upcoming_speaker_updated = local_hygiene_ok and len(upcoming_missing_reasons) == 0

    if not local_hygiene_ok:
        verdict = "error_hygiene_or_test_failed"
        summary_msg = "錯誤：本機網站檔案檢查或測試未通過 — " + "；".join(hygiene_errors)
    elif upcoming_speaker_updated:
        verdict = "verified_ok"
        summary_msg = f"驗證通過：官網已完成 {target_ymd}（{join_option_speaker}）主講更新且所有本機測試通過。"
    else:
        verdict = "blocked_upcoming_speaker_not_updated"
        src_note = latest_source_message or "Facebook 粉專需登入驗證或尚未發布下週預告"
        summary_msg = (
            f"受阻：本機過期場次保護與網站測試已通過，但尚未完成下一場（{target_ymd}）主講者更新。"
            f"原因：{src_note}。依規定禁止標記排程為成功（verified_ok）。"
        )

    return {
        "checked_at": ref_dt.isoformat(timespec="seconds"),
        "target_friday": target_ymd,
        "active_site_meeting_date": join_option_date,
        "active_site_speaker": join_option_speaker,
        "local_hygiene_ok": local_hygiene_ok,
        "hygiene_errors": hygiene_errors,
        "node_test": node_test_res,
        "upcoming_speaker_updated": upcoming_speaker_updated,
        "upcoming_missing_reasons": upcoming_missing_reasons,
        "latest_source_status": latest_source_status,
        "verdict": verdict,
        "message": summary_msg,
    }


def cmd_verify_status(args: argparse.Namespace) -> int:
    ref_dt = now_tpe(args.now)
    target_friday = (
        datetime.date.fromisoformat(args.target_friday)
        if args.target_friday
        else compute_target_friday(ref_dt)
    )
    docs_dir = pathlib.Path(args.docs_dir).resolve() if args.docs_dir else DOCS_DIR
    content_dir = pathlib.Path(args.content_dir).resolve() if args.content_dir else CONTENT_DIR

    res = verify_site_status(
        docs_dir=docs_dir,
        content_dir=content_dir,
        ref_dt=ref_dt,
        target_friday=target_friday,
        run_node_test=not args.skip_node_test,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))

    if not res["local_hygiene_ok"]:
        return 1
    if args.require_upcoming_speaker and not res["upcoming_speaker_updated"]:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BNI 金貴官網每週自動更新與驗收工具")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_fetch = sub.add_parser("fetch-source", help="讀取 Facebook 粉專最新主講者資訊或檢查本機驗證回執")
    p_fetch.add_argument("--now", help="指定台北時間 ISO 字串（測試用）")
    p_fetch.add_argument("--target-friday", help="指定目標週五日期 YYYY-MM-DD")
    p_fetch.add_argument(
        "--check-open-chrome",
        action="store_true",
        help="唯讀檢查目前已開啟之 Chrome 分頁是否停留在金貴粉專（不索取密碼、不繞過登入）",
    )

    p_upd = sub.add_parser("update-site", help="依 UPDATE_WORKFLOW.md 更新本機網站檔案")
    p_upd.add_argument("--now", help="指定台北時間 ISO 字串（測試用）")
    p_upd.add_argument("--docs-dir", help="指定 docs 目錄路徑（預設為正式 repo docs/）")
    p_upd.add_argument("--source-json", help="使用已驗證之講者來源 JSON 更新下一場主講人")
    p_upd.add_argument(
        "--close-expired",
        action="store_true",
        help="依 UPDATE_WORKFLOW.md 第 6 條關閉已過期場次報名、保留影音入口並引導接待窗口安排",
    )

    p_ver = sub.add_parser("verify-status", help="驗證本機網站狀態與是否真正完成下一週更新")
    p_ver.add_argument("--now", help="指定台北時間 ISO 字串（測試用）")
    p_ver.add_argument("--target-friday", help="指定目標週五日期 YYYY-MM-DD")
    p_ver.add_argument("--docs-dir", help="指定 docs 目錄路徑")
    p_ver.add_argument("--content-dir", help="指定 content 目錄路徑")
    p_ver.add_argument("--skip-node-test", action="store_true", help="略過執行 node 測試（供單元測試快速驗證）")
    p_ver.add_argument(
        "--require-upcoming-speaker",
        action="store_true",
        help="嚴格要求下一週（target_friday）主講者必須已更新完成，否則回傳 exit code 2（防止排程假成功）",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.subcommand == "fetch-source":
        return cmd_fetch_source(args)
    if args.subcommand == "update-site":
        return cmd_update_site(args)
    if args.subcommand == "verify-status":
        return cmd_verify_status(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
