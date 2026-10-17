"""클라우드 회차(GitHub Actions, PC 없이) — ① 정해진 시각이면 캐러셀 1편 발행 ② 최근 글 댓글에 자료 DM(비공개 답장)
③ 다음에 깰 대기 시간을 GITHUB_OUTPUT 'next' 로.

발행 규칙(config): 하루 posts_per_day 편 · slots(예 10:00·15:00·21:00) 이후 · 직전 글과 min_gap_minutes 이상 · end 넘으면 그날 끝.
겹침 방지: 저장소 기록(state/ledger.json)과 인스타 실제 마지막 게시 시각 둘 다 확인.
DM: 레퍼런스(@every._ai)처럼 '아무 댓글이나 남기면 DM으로 자료' — 공식 API 비공개 답장(댓글당 1번·7일 안).
    DM 이 막히면(개발 모드 제한 등) 댓글에 '프로필 링크 → 자료실' 공개 답글로 대신한다. 처리 기록 state/dm.json.
python3 cloud/tick.py [--dry] [--test]   (표준 라이브러리만)
"""
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
ST = ROOT / "state"
REPO = os.environ.get("GITHUB_REPOSITORY", "KangJH79/moneyball-ai")
GRAPH = "https://graph.instagram.com/v23.0"
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
UID = os.environ.get("IG_USER_ID", "")
DRY = "--dry" in sys.argv


def jl(name, d):
    p = ST / name
    try:
        return json.loads(p.read_text("utf-8")) if p.exists() else d
    except ValueError:
        return d


def js(name, obj):
    (ST / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")


CFG = jl("config.json", {})
N = int(CFG.get("posts_per_day", 3))
GAP = dt.timedelta(minutes=int(CFG.get("min_gap_minutes", 180)))
MAX_FAIL = int(CFG.get("max_fail_per_day", 2))
HANDLE = CFG.get("handle", "moneyball7979")
BLOCK_CODES = (190, 10, 200, 4, 17, 32, 613)


class IGErr(RuntimeError):
    def __init__(self, msg, code=None, sub=None):
        super().__init__(msg)
        self.code = code
        self.sub = sub


def api(method, path, json_body=None, **params):
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{path}"
    for i in range(3):
        try:
            if method == "GET":
                req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params))
            elif json_body is not None:
                req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), method="POST",
                                             data=json.dumps(json_body).encode(), headers={"Content-Type": "application/json"})
            else:
                req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode()).get("error", {})
            except Exception:  # noqa: BLE001
                err = {}
            if e.code >= 500 and i < 2:
                time.sleep(5 * (i + 1))
                continue
            raise IGErr(f"HTTP {e.code} code={err.get('code')} sub={err.get('error_subcode')} {err.get('message', '')[:160]}",
                        err.get("code"), err.get("error_subcode"))
        except (urllib.error.URLError, TimeoutError) as e:
            if i < 2:
                time.sleep(5 * (i + 1))
                continue
            raise IGErr(f"네트워크 {e}")


def kst(s):
    return dt.datetime.fromisoformat(s).astimezone(KST)


def wait_ready(cid, tries=40):
    for _ in range(tries):  # 최대 2분
        st = api("GET", cid, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise IGErr(f"컨테이너 {st}", "container")
        time.sleep(3)
    raise IGErr("컨테이너 준비 2분 넘음", "container")


def head_ok(u):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=30) as r:
            return r.status == 200 and "image" in (r.headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        return False


def publish(item):
    urls = [f"https://raw.githubusercontent.com/{REPO}/media/{item['id']}/{i:02d}.jpg" for i in range(1, item["cards"] + 1)]
    bad = [u for u in urls if not head_ok(u)]
    if bad:
        raise IGErr(f"카드 그림 주소 안 열림 {len(bad)}장", "media")
    kids = [api("POST", f"{UID}/media", image_url=u, is_carousel_item="true")["id"] for u in urls]
    for k in kids:
        wait_ready(k)
    parent = api("POST", f"{UID}/media", media_type="CAROUSEL", children=",".join(kids), caption=item["caption"])["id"]
    wait_ready(parent)
    if DRY:
        print("DRY: 컨테이너까지 OK(인스타가 그림을 가져감) — 발행 안 함")
        return None
    mid = api("POST", f"{UID}/media_publish", creation_id=parent)["id"]
    info = api("GET", mid, fields="permalink,timestamp,caption,children{id}")
    n = len((info.get("children") or {}).get("data", []))
    return {"id": item["id"], "media_id": mid, "permalink": info.get("permalink"), "format": "carousel",
            "published_at": dt.datetime.now(KST).isoformat(timespec="seconds"), "cards": n,
            "caption_ok": (info.get("caption") or "").strip() == item["caption"].strip(),
            "deliverable": item.get("deliverable"), "guide_url": item.get("guide_url"), "dm_text": item.get("dm_text")}


def commit(msg):
    if DRY or not os.environ.get("GITHUB_ACTIONS"):
        return
    for i in range(3):
        subprocess.run(["git", "add", "state"], cwd=ROOT, check=True)
        if not subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
            return
        subprocess.run(["git", "-c", "user.name=ai-tick", "-c", "user.email=tick@moneyball-ai", "commit", "-q", "-m", msg],
                       cwd=ROOT, check=True)
        if subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], cwd=ROOT)
    print("기록 올리기 실패(3번)")


def wait_name(minutes):
    if minutes >= 150:
        return "wait120"
    if minutes >= 35:
        return "wait30"
    if minutes >= 6:
        return "wait5"
    return "wait1"


# ── 댓글 → DM ─────────────────────────────────────────────
def dm_round(led, now):
    """최근 7일 글의 새 댓글에 자료 DM. 돌려줌 = (보낸 수, 최근 48시간 안 글이 있나)."""
    cfg = CFG.get("dm") or {}
    if not cfg.get("enabled", True) or not TOKEN:
        return 0, False
    log = jl("dm.json", {"done": {}, "blocked_until": None, "stats": {}})
    done = log.setdefault("done", {})
    cap = int(cfg.get("max_per_tick", 40))
    recent = [r for r in led if r.get("media_id") and now - kst(r["published_at"]) < dt.timedelta(days=7)]
    hot = any(now - kst(r["published_at"]) < dt.timedelta(hours=48) for r in recent)
    sent = 0
    dm_blocked = log.get("blocked_until") and now < kst(log["blocked_until"])
    for r in sorted(recent, key=lambda x: x["published_at"], reverse=True):
        if sent >= cap:
            break
        try:
            cs = api("GET", f"{r['media_id']}/comments", fields="id,text,username,timestamp,from", limit=50).get("data") or []
        except IGErr as e:
            print(f"댓글 읽기 실패 {r['id']}: {e}")
            if e.code in BLOCK_CODES:
                break
            continue
        url = r.get("guide_url") or CFG.get("jaryo_url", "")
        what = r.get("deliverable") or "자료"
        for c in cs:
            if sent >= cap:
                break
            cid = c.get("id")
            who = (c.get("from") or {}).get("username") or c.get("username") or ""
            if not cid or cid in done or who == HANDLE:
                continue
            if now - dt.datetime.strptime(c["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(KST) > dt.timedelta(days=7):
                done[cid] = {"t": now.isoformat(timespec="seconds"), "how": "old"}
                continue
            text = r.get("dm_text") or (f"안녕하세요! 머니볼 AI예요 🙌\n요청하신 「{what}」 정리해뒀어요 👇\n{url}\n\n"
                                        "저장해두고 필요할 때 꺼내 쓰세요. 다음 자료도 놓치지 않게 팔로우해두세요!")
            how = None
            if not dm_blocked and not DRY:
                try:
                    api("POST", f"{UID}/messages", json_body={"recipient": {"comment_id": cid}, "message": {"text": text}})
                    how = "dm"
                except IGErr as e:
                    print(f"DM 실패({who}): {e}")
                    if e.code in (10, 200, 3, 230, 551) or "permission" in str(e).lower() or "capability" in str(e).lower():
                        dm_blocked = True
                        log["blocked_until"] = (now + dt.timedelta(hours=6)).isoformat(timespec="seconds")
                        log["blocked_why"] = str(e)[:200]
                    elif e.code in BLOCK_CODES:
                        break
            if DRY:
                print(f"DRY: {who} 댓글에 DM/답글 예정")
                continue
            try:
                if how == "dm":
                    api("POST", f"{cid}/replies", message="DM으로 보내드렸어요! 📩 안 보이면 메시지 요청함을 확인해주세요")
                elif cfg.get("fallback_public_reply", True):
                    api("POST", f"{cid}/replies", message=f"프로필 링크 → 자료실에 「{what}」 올려뒀어요! 📩")
                    how = "reply"
            except IGErr as e:
                print(f"답글 실패({who}): {e}")
                if e.code in BLOCK_CODES:
                    break
            done[cid] = {"t": now.isoformat(timespec="seconds"), "how": how or "fail", "post": r["id"]}
            st = log.setdefault("stats", {})
            st[how or "fail"] = st.get(how or "fail", 0) + 1
            sent += 1
            time.sleep(2)
    if len(done) > 5000:                     # 기록이 끝없이 불지 않게 최근 것만
        keep = sorted(done.items(), key=lambda kv: kv[1].get("t", ""))[-4000:]
        log["done"] = dict(keep)
    js("dm.json", log)
    return sent, hot


def main():
    now = dt.datetime.now(KST)
    q, led, blk = jl("queue.json", []), jl("ledger.json", []), jl("blocked.json", {})
    today = now.date().isoformat()
    slots = [now.replace(hour=int(s[:2]), minute=int(s[3:5]), second=0, microsecond=0) for s in CFG.get("slots", ["10:00", "15:00", "21:00"])]
    eh, em = map(int, CFG.get("end", "22:30").split(":"))
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    tomorrow = slots[0] + dt.timedelta(days=1)
    pub_today = [r for r in led if kst(r["published_at"]).date().isoformat() == today]
    fails = [it for it in q if it.get("status") == "failed" and (it.get("failed_at") or "")[:10] == today]
    last = max([kst(r["published_at"]) for r in led] or [now - dt.timedelta(days=1)])
    k = min(len(pub_today), len(slots) - 1)
    due = max(slots[k], last + GAP)
    if blk.get("until"):
        due = max(due, kst(blk["until"]))
    msg = ""
    if len(pub_today) >= N or now >= end or len(fails) >= MAX_FAIL:
        due = max(tomorrow, kst(blk["until"])) if blk.get("until") else tomorrow
        msg = f"오늘 발행 끝({len(pub_today)}/{N}, 실패 {len(fails)})"
    elif now >= due:
        ready = [it for it in q if it.get("status") == "ready" and it["id"] not in {r["id"] for r in led}]
        if not ready:
            msg = "발행할 '준비' 글 없음"
            due = now + dt.timedelta(minutes=30)
        else:
            try:
                m = api("GET", f"{UID}/media", fields="timestamp", limit=1).get("data") or []
                if m:
                    api_last = dt.datetime.strptime(m[0]["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(KST)
                    if now < api_last + GAP:
                        due = api_last + GAP
                        msg = f"인스타 마지막 게시 {api_last:%H:%M} → {due:%H:%M} 까지 기다림"
            except IGErr as e:
                msg = f"마지막 게시 확인 실패({e}) → 30분 뒤"
                due = now + dt.timedelta(minutes=30)
            if not msg:
                item = ready[0]
                try:
                    rec = publish(item)
                    if rec:
                        led.append(rec)
                        item["status"] = "published"
                        js("ledger.json", led)
                        js("queue.json", q)
                        if blk:
                            js("blocked.json", {})
                        commit(f"published {item['id']}")
                        msg = f"발행 {item['id']} {rec['permalink']} ({rec['cards']}장, 본문 일치 {rec['caption_ok']})"
                        k2 = min(len(pub_today) + 1, len(slots) - 1)
                        due = max(slots[k2], dt.datetime.now(KST) + GAP)
                    else:
                        msg = "DRY 끝"
                except IGErr as e:
                    if e.code in BLOCK_CODES or "limit" in str(e).lower():
                        js("blocked.json", {"until": (now + dt.timedelta(minutes=60)).isoformat(timespec="seconds"), "why": str(e)})
                        msg = f"계정·한도 문제 → 60분 쉼: {e}"
                        due = now + dt.timedelta(minutes=60)
                    else:
                        item["status"] = "failed"
                        item["error"] = str(e)
                        item["failed_at"] = now.isoformat(timespec="seconds")
                        js("queue.json", q)
                        msg = f"실패 {item['id']}: {e} → 다음 글은 5분 뒤"
                        due = now + dt.timedelta(minutes=5)
                    commit(f"tick: {msg[:60]}")
    else:
        msg = f"다음 발행 {due:%m-%d %H:%M} (오늘 {len(pub_today)}/{N})"
    sent, hot = 0, False
    try:
        sent, hot = dm_round(led, dt.datetime.now(KST))
        if sent:
            commit(f"dm +{sent}")
    except Exception as e:  # noqa: BLE001 — DM 이 발행을 막지 않게
        print(f"DM 회차 오류: {e}")
    mins = max(0, math.ceil((due - dt.datetime.now(KST)).total_seconds() / 60))
    if hot and 8 <= now.hour < 24:
        mins = min(mins, 10)                 # 최근 48시간 글이 있으면 낮엔 10분마다 댓글 확인
    nxt = wait_name(mins)
    print(f"[{now:%m-%d %H:%M}] {msg} · DM {sent} · 다음 깨움 {nxt}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"next={nxt}\n")


if __name__ == "__main__":
    if "--test" in sys.argv:  # 시각 무시하고 첫 '준비' 글로 컨테이너까지만(발행 안 함)
        DRY = True
        it = next(it for it in jl("queue.json", []) if it.get("status") == "ready")
        print("시험", it["id"], publish(it))
    else:
        main()
