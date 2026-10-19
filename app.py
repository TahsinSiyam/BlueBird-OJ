"""BlueBird Competitive Programming - Online Judge (Streamlit + Supabase)."""
import hmac
import html
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from supabase import create_client

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover
    resource = None

ROOT = Path(__file__).parent
LOGO = ROOT / "logo.jpeg"
st.set_page_config(page_title="BlueBird Online Judge", page_icon="🐦", layout="wide")
st.markdown(
    """<style>
.block-container{padding-top:2rem;max-width:1200px}
.badge{padding:2px 10px;border-radius:999px;font-size:.78rem;font-weight:600;color:#fff;white-space:nowrap}
.tag{padding:2px 8px;border-radius:6px;font-size:.75rem;background:rgba(37,99,235,.14);color:#2563eb;margin-right:4px}
</style>""",
    unsafe_allow_html=True,
)

ss = st.session_state
ss.setdefault("page", "🏠 Home")
ss.setdefault("pid", None)
ss.setdefault("admin", False)
ss.setdefault("ed_ver", 0)

HOME, PROB, LB, MYS, ADM = "🏠 Home", "🧩 Problems", "🏆 Leaderboard", "📜 My Submissions", "🔐 Admin"
DIFFS = ["Easy", "Medium", "Hard"]
COLORS = {"Easy": "#16a34a", "Medium": "#f59e0b", "Hard": "#dc2626"}
ICON = {"Accepted": "✅", "Wrong Answer": "❌", "Time Limit Exceeded": "⏱️",
        "Runtime Error": "💥", "Compilation Error": "🛠️"}

# --------------------------------------------------------------------------- #
# Supabase
# --------------------------------------------------------------------------- #
def secret(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return default


@st.cache_resource
def client():
    return create_client(secret("SUPABASE_URL"), secret("SUPABASE_KEY"))


def run(query):
    try:
        return query.execute().data or []
    except Exception as e:  # noqa: BLE001
        st.error(f"Database error: {e}")
        return []


def problems(include_hidden=False):
    q = client().table("problems").select("*").order("id")
    if not include_hidden:
        q = q.eq("is_published", True)
    return run(q)


def tests_of(pid):
    return run(client().table("test_cases").select("*").eq("problem_id", pid).order("id"))


def subs(user=None, pid=None, limit=500,
         cols="id,username,problem_id,language,verdict,passed,total,runtime_ms,created_at"):
    q = client().table("submissions").select(cols).order("id", desc=True).limit(limit)
    if user:
        q = q.eq("username", user)
    if pid:
        q = q.eq("problem_id", pid)
    return run(q)


def sub_table(rows, titles):
    if not rows:
        st.info("No submissions yet.")
        return
    df = pd.DataFrame(rows)
    df["Problem"] = df["problem_id"].map(titles).fillna("—")
    df["When"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M")
    df["Tests"] = df["passed"].astype(str) + "/" + df["total"].astype(str)
    out = df[["id", "When", "username", "Problem", "language", "verdict", "Tests", "runtime_ms"]]
    st.dataframe(out.rename(columns={"id": "ID", "username": "User", "language": "Language",
                                     "verdict": "Verdict", "runtime_ms": "Time (ms)"}),
                 hide_index=True)


# --------------------------------------------------------------------------- #
# Judge engine
# --------------------------------------------------------------------------- #
LANGS = {
    "Python 3": dict(ext="py", run=[sys.executable, "{src}"]),
    "C++17": dict(ext="cpp", bin="g++", compile=["g++", "-O2", "-std=c++17", "-o", "{exe}", "{src}"], run=["{exe}"]),
    "C11": dict(ext="c", bin="gcc", compile=["gcc", "-O2", "-std=c11", "-o", "{exe}", "{src}", "-lm"], run=["{exe}"]),
    "Java": dict(ext="java", bin="javac", compile=["javac", "{src}"],
                 run=["java", "-Xmx256m", "-cp", "{dir}", "Main"]),
}
TEMPLATES = {
    "Python 3": "import sys\n\ndef main():\n    data = sys.stdin.read().split()\n    # your code here\n\nmain()\n",
    "C++17": "#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n    ios::sync_with_stdio(false);\n    cin.tie(nullptr);\n    // your code here\n    return 0;\n}\n",
    "C11": "#include <stdio.h>\n\nint main(void) {\n    /* your code here */\n    return 0;\n}\n",
    "Java": "import java.util.*;\n\npublic class Main {\n    public static void main(String[] args) {\n        Scanner sc = new Scanner(System.in);\n        // your code here\n    }\n}\n",
}


def available_langs():
    return [k for k, v in LANGS.items() if "bin" not in v or shutil.which(v["bin"])]


def norm(s):
    return "\n".join(line.rstrip() for line in (s or "").replace("\r\n", "\n").strip().splitlines())


def _preexec(mem_mb, java):
    if os.name != "posix" or resource is None:
        return None

    def limit():
        try:
            if not java:
                b = int(mem_mb) * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (b, b))
            f = 16 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (f, f))
        except Exception:  # noqa: BLE001
            pass

    return limit


def run_one(cmd, inp, tl, cwd, mem_mb, java):
    if inp and not inp.endswith("\n"):
        inp += "\n"
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "HOME": cwd}
    start = time.perf_counter()
    try:
        p = subprocess.run(cmd, input=inp or "", capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=tl + (1.0 if java else 0), cwd=cwd, env=env,
                           preexec_fn=_preexec(mem_mb, java))
    except subprocess.TimeoutExpired:
        return "Time Limit Exceeded", "", int(tl * 1000)
    except Exception as e:  # noqa: BLE001
        return "Runtime Error", str(e), 0
    ms = int((time.perf_counter() - start) * 1000)
    if p.returncode != 0:
        return "Runtime Error", (p.stderr or f"Exit code {p.returncode}")[-1500:], ms
    return "OK", p.stdout[:50000], ms


def judge(code, lang, tests, tl, mem_mb):
    cfg = LANGS[lang]
    cases = []
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / ("Main.java" if lang == "Java" else f"sol.{cfg['ext']}")
        src.write_text(code, encoding="utf-8")
        exe = str(Path(d) / "sol")

        def fmt(cmd):
            return [a.format(src=str(src), exe=exe, dir=d) for a in cmd]

        if "compile" in cfg:
            try:
                c = subprocess.run(fmt(cfg["compile"]), capture_output=True, text=True, timeout=60, cwd=d)
            except subprocess.TimeoutExpired:
                return dict(verdict="Compilation Error", passed=0, total=len(tests), ms=0, cases=[],
                            error="Compilation timed out.")
            if c.returncode != 0:
                return dict(verdict="Compilation Error", passed=0, total=len(tests), ms=0, cases=[],
                            error=(c.stderr or c.stdout)[-3000:])
        for i, t in enumerate(tests, 1):
            status, out, ms = run_one(fmt(cfg["run"]), t.get("input", ""), tl, d, mem_mb, lang == "Java")
            exp = t.get("expected_output")
            if status == "OK":
                status = "Accepted" if exp is None or norm(out) == norm(exp) else "Wrong Answer"
            cases.append(dict(n=i, v=status, ms=ms, sample=bool(t.get("is_sample")),
                              inp=t.get("input", ""), exp=exp, got=out))
    passed = sum(c["v"] == "Accepted" for c in cases)
    verdict = "Accepted" if passed == len(cases) else next(c["v"] for c in cases if c["v"] != "Accepted")
    return dict(verdict=verdict, passed=passed, total=len(cases),
                ms=max((c["ms"] for c in cases), default=0), cases=cases, error="")


def show_result(r):
    v = r["verdict"]
    msg = f"{ICON.get(v, '')} **{v}** — {r['passed']}/{r['total']} tests passed · max {r['ms']} ms"
    (st.success if v == "Accepted" else st.error)(msg)
    if r["error"]:
        st.code(r["error"], language="text")
    if r["cases"]:
        st.dataframe(pd.DataFrame([{"Test": c["n"], "Verdict": f"{ICON.get(c['v'], '')} {c['v']}",
                                    "Time (ms)": c["ms"], "Type": "Sample" if c["sample"] else "Hidden"}
                                   for c in r["cases"]]), hide_index=True)
        for c in r["cases"]:
            if c["sample"] and c["v"] != "Accepted":
                with st.expander(f"Sample test {c['n']} — details"):
                    a, b, d = st.columns(3)
                    a.caption("Input"); a.code(c["inp"] or " ", language="text")
                    b.caption("Expected"); b.code(c["exp"] or " ", language="text")
                    d.caption("Your output / error"); d.code(c["got"] or " ", language="text")


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def badge(d):
    return f'<span class="badge" style="background:{COLORS.get(d, "#64748b")}">{html.escape(d)}</span>'


def tag_html(tags):
    return "".join(f'<span class="tag">{html.escape(t)}</span>' for t in (tags or []))


def goto(page):
    ss.page = page


def open_problem(pid):
    ss.page, ss.pid = PROB, pid


def close_problem():
    ss.pid = None


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def home_page():
    _, mid, _ = st.columns([1, 2, 1])
    if LOGO.exists():
        mid.image(str(LOGO))
    st.markdown("<h3 style='text-align:center'>Solve. Compete. Soar.</h3>", unsafe_allow_html=True)
    plist = problems()
    rows = subs(limit=5000, cols="id,username,problem_id,language,verdict,passed,total,runtime_ms,created_at")
    df = pd.DataFrame(rows)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Problems", len(plist))
    c2.metric("Submissions", len(df))
    c3.metric("Competitors", df["username"].nunique() if not df.empty else 0)
    c4.metric("Acceptance", f"{(df['verdict'] == 'Accepted').mean() * 100:.0f}%" if not df.empty else "—")
    st.button("🚀 Browse problems", type="primary", on_click=goto, args=(PROB,))
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Recent submissions")
        sub_table(rows[:10], {p["id"]: p["title"] for p in plist})
    with right:
        st.subheader("Verdicts")
        if not df.empty:
            st.bar_chart(df["verdict"].value_counts())
        else:
            st.info("Charts appear after the first submission.")


def problem_list(user):
    st.title("🧩 Problems")
    plist = problems()
    if not plist:
        st.info("No problems published yet. Ask an admin to add some.")
        return
    tags = sorted({t for p in plist for t in (p.get("tags") or [])})
    f1, f2, f3 = st.columns([3, 2, 2])
    q = f1.text_input("Search", placeholder="Title or ID…", label_visibility="collapsed")
    dsel = f2.multiselect("Difficulty", DIFFS, placeholder="Difficulty", label_visibility="collapsed")
    tsel = f3.multiselect("Tags", tags, placeholder="Tags", label_visibility="collapsed")
    mine = subs(user=user, limit=3000, cols="problem_id,verdict") if user else []
    solved = {s["problem_id"] for s in mine if s["verdict"] == "Accepted"}
    tried = {s["problem_id"] for s in mine}
    shown = [p for p in plist
             if (not q or q.lower() in p["title"].lower() or q.strip() == str(p["id"]))
             and (not dsel or p["difficulty"] in dsel)
             and (not tsel or set(tsel) & set(p.get("tags") or []))]
    st.caption(f"{len(shown)} problem(s)")
    for p in shown:
        with st.container(border=True):
            a, b, c, d, e = st.columns([0.5, 4, 1.2, 3, 1.2], vertical_alignment="center")
            a.markdown("✅" if p["id"] in solved else "🟡" if p["id"] in tried else "⚪")
            b.markdown(f"**#{p['id']} · {html.escape(p['title'])}**", unsafe_allow_html=True)
            c.markdown(badge(p["difficulty"]), unsafe_allow_html=True)
            d.markdown(tag_html(p.get("tags")), unsafe_allow_html=True)
            e.button("Open", key=f"open{p['id']}", on_click=open_problem, args=(p["id"],))


def problem_page(user):
    p = next((x for x in problems() if x["id"] == ss.pid), None)
    if not p:
        close_problem()
        st.rerun()
    st.button("← All problems", on_click=close_problem)
    st.title(f"#{p['id']} · {p['title']}")
    st.markdown(f"{badge(p['difficulty'])} &nbsp; ⏱ {p['time_limit']}s &nbsp; 💾 {p['memory_limit']} MB &nbsp; "
                f"{tag_html(p.get('tags'))}", unsafe_allow_html=True)
    t_statement, t_submit, t_hist = st.tabs(["📄 Statement", "💻 Submit", "🕘 My submissions"])
    tests = tests_of(p["id"])

    with t_statement:
        st.markdown(p["statement"])
        for title, key in (("Input", "input_format"), ("Output", "output_format"),
                           ("Constraints", "constraints_text")):
            if p.get(key):
                st.markdown(f"#### {title}")
                st.markdown(p[key])
        samples = [t for t in tests if t["is_sample"]]
        for i, t in enumerate(samples, 1):
            st.markdown(f"#### Sample {i}")
            a, b = st.columns(2)
            a.caption("Input"); a.code(t["input"] or " ", language="text")
            b.caption("Output"); b.code(t["expected_output"], language="text")

    with t_submit:
        langs = available_langs()
        lang = st.selectbox("Language", langs, key="lang")
        code = st.text_area("Your solution", TEMPLATES[lang], height=360, key=f"code_{p['id']}_{lang}")
        up = st.file_uploader("…or upload a source file", type=["py", "cpp", "c", "java"], key=f"up{p['id']}")
        if up:
            code = up.getvalue().decode("utf-8", "ignore")
            st.caption(f"Using uploaded file: {up.name}")
        with st.expander("🧪 Test with custom input"):
            cin = st.text_area("Custom input", height=100, key=f"cin{p['id']}")
            if st.button("Run", key=f"runc{p['id']}"):
                if not code.strip():
                    st.warning("Write some code first.")
                else:
                    with st.spinner("Running…"):
                        r = judge(code, lang, [dict(input=cin, expected_output=None)],
                                  float(p["time_limit"]), p["memory_limit"])
                    if r["error"]:
                        st.error("Compilation Error"); st.code(r["error"], language="text")
                    else:
                        c = r["cases"][0]
                        st.caption(f"{c['v']} · {c['ms']} ms")
                        st.code(c["got"] or "(no output)", language="text")
        if st.button("🚀 Submit solution", type="primary", key=f"sub{p['id']}"):
            if not user:
                st.warning("Enter a username in the sidebar first.")
            elif not code.strip():
                st.warning("Your solution is empty.")
            elif not tests:
                st.error("This problem has no test cases yet.")
            else:
                with st.spinner(f"Judging on {len(tests)} test(s)…"):
                    r = judge(code, lang, tests, float(p["time_limit"]), p["memory_limit"])
                try:
                    client().table("submissions").insert(dict(
                        username=user, problem_id=p["id"], language=lang, code=code, verdict=r["verdict"],
                        passed=r["passed"], total=r["total"], runtime_ms=r["ms"])).execute()
                except Exception as e:  # noqa: BLE001
                    st.error(f"Could not save submission: {e}")
                show_result(r)
                if r["verdict"] == "Accepted":
                    st.balloons()

    with t_hist:
        if not user:
            st.info("Enter a username in the sidebar to see your submissions.")
        else:
            rows = subs(user=user, pid=p["id"])
            sub_table(rows, {p["id"]: p["title"]})
            if rows:
                sid = st.selectbox("View code of submission", [r["id"] for r in rows], key=f"vc{p['id']}")
                got = run(client().table("submissions").select("code,language").eq("id", sid))
                if got:
                    st.code(got[0]["code"], language={"Python 3": "python", "C++17": "cpp", "C11": "c",
                                                      "Java": "java"}.get(got[0]["language"], "text"))


def my_submissions(user):
    st.title("📜 My Submissions")
    if not user:
        st.info("Enter a username in the sidebar.")
        return
    sub_table(subs(user=user, limit=300), {p["id"]: p["title"] for p in problems(True)})


def leaderboard():
    st.title("🏆 Leaderboard")
    rows = subs(limit=5000, cols="username,problem_id,verdict")
    if not rows:
        st.info("No submissions yet — be the first!")
        return
    df = pd.DataFrame(rows)
    ac = df[df["verdict"] == "Accepted"].drop_duplicates(["username", "problem_id"]).groupby("username").size()
    lb = pd.concat([ac.rename("Solved"), df.groupby("username").size().rename("Submissions")], axis=1)
    lb = lb.fillna(0).astype(int).sort_values(["Solved", "Submissions"], ascending=[False, True])
    lb = lb.rename_axis("User").reset_index()
    lb.insert(0, "Rank", range(1, len(lb) + 1))
    st.dataframe(lb, hide_index=True)


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def test_inputs(key, existing):
    n = st.number_input("Manual test cases", 0, 200, min(max(len(existing), 2), 200) if existing or key.startswith("ednew") else 0,
                        key=f"{key}_n")
    out = []
    for i in range(int(n)):
        e = existing[i] if i < len(existing) else {}
        with st.expander(f"Test case #{i + 1}", expanded=i < 2):
            c1, c2 = st.columns(2)
            a = c1.text_area("Input", e.get("input", ""), height=120, key=f"{key}_i{i}")
            b = c2.text_area("Expected output", e.get("expected_output", ""), height=120, key=f"{key}_o{i}")
            s = st.checkbox("Show as sample", bool(e.get("is_sample", i == 0)), key=f"{key}_s{i}")
        out.append(dict(input=a, expected_output=b, is_sample=s))
    ups = st.file_uploader("Bulk import test files (pairs like 1.in / 1.out)", accept_multiple_files=True,
                           key=f"{key}_up")
    files = {f.name.lower(): f.getvalue().decode("utf-8", "replace") for f in ups or []}
    for name in sorted(files):
        stem, ext = os.path.splitext(name)
        if ext in (".in", ".inp", ".input"):
            for oe in (".out", ".ans", ".output", ".sol"):
                if stem + oe in files:
                    out.append(dict(input=files[name], expected_output=files[stem + oe], is_sample=False))
                    break
    if ups:
        st.caption(f"{len(files)} file(s) uploaded → {len(out) - int(n)} pair(s) detected.")
    return out


def problem_editor(p=None):
    k = f"ed{p['id']}" if p else f"ednew{ss.ed_ver}"
    existing_tests = tests_of(p["id"]) if p else []
    p = p or {}
    c1, c2, c3 = st.columns([3, 1, 1])
    title = c1.text_input("Title", p.get("title", ""), key=k + "t")
    diff = c2.selectbox("Difficulty", DIFFS, DIFFS.index(p.get("difficulty", "Easy")), key=k + "d")
    pub = c3.toggle("Published", bool(p.get("is_published", True)), key=k + "p")
    c4, c5, c6 = st.columns(3)
    tl = c4.number_input("Time limit (s)", 0.5, 15.0, float(p.get("time_limit", 2.0)), 0.5, key=k + "tl")
    ml = c5.number_input("Memory limit (MB)", 16, 1024, int(p.get("memory_limit", 256)), 16, key=k + "ml")
    tags = c6.text_input("Tags (comma separated)", ", ".join(p.get("tags") or []), key=k + "tg")
    stmt = st.text_area("Problem statement (Markdown + LaTeX)", p.get("statement", ""), height=220, key=k + "s")
    inf = st.text_area("Input format", p.get("input_format", ""), height=80, key=k + "if")
    outf = st.text_area("Output format", p.get("output_format", ""), height=80, key=k + "of")
    cons = st.text_area("Constraints", p.get("constraints_text", ""), height=80, key=k + "c")
    st.markdown("##### Test cases")
    tests = test_inputs(k, existing_tests)
    if st.button("💾 Save problem", type="primary", key=k + "save"):
        tests = [dict(input=t["input"].replace("\r\n", "\n"), expected_output=t["expected_output"].replace("\r\n", "\n"),
                      is_sample=t["is_sample"]) for t in tests if t["expected_output"].strip()]
        if not title.strip() or not stmt.strip():
            st.error("Title and statement are required.")
        elif not tests:
            st.error("Add at least one test case with an expected output.")
        else:
            data = dict(title=title.strip(), difficulty=diff, is_published=pub, time_limit=tl, memory_limit=int(ml),
                        tags=[t.strip() for t in tags.split(",") if t.strip()], statement=stmt,
                        input_format=inf, output_format=outf, constraints_text=cons)
            try:
                c = client()
                if p.get("id"):
                    pid = p["id"]
                    c.table("problems").update(data).eq("id", pid).execute()
                    c.table("test_cases").delete().eq("problem_id", pid).execute()
                else:
                    pid = c.table("problems").insert(data).execute().data[0]["id"]
                c.table("test_cases").insert([dict(problem_id=pid, **t) for t in tests]).execute()
            except Exception as e:  # noqa: BLE001
                st.error(f"Save failed: {e}")
            else:
                ss.flash = f"Problem #{pid} saved with {len(tests)} test case(s)."
                if not p.get("id"):
                    ss.ed_ver += 1
                st.rerun()


def admin_page():
    st.title("🔐 Admin Panel")
    pwd = str(secret("ADMIN_PASSWORD"))
    if not ss.admin:
        if not pwd:
            st.warning("Set ADMIN_PASSWORD in your Streamlit secrets to enable the admin panel.")
            return
        entered = st.text_input("Admin password", type="password")
        if st.button("Sign in", type="primary"):
            if hmac.compare_digest(entered.encode(), pwd.encode()):
                ss.admin = True
                st.rerun()
            st.error("Invalid password.")
        return
    msg = ss.pop("flash", None)
    if msg:
        st.toast(msg, icon="✅")
    if st.button("Sign out"):
        ss.admin = False
        st.rerun()
    t_add, t_manage, t_subs = st.tabs(["➕ Add problem", "🛠 Manage problems", "📊 All submissions"])
    with t_add:
        problem_editor(None)
    with t_manage:
        allp = problems(True)
        if not allp:
            st.info("No problems yet.")
        else:
            opts = {f"#{x['id']} · {x['title']}" + ("" if x["is_published"] else "  (hidden)"): x for x in allp}
            p = opts[st.selectbox("Select a problem", list(opts))]
            with st.expander("✏️ Edit problem", expanded=True):
                problem_editor(p)
            with st.expander("🗑 Danger zone"):
                ok = st.checkbox("Delete this problem with all its test cases and submissions", key=f"cf{p['id']}")
                if st.button("Delete problem", disabled=not ok, key=f"del{p['id']}"):
                    run(client().table("problems").delete().eq("id", p["id"]))
                    ss.flash = f"Problem #{p['id']} deleted."
                    st.rerun()
    with t_subs:
        verdicts = st.multiselect("Filter by verdict", list(ICON), key="adm_v")
        rows = subs(limit=500)
        rows = [r for r in rows if not verdicts or r["verdict"] in verdicts]
        sub_table(rows, {p["id"]: p["title"] for p in problems(True)})


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #
if not secret("SUPABASE_URL") or not secret("SUPABASE_KEY"):
    st.error("Supabase is not configured. Add SUPABASE_URL and SUPABASE_KEY to `.streamlit/secrets.toml`.")
    st.stop()

with st.sidebar:
    if LOGO.exists():
        st.image(str(LOGO))
    st.radio("Navigate", [HOME, PROB, LB, MYS, ADM], key="page", label_visibility="collapsed")
    st.divider()
    st.text_input("👤 Username", key="user", placeholder="your handle", max_chars=30)
    st.caption("Languages: " + ", ".join(available_langs()))
user = (ss.get("user") or "").strip()

page = ss.page
if page == HOME:
    home_page()
elif page == PROB:
    problem_page(user) if ss.pid else problem_list(user)
elif page == LB:
    leaderboard()
elif page == MYS:
    my_submissions(user)
else:
    admin_page()
