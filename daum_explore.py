# -*- coding: utf-8 -*-
"""다음앱 UI 탐색 — 검색 → 플레이스 → 리뷰탭 덤프."""
import os
import re
import sys
import time
import subprocess
import xml.etree.ElementTree as ET

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
ADB = os.path.join(ROOT, "platform-tools", "adb")
SERIAL = "R3CY80RW96Y"
PKG = "net.daum.android.daum"
MAIN = f"{PKG}/.DaumActivity"
KEYWORD = "영월 닭강정 가나닭강정"
PLACE = "영월가나닭강정"

import uiautomator2 as u2


def adb(*args, timeout=25):
    exe = ADB if os.path.isfile(ADB) else "adb"
    return subprocess.run(
        [exe, "-s", SERIAL, *args], capture_output=True, text=True, timeout=timeout
    )


def save_dump(d, name):
    path = os.path.join(ROOT, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(d.dump_hierarchy(compressed=False))
    print(f"[+] dump {path}")
    try:
        d.screenshot(os.path.join(ROOT, name.replace(".xml", ".png")))
    except Exception:
        pass


def dismiss_popups(d):
    for t in ("허용 안함", "허용 안 함", "나중에", "닫기", "건너뛰기", "오늘 그만보기"):
        el = d(text=t)
        if el.exists:
            el.click()
            time.sleep(1)


def open_daum(d):
    adb("shell", "cmd", "device_state", "state", "0")
    adb("shell", "settings", "put", "secure", "location_mode", "0")
    adb("shell", "am", "force-stop", PKG)
    time.sleep(1)
    adb(
        "shell",
        "am",
        "start",
        "--display",
        "0",
        "-a",
        "android.intent.action.MAIN",
        "-c",
        "android.intent.category.LAUNCHER",
        "-n",
        MAIN,
    )
    time.sleep(4)
    dismiss_popups(d)


def open_search(d):
    if d(description="검색창").exists:
        d(description="검색창").click()
    else:
        d.click(540, 374)
    time.sleep(1.5)


def type_keyword(d, keyword):
    # 검색 입력 영역
    if d(text="검색어 입력").exists:
        d(text="검색어 입력").click()
    elif d(description="통합검색").exists:
        d(description="통합검색").click()
    else:
        d.click(526, 220)
    time.sleep(0.3)
    d.clear_text()
    for ch in keyword:
        d.send_keys(ch)
        time.sleep(0.05)
    time.sleep(0.5)
    d.press("enter")


def find_place_texts(d):
    root = ET.fromstring(d.dump_hierarchy())
    hits = []
    for n in root.iter("node"):
        if n.get("package") != PKG:
            continue
        text = (n.get("text") or "").replace("\u200b", "").strip()
        desc = (n.get("content-desc") or "").strip()
        if PLACE in text or PLACE in desc or "가나닭강정" in text or "가나닭강정" in desc:
            hits.append(
                (
                    n.get("resource-id", ""),
                    text[:50],
                    desc[:50],
                    n.get("bounds", ""),
                    n.get("clickable"),
                )
            )
    return hits


def main():
    d = u2.connect(SERIAL)
    print(f"[+] {d.window_size()}")
    open_daum(d)
    save_dump(d, "daum_step1_main.xml")
    open_search(d)
    save_dump(d, "daum_step2_search_open.xml")
    type_keyword(d, KEYWORD)
    time.sleep(4)
    save_dump(d, "daum_step3_results.xml")
    hits = find_place_texts(d)
    print(f"[*] place hits: {len(hits)}")
    for h in hits[:20]:
        print(" ", h)
    if hits:
        b = hits[0][3]
        nums = list(map(int, re.findall(r"-?\d+", b)[:4]))
        cx, cy = (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2
        d.click(cx, cy)
        time.sleep(4)
        save_dump(d, "daum_step4_place.xml")
        # 리뷰 탭 찾기
        root = ET.fromstring(d.dump_hierarchy())
        for n in root.iter("node"):
            t = (n.get("text") or "").strip()
            desc = (n.get("content-desc") or "").strip()
            if "리뷰" in t or "리뷰" in desc:
                print("review node:", t, desc, n.get("bounds"), n.get("clickable"))
        if d(text="리뷰").exists:
            d(text="리뷰").click()
            time.sleep(2)
            save_dump(d, "daum_step5_review.xml")


if __name__ == "__main__":
    main()
